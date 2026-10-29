"""Explicitly bounded, broad-only analysis for one source interval."""

from __future__ import annotations

import json
import os
import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from video_editor.analysis.gemini import GeminiAdapter
from video_editor.analysis.models import (
    BroadScanResponse,
    RetryPolicy,
    SourceRange,
)
from video_editor.analysis.orchestrator import (
    AnalysisCacheIdentity,
    BroadAnalysisRequest,
    run_broad_analysis,
)
from video_editor.analysis.pricing import pricing_for_model
from video_editor.analysis.proxy_chunks import (
    CloudProxySettings,
    create_cloud_proxy_chunk,
    register_proxy_manifest,
)
from video_editor.config import AppConfig
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.discovery import discover_sources
from video_editor.persistence.database import JobStore
from video_editor.workflow import (
    BROAD_PROMPT_VERSION,
    STAGE_IMPLEMENTATION_VERSIONS,
    WorkflowService,
)

_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REQUEST_CAP = Decimal("0.262051")
_TOTAL_CAP = Decimal("1.50")


class _CostChunk:
    def __init__(self, chunk_id: str, proxy_start: Decimal, proxy_end: Decimal) -> None:
        self.chunk_id = chunk_id
        self.proxy_start = proxy_start
        self.proxy_end = proxy_end


def _claim_attempt(state_dir: Path, attempt_id: str) -> Path:
    """Durably claim one attempt ID; existing claim always blocks replay."""
    if not _ATTEMPT_ID.fullmatch(attempt_id) or attempt_id in {".", ".."}:
        raise VideoEditorError(ErrorCategory.STATE, "invalid bounded attempt ID")
    root = state_dir / "bounded-attempts"
    try:
        if state_dir.is_symlink():
            raise VideoEditorError(
                ErrorCategory.STATE, "bounded state root is a symlink"
            )
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_stat = root.lstat()
        if (
            root.is_symlink()
            or not root.is_dir()
            or root_stat.st_uid != os.getuid()
            or (root_stat.st_mode & 0o077) != 0
        ):
            raise VideoEditorError(
                ErrorCategory.STATE, "bounded attempt root is unsafe"
            )
        claim = root / attempt_id
        claim.mkdir(mode=0o700, exist_ok=False)
        claim_stat = claim.lstat()
        if (
            claim.is_symlink()
            or not claim.is_dir()
            or claim_stat.st_uid != os.getuid()
            or (claim_stat.st_mode & 0o077) != 0
        ):
            raise VideoEditorError(
                ErrorCategory.STATE, "bounded attempt claim is unsafe"
            )
        claim_file = claim / "claimed.json"
        descriptor = os.open(
            claim_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w") as handle:
            handle.write(
                json.dumps({"attempt_id": attempt_id, "state": "claimed"}) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(claim, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        root_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        return claim
    except FileExistsError as exc:
        raise VideoEditorError(
            ErrorCategory.STATE, "bounded attempt ID already exists; refusing replay"
        ) from exc
    except OSError as exc:
        raise VideoEditorError(
            ErrorCategory.STATE, "cannot safely claim bounded attempt ID"
        ) from exc


def _write_attempt_state(claim: Path, payload: dict[str, str]) -> None:
    """Atomically persist attempt status or surface the filesystem error."""
    temporary = claim / ".claimed.json.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, claim / "claimed.json")
        directory_fd = os.open(claim, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            raise VideoEditorError(
                ErrorCategory.STATE, "attempt status write and cleanup both failed"
            ) from cleanup_error
        raise VideoEditorError(
            ErrorCategory.STATE, "cannot persist attempt status"
        ) from exc


def _request_maximum(duration: Decimal, config: AppConfig) -> Decimal:
    """Compute static maximum offline using current pinned estimator."""
    pricing = pricing_for_model(config.gemini.model)
    # Importing adapter imports SDK types but does not instantiate a client/key.
    chunk = _CostChunk(
        chunk_id="preflight",
        proxy_start=Decimal(0),
        proxy_end=duration,
    )
    return GeminiAdapter._estimate_request_maximum(
        chunk,
        pricing,
        fps=Decimal("0.5"),
        attempts=config.gemini.max_transient_attempts,
        response_model=BroadScanResponse,
    )


def preflight(
    source: Path, start: Decimal, end: Decimal, config: AppConfig
) -> dict[str, Any]:
    """Return nominal estimate without JobStore, source reads, media work, or secrets."""
    if not start.is_finite() or not end.is_finite() or start < 0 or end <= start:
        raise VideoEditorError(
            ErrorCategory.ANALYSIS, "source interval must be finite and positive"
        )
    duration = end - start
    if (
        config.gemini.model != "gemini-3.8-flash"
        or config.gemini.max_transient_attempts != RetryPolicy().max_attempts
    ):
        raise VideoEditorError(
            ErrorCategory.BUDGET,
            "bounded request requires the approved Gemini model and retry count",
        )
    if duration > Decimal(config.gemini.chunk_max_seconds):
        raise VideoEditorError(
            ErrorCategory.ANALYSIS, "interval exceeds maximum chunk duration"
        )
    maximum = _request_maximum(duration, config)
    if maximum > _REQUEST_CAP or _REQUEST_CAP > _TOTAL_CAP:
        raise VideoEditorError(
            ErrorCategory.BUDGET, "bounded request exceeds approved cap"
        )
    return {
        "source": str(source),
        "source_interval": [str(start), str(end)],
        "source_duration": "unknown until dispatch preflight",
        "proxy_digest": "unknown until bounded proxy generation",
        "request_maximum_usd": str(maximum),
        "request_cap_usd": str(_REQUEST_CAP),
        "total_cap_usd": str(_TOTAL_CAP),
        "dispatch_required": True,
        "nominal_only": True,
    }


def run_bounded_broad(
    *,
    source: Path,
    start: Decimal,
    end: Decimal,
    attempt_id: str = "preflight",
    config: AppConfig,
    dispatch: bool = False,
) -> dict[str, Any]:
    """Inspect one source and dispatch one broad request only with explicit opt-in."""
    estimate = preflight(source, start, end, config)
    if not dispatch:
        return estimate
    if not source.is_file() or source.is_symlink():
        raise VideoEditorError(
            ErrorCategory.INSPECTION,
            "source must be an existing regular non-symlink file",
        )
    source = source.resolve(strict=True)
    candidates = discover_sources(source)
    if len(candidates) != 1 or candidates[0].path.resolve() != source:
        raise VideoEditorError(
            ErrorCategory.INSPECTION, "source selection is not unique"
        )
    WorkflowService.validate_configured_roots(config, source, [source])
    claim = _claim_attempt(config.paths.state_dir, attempt_id)
    store = JobStore(config.paths.state_dir / "jobs.sqlite3")
    with store:
        service = WorkflowService(config, store)
        service_result = service.inspect(source)
        job_id = str(service_result["job_id"])
        _write_attempt_state(
            claim, {"attempt_id": attempt_id, "job_id": job_id, "state": "preparing"}
        )
        generated_root = config.paths.cache_dir / job_id
        generated_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        inspection = service_result
        if len(inspection["sources"]) != 1:
            raise VideoEditorError(
                ErrorCategory.INSPECTION,
                "source inspection did not yield exactly one source",
            )
        if Decimal(str(inspection["sources"][0]["probe"]["duration"])) < end:
            raise VideoEditorError(
                ErrorCategory.ANALYSIS, "requested interval exceeds source duration"
            )
        service._ensure_proxy(job_id)
        job = store.get_job(job_id)
        sources = job.get("sources", [])
        if len(sources) != 1:
            raise VideoEditorError(
                ErrorCategory.STATE, "bounded attempt requires exactly one source"
            )
        source_row = sources[0]
        metadata = store.get_proxy_artifact_metadata(job_id, source_row["source_id"])
        if metadata is None or not isinstance(metadata.get("mapping"), dict):
            raise VideoEditorError(
                ErrorCategory.ANALYSIS, "upstream proxy provenance missing"
            )
        mapping_data = metadata["mapping"]
        from video_editor.workflow import _mapping

        manifest = create_cloud_proxy_chunk(
            source=source,
            generated_root=config.paths.cache_dir / job_id,
            mapping=_mapping(mapping_data),
            source_start=start,
            source_end=end,
            job_id=job_id,
            source_fingerprint=source_row["fingerprint"],
            paths=config.paths,
            store=store,
            settings=CloudProxySettings(
                target_seconds=end - start,
                minimum_seconds=min(
                    Decimal(config.gemini.chunk_min_seconds), end - start
                ),
                maximum_seconds=min(
                    Decimal(config.gemini.chunk_max_seconds), end - start
                ),
            ),
        )
        register_proxy_manifest(manifest, store)
        maximum = _request_maximum(
            manifest.data.proxy_end - manifest.data.proxy_start, config
        )
        if maximum > _REQUEST_CAP:
            raise VideoEditorError(
                ErrorCategory.BUDGET,
                "final digest-bound request maximum exceeds approved cap",
            )
        store.initialize_budget(job_id, _TOTAL_CAP)
        identity = AnalysisCacheIdentity(
            source_identity=manifest.data.source_identity,
            source_fingerprint=manifest.data.source_fingerprint,
            proxy_digest=manifest.digest,
            mapping_version=manifest.data.mapping_version,
            source_start=start,
            source_end=end,
            mode="broad",
            fps=Decimal("0.5"),
            media_width=manifest.data.video_width,
            media_height=manifest.data.video_height,
            provider="gemini",
            model=config.gemini.model,
            prompt_version=BROAD_PROMPT_VERSION,
            response_schema_version="broad-v2",
            implementation_version=STAGE_IMPLEMENTATION_VERSIONS["analyze"],
        )
        request = BroadAnalysisRequest(
            manifest_id=manifest.manifest_id,
            chunk_id=manifest.chunk_id,
            cache_identity=identity,
            maximum_cost_usd=maximum,
            request_id=str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"bounded-broad:{attempt_id}")
            ),
        )
        # Client construction follows the final digest-bound cap check.
        provider = GeminiAdapter.from_environment(
            pricing=pricing_for_model(config.gemini.model),
            retry_policy=RetryPolicy(max_attempts=config.gemini.max_transient_attempts),
        )
        outcome = run_broad_analysis(
            store=store,
            paths=config.paths,
            provider=provider,
            job_id=job_id,
            expected_ranges=[
                SourceRange(source_id=manifest.source_id, start=start, end=end)
            ],
            requests=[request],
        )
        _write_attempt_state(
            claim, {"attempt_id": attempt_id, "job_id": job_id, "state": outcome.state}
        )
        return {
            "job_id": job_id,
            "attempt_id": attempt_id,
            "state": outcome.state,
            "coverage": outcome.coverage.model_dump(mode="json"),
            "budget": store.budget_state(job_id).model_dump(mode="json"),
            "request_maximum_usd": str(maximum),
            "broad_requests": 1,
            "candidate_refinement": False,
            "render": False,
        }
