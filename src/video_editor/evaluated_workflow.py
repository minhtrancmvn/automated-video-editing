"""Explicit bundle-backed workflow admission; never invoked by ordinary resume."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field

from video_editor.analysis.evaluation import (
    EvaluationBundle,
    admit_completed_bundle,
    evaluated_reviews,
)
from video_editor.analysis.evidence import (
    EVIDENCE_MAP_VERSION,
    TRANSITION_MAP_VERSION,
    derive_transition_evidence,
    map_ranking_evidence,
    subject_seeds,
)
from video_editor.analysis.models import (
    AnalysisModel,
    AnalysisProvider,
    AnalysisResultData,
    BroadScanResponse,
    CandidateRefinementResponse,
    CandidateWindow,
    CropTrack,
    LocalSegmentation,
    NonEmptyString,
)
from video_editor.analysis.orchestrator import (
    CandidateAnalysisRequest,
    _candidate_fps,
    _candidate_identity,
    analysis_cache_key,
)
from video_editor.analysis.ranking import rank_candidates
from video_editor.analysis.segmentation import (
    _IMPLEMENTATION_VERSION as SEGMENTATION_VERSION,
)
from video_editor.analysis.segmentation import (
    SegmentationSettings,
)
from video_editor.analysis.segmentation import (
    _settings_hash as segmentation_settings_hash,
)
from video_editor.analysis.selection import Digest, RefinementApproval, SelectionReview
from video_editor.analysis.tracking import TRACKING_VERSION, track_subject
from video_editor.config import AppConfig
from video_editor.media.discovery import IDENTITY_VERSION, bounded_fingerprint
from video_editor.media.probe import probe_media
from video_editor.media.proxies import ProxySettings
from video_editor.media.proxies import settings_hash as proxy_settings_hash
from video_editor.models.edit_plan import EditPlanV2, PlanSourceV2
from video_editor.persistence.database import JobStore
from video_editor.planning.highlight_plan import (
    create_highlight_plans,
    short_eligibility_diagnostics,
)
from video_editor.rendering.compiler import compile_render
from video_editor.rendering.runner import run_render
from video_editor.validation.outputs import validate_phase2_output
from video_editor.workflow import (
    CANDIDATE_IDENTITY_VERSION,
    CANDIDATE_PROMPT_VERSION,
    RANKING_SETTINGS,
    STAGE_IMPLEMENTATION_VERSIONS,
    WorkflowService,
    _hash,
    _mapping,
)


class EvaluatedWorkflowService(WorkflowService):
    """Revalidate registered evaluator provenance before each selection access.

    Construction grants no paid refinement or render permission. Inherited final
    selection denial and separate refinement approval remain unchanged.
    """

    def __init__(
        self,
        config: AppConfig,
        store: JobStore,
        progress: Callable[[str], None] | None = None,
        *,
        evaluation_bundle: EvaluationBundle,
        provider: AnalysisProvider | None = None,
        refinement_approval: RefinementApproval | None = None,
    ) -> None:
        # Base constructor assigns its default reviews through the override setter.
        self._reviews_initialized = False
        super().__init__(
            config,
            store,
            progress,
            provider=provider,
            selection_reviews=(),
            refinement_approval=refinement_approval,
        )
        self._evaluation_bundle = evaluation_bundle
        self._reviews_initialized = True
        # Provenance validation waits until selection_reviews is accessed, after
        # callers open JobStore. Every access revalidates ledger and media state.

    @property
    def selection_reviews(self) -> tuple[SelectionReview, ...]:
        return evaluated_reviews(
            self._evaluation_bundle, store=self.store, paths=self.config.paths
        )

    @selection_reviews.setter
    def selection_reviews(self, value: tuple[SelectionReview, ...]) -> None:
        # The base constructor assigns its empty default before config/store.
        if value or self._reviews_initialized:
            raise ValueError(
                "legacy review override is forbidden for evaluated workflow"
            )
        self._reviews_initialized = True


class ReviewedDraft(AnalysisModel):
    """Whole deterministic plan set; neither paid authority nor edit acceptance."""

    model_config = ConfigDict(extra="forbid")

    job_id: NonEmptyString
    evaluation_digest: Digest
    provenance_digest: Digest
    plans: tuple[EditPlanV2, ...] = Field(min_length=1)
    tracks: tuple[CropTrack, ...]
    metadata: dict[str, Any] = Field(default_factory=dict)
    paid_dispatch_authorized: Literal[False] = False
    final_edit_accepted: Literal[False] = False


class ReviewedRenderApproval(AnalysisModel):
    """Trusted local assertion of exact-draft render scope, not owner authentication."""

    model_config = ConfigDict(extra="forbid")

    approval_id: NonEmptyString
    draft_digest: Digest


_REVIEWED_VERSION = "reviewed-draft-v1"


def reviewed_draft_digest(draft: ReviewedDraft) -> str:
    """Bind every serialized field, including every plan and crop observation."""
    return hashlib.sha256(
        json.dumps(
            draft.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _fresh_directory(path: Path, protected_roots: tuple[Path, ...]) -> None:
    """Check only; creation must remain after approval and current evidence gates."""
    if path.exists() or path.is_symlink():
        raise ValueError("fresh reviewed output required")
    if not path.parent.is_dir():
        raise ValueError("reviewed output parent must exist")
    # Resolve macOS /tmp aliases supplied by config before calling this boundary.
    if any(parent.is_symlink() for parent in path.absolute().parents):
        raise ValueError("reviewed output must not use symlink components")
    resolved = path.resolve()
    if any(
        resolved.is_relative_to(root.resolve())
        or root.resolve().is_relative_to(resolved)
        for root in protected_roots
    ):
        raise ValueError("reviewed output overlaps protected tree")


def _file_digest(path: Path) -> str:
    """Hash current evidence bytes and reject changes during reading."""
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if path.stat() != before:
        raise ValueError("current evidence changed while reading")
    return digest.hexdigest()


def _write_reviewed_json(path: Path, value: dict[str, Any]) -> None:
    """Create private, durable audit files without replacing existing evidence."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(
            json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_reviewed_media(staged: Path, final: Path) -> None:
    """Atomically publish validated media without replacement or exFAT hardlinks."""
    if sys.platform == "darwin":
        import ctypes

        library = ctypes.CDLL(None, use_errno=True)
        rename = library.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        # Darwin RENAME_EXCL; unlike plain rename, an existing target is preserved.
        if rename(os.fsencode(staged), os.fsencode(final), 0x00000004) != 0:
            error = ctypes.get_errno()
            raise OSError(error, "exclusive reviewed publication failed", str(final))
    else:
        # Other supported local filesystems may provide atomic no-replace links.
        # Unsupported filesystems fail closed; never fall back to overwriting rename.
        os.link(staged, final)
        staged.unlink()


def _completed_analysis_request(
    store: JobStore, job_id: str, request_id: str, cache_key: str
) -> None:
    """Read billing state only; cached output cannot turn an unknown hold into permission."""
    row = store.connection.execute(
        "SELECT status, actual_cost_microusd, maximum_cost_microusd FROM analysis_requests WHERE job_id = ? AND request_id = ? AND cache_key = ?",
        (job_id, request_id, cache_key),
    ).fetchone()
    if (
        row is None
        or row["status"] != "completed"
        or row["actual_cost_microusd"] is None
        or not 0 <= row["actual_cost_microusd"] <= row["maximum_cost_microusd"]
    ):
        raise ValueError("completed analysis billing required")


class ReviewedWorkflowService(EvaluatedWorkflowService):
    """Read existing evidence; prepare locally, then render only exact approved drafts.

    No call into ordinary run/resume, refinement, or writable stage orchestration.
    """

    def __init__(
        self, config: AppConfig, store: JobStore, *, evaluation_bundle: EvaluationBundle
    ) -> None:
        super().__init__(config, store, evaluation_bundle=evaluation_bundle)

    def _reviewed_evidence(
        self,
    ) -> tuple[dict[str, Any], dict[str, LocalSegmentation], str]:
        """Revalidate and bind local stage records, media bytes, settings and lineage."""
        self._require_open_store()
        admit_completed_bundle(
            self._evaluation_bundle, store=self.store, paths=self.config.paths
        )
        job = self.store.get_job(self._evaluation_bundle.audit.plan.job_id)
        stages = job.get("stages", {})
        for name in ("proxy", "segment"):
            stage = stages.get(name)
            if not stage or stage["status"] != "completed":
                raise ValueError(f"completed {name} stage required")
            if stage["implementation_version"] != STAGE_IMPLEMENTATION_VERSIONS[name]:
                raise ValueError(f"completed {name} stage implementation mismatch")
        proxy_stage = stages["proxy"]
        segment_stage = stages["segment"]
        sources = job["sources"]
        if not sources:
            raise ValueError("current sources required")
        proxy_settings = ProxySettings()
        proxy_fingerprint = {
            "identity_version": IDENTITY_VERSION,
            "current_identity": [
                {
                    "source_id": item["source_id"],
                    "identity": f"{IDENTITY_VERSION}:{item['fingerprint']}",
                }
                for item in sources
            ],
        }
        expected_proxy_settings = {
            "cache": str(self.config.paths.cache_dir),
            "proxy_settings": asdict(proxy_settings),
            "tool_version": "ffmpeg",
        }
        artifacts = proxy_stage["result"]["artifacts"]
        for stage, fingerprint, settings in (
            (proxy_stage, proxy_fingerprint, expected_proxy_settings),
            (
                segment_stage,
                artifacts,
                {"segment": STAGE_IMPLEMENTATION_VERSIONS["segment"]},
            ),
        ):
            if (stage["input_fingerprint"], stage["settings_hash"]) != self._stage_keys(
                fingerprint, settings
            ):
                raise ValueError("current local stage identity mismatch")
        if {item["source_id"] for item in artifacts} != {
            item["source_id"] for item in sources
        } or len(artifacts) != len(sources):
            raise ValueError("current proxy source coverage mismatch")
        source_paths = [Path(item["path"]) for item in sources]
        self._validate_roots(self.config.paths.input_dir, source_paths)
        probes = self._source_probes(job)
        files: dict[str, Any] = {}
        for item in sources:
            path = Path(item["path"])
            current_stat = path.stat()
            if (
                item["identity_version"] != IDENTITY_VERSION
                or bounded_fingerprint(path) != item["fingerprint"]
                or current_stat.st_size != item["size_bytes"]
            ):
                raise ValueError("current source identity mismatch")
            if probes[item["source_id"]] != probe_media(path):
                raise ValueError("current source probe mismatch")
            files[str(path)] = {
                "size": current_stat.st_size,
                "mtime_ns": current_stat.st_mtime_ns,
                "device": current_stat.st_dev,
                "inode": current_stat.st_ino,
            }
        segmentations: dict[str, LocalSegmentation] = {}
        segment_paths = segment_stage["result"]["segmentations"]
        if set(segment_paths) != {item["source_id"] for item in sources}:
            raise ValueError("current segmentation coverage mismatch")
        for artifact in artifacts:
            source_id = artifact["source_id"]
            source = next(item for item in sources if item["source_id"] == source_id)
            mapping = _mapping(artifact["mapping"])
            if (
                mapping.source_id != source_id
                or mapping.source_identity
                != f"{IDENTITY_VERSION}:{source['fingerprint']}"
                or mapping.settings_hash != proxy_settings_hash(proxy_settings)
                or mapping.tool_version != "ffmpeg"
                or mapping.source_start != 0
                or mapping.proxy_start != 0
                or mapping.source_end != Decimal(str(probes[source_id].duration))
                or mapping.proxy_end != mapping.source_end
            ):
                raise ValueError("current proxy mapping mismatch")
            for kind in ("proxy", "audio"):
                raw_path = artifact.get(kind)
                if kind == "audio" and (raw_path is not None) != (
                    probes[source_id].audio is not None
                ):
                    raise ValueError("current proxy audio coverage mismatch")
                if raw_path is None:
                    continue
                path = Path(raw_path)
                records = [
                    record
                    for record in job["artifacts"]
                    if record["stage"] == "proxy" and record["path"] == str(path)
                ]
                if len(records) != 1:
                    raise ValueError("registered local proxy artifact required")
                metadata = records[0]["metadata"]
                if (
                    metadata.get("kind") != kind
                    or metadata.get("source_id") != source_id
                    or metadata.get("source_path") != source["path"]
                    or metadata.get("settings") != asdict(proxy_settings)
                    or metadata.get("mapping") != artifact["mapping"]
                    or not self._proxy_artifact_valid(path, metadata)
                ):
                    raise ValueError("current proxy artifact provenance mismatch")
                files[str(path)] = _file_digest(path)
            segmentation_path = Path(segment_paths[source_id])
            if not any(
                record["stage"] == "segment"
                and record["path"] == str(segmentation_path)
                for record in job["artifacts"]
            ):
                raise ValueError("registered segmentation artifact required")
            segmentation = LocalSegmentation.model_validate_json(
                segmentation_path.read_text()
            )
            if (
                segmentation.schema_version != 1
                or segmentation.source_id != source_id
                or segmentation.source_identity != mapping.source_identity
                or segmentation.implementation_version != SEGMENTATION_VERSION
                or segmentation.settings_hash
                != segmentation_settings_hash(SegmentationSettings())
                or segmentation.proxy_settings_hash != mapping.settings_hash
                or segmentation.proxy_tool_version != mapping.tool_version
            ):
                raise ValueError("current segmentation provenance mismatch")
            files[str(segmentation_path)] = _file_digest(segmentation_path)
            segmentations[source_id] = segmentation
        provenance = _hash(
            {
                "version": _REVIEWED_VERSION,
                "evaluation": self._evaluation_bundle.model_dump(mode="json"),
                "sources": sources,
                "chronology": job.get("chronology", []),
                "volume": job["volume"],
                "job_config": job["config"],
                "stages": {name: stages[name] for name in ("proxy", "segment")},
                "artifacts": [
                    item
                    for item in job["artifacts"]
                    if item["stage"] in {"proxy", "segment"}
                ],
                "analysis_results": job.get("analysis_results", []),
                "proxy_manifests": job.get("proxy_manifests", []),
                "files": files,
                "config": asdict(self.config),
                "ranking": RANKING_SETTINGS.model_dump(mode="json"),
                "tracking": TRACKING_VERSION,
                "evidence_map": EVIDENCE_MAP_VERSION,
                "transition_map": TRANSITION_MAP_VERSION,
                "stages_version": STAGE_IMPLEMENTATION_VERSIONS,
            }
        )
        return job, segmentations, provenance

    def prepare_reviewed_plans(self) -> ReviewedDraft:
        """Derive plans from admitted primary proposals and existing exact refinements."""
        job, segmentations, provenance = self._reviewed_evidence()
        report = admit_completed_bundle(
            self._evaluation_bundle, store=self.store, paths=self.config.paths
        )
        if not report.eligible:
            raise ValueError("eligible reviewed primary selection required")
        job_id = self._evaluation_bundle.audit.plan.job_id
        chunk_map = {
            item["chunk_id"]: item
            for item in job["stages"]["segment"]["result"]["chunks"]
        }
        evidence = []
        refined_by_id = {}
        seen_ids: set[str] = set()
        for proposal in report.eligible:
            broad = proposal.broad
            chunk_item = chunk_map.get(proposal.chunk_id)
            if chunk_item is None:
                raise ValueError("current registered chunk required")
            chunk = self._chunk_data(job_id, proposal.chunk_id)
            if (
                chunk.source_id != chunk_item["source_id"]
                or chunk.source_identity != proposal.source_identity
                or chunk.source_identity != chunk_item["source_identity"]
                or chunk.source_start != Decimal(chunk_item["source_start"])
                or chunk.source_end != Decimal(chunk_item["source_end"])
                or chunk.proxy_start != Decimal(chunk_item["proxy_start"])
                or chunk.proxy_end != Decimal(chunk_item["proxy_end"])
                or chunk.mapping_version != chunk_item["mapping_version"]
            ):
                raise ValueError("current chunk provenance mismatch")
            broad_identity = self._broad_identity(chunk_item)
            stored_broad = self.store.find_analysis_result(
                job_id, analysis_cache_key(broad_identity)
            )
            if (
                stored_broad is None
                or not stored_broad["validated"]
                or stored_broad["mode"] != "broad"
                or stored_broad["chunk_id"] != proposal.chunk_id
            ):
                raise ValueError("current broad lineage required")
            broad_response = BroadScanResponse.model_validate(
                stored_broad["normalized"]
            )
            if broad not in broad_response.candidates:
                raise ValueError("current broad proposal mismatch")
            candidate_identity = broad_identity.model_copy(
                update={
                    "mode": "candidate",
                    "prompt_version": CANDIDATE_PROMPT_VERSION,
                    "response_schema_version": "candidate-v2",
                    "implementation_version": _hash(
                        [
                            CANDIDATE_IDENTITY_VERSION,
                            analysis_cache_key(broad_identity),
                            broad.candidate_id,
                            broad.start.as_integer_ratio(),
                            broad.end.as_integer_ratio(),
                        ]
                    ),
                }
            )
            request = CandidateAnalysisRequest(
                request_id="reviewed-identity-only",
                manifest_id=chunk_item["manifest_id"],
                candidate=CandidateWindow(
                    chunk_id=proposal.chunk_id,
                    candidate_id=broad.candidate_id,
                    start=broad.start,
                    end=broad.end,
                ),
                cache_identity=candidate_identity,
                maximum_cost_usd=Decimal("0.01"),
            )
            identity = _candidate_identity(
                request, _candidate_fps(request, list(segmentations.values()))
            )
            stored = self.store.find_analysis_result(
                job_id, analysis_cache_key(identity)
            )
            if (
                stored is None
                or not stored["validated"]
                or stored["mode"] != "candidate"
                or stored["chunk_id"] != proposal.chunk_id
            ):
                raise ValueError("current completed candidate refinement required")
            for record, expected in (
                (stored_broad, broad_identity),
                (stored, identity),
            ):
                metadata = AnalysisResultData.model_validate(record["data"])
                if (
                    metadata.provider != expected.provider
                    or metadata.model != expected.model
                    or metadata.prompt_version != expected.prompt_version
                    or metadata.response_schema_version
                    != expected.response_schema_version
                    or metadata.implementation_version
                    != expected.implementation_version
                ):
                    raise ValueError("current analysis metadata mismatch")
                _completed_analysis_request(
                    self.store, job_id, metadata.request_id, record["cache_key"]
                )
            refined = CandidateRefinementResponse.model_validate(stored["normalized"])
            if (
                refined.chunk_id != proposal.chunk_id
                or refined.candidate_id != broad.candidate_id
                or refined.start != broad.start
                or refined.end != broad.end
            ):
                raise ValueError("current candidate refinement interval mismatch")
            output_id = f"phase2:{proposal.chunk_id}:{broad.candidate_id}"
            if output_id in seen_ids:
                raise ValueError("reviewed candidate identities must be unique")
            seen_ids.add(output_id)
            refined = refined.model_copy(update={"candidate_id": output_id})
            refined_by_id[output_id] = (refined, chunk)
            evidence.append(
                map_ranking_evidence(
                    broad.model_copy(update={"candidate_id": output_id}),
                    refined,
                    chunk,
                    segmentations[chunk.source_id],
                    scenes=broad_response.scenes,
                )
            )
        ranked = rank_candidates(evidence, RANKING_SETTINGS)
        probes = self._source_probes(job)
        mappings = {
            item["source_id"]: (Path(item["proxy"]), _mapping(item["mapping"]))
            for item in job["stages"]["proxy"]["result"]["artifacts"]
        }
        tracks = []
        for candidate in ranked:
            if not candidate.selected:
                continue
            video = probes[candidate.source_id].video
            if video is None or video.width is None or video.height is None:
                raise ValueError("current video dimensions required")
            proxy, mapping = mappings[candidate.source_id]
            response, chunk = refined_by_id[candidate.candidate_id]
            tracks.append(
                track_subject(
                    proxy,
                    mapping,
                    clip_id=candidate.candidate_id,
                    candidate_start=candidate.source_start,
                    candidate_end=candidate.source_end,
                    subject_observations=subject_seeds(response, chunk),
                    source_size=(video.width, video.height),
                    output_size=(1080, 1920),
                    settings=self.config.crop,
                )
            )
        groups = self._groups(job)
        chapters = {}
        positions = {}
        for group_index, group in enumerate(groups):
            for member_index, member in enumerate(group.members):
                chapters[member.source.fingerprint] = group.group_id
                positions[member.source.fingerprint] = (group_index, member_index)
        ordered = sorted(
            (item for item in ranked if item.selected),
            key=lambda item: (
                positions[item.source_id],
                item.source_start,
                item.candidate_id,
            ),
        )
        sources = [
            PlanSourceV2(
                id=source_id,
                path=probes[source_id].path,
                identity=mapping.source_identity,
                duration=Decimal(str(probes[source_id].duration)),
                has_audio=probes[source_id].audio is not None,
            )
            for source_id, (_, mapping) in mappings.items()
        ]
        plans = create_highlight_plans(
            ranked,
            groups,
            tracks,
            sources,
            derive_transition_evidence(ordered, chapters),
            self.config.highlights,
        )
        if self._reviewed_evidence()[2] != provenance:
            raise ValueError("current evidence changed during preparation")
        return ReviewedDraft(
            job_id=job_id,
            evaluation_digest=self._evaluation_bundle.audit_digest,
            provenance_digest=provenance,
            plans=(plans.long, *plans.shorts),
            tracks=tuple(tracks),
            metadata={
                "version": _REVIEWED_VERSION,
                "short_diagnostics": short_eligibility_diagnostics(
                    ranked, tracks, self.config.highlights, self.config.crop
                ),
                "themes": list(plans.themes),
                "chapters": list(plans.long_evidence.chapter_progression),
            },
        )

    def render_reviewed_plans(
        self, draft: ReviewedDraft, approval: ReviewedRenderApproval, output_root: Path
    ) -> dict[str, Any]:
        """Re-derive exact approved draft before writing any new local outputs."""
        if (
            not isinstance(approval, ReviewedRenderApproval)
            or not isinstance(draft, ReviewedDraft)
            or approval.draft_digest != reviewed_draft_digest(draft)
        ):
            raise ValueError("exact draft approval required")
        # model_copy can skip validation; never trust caller-created nested models.
        draft = ReviewedDraft.model_validate_json(draft.model_dump_json())
        current = self.prepare_reviewed_plans()
        if reviewed_draft_digest(current) != approval.draft_digest:
            raise ValueError("approved draft does not match current evidence")
        protected_roots = (
            self.config.paths.input_dir,
            self.config.paths.state_dir,
            self.config.paths.cache_dir,
            self.config.paths.workspace_dir,
            *(source.path.parent for plan in draft.plans for source in plan.sources),
            *(
                Path(item.manifest.source_path).parent
                for item in self._evaluation_bundle.audit.plan.inputs
            ),
        )
        _fresh_directory(output_root, protected_roots)
        names = [plan.output.filename for plan in draft.plans]
        if len(set(names)) != len(names) or any(
            Path(name).name != name
            or name.startswith(".")
            or name in {"audit.json", "failure.json", "draft.json"}
            for name in names
        ):
            raise ValueError("unique safe render output filenames required")
        required = sum(
            source.path.stat().st_size
            for plan in draft.plans
            for source in plan.sources
        )
        volume = self._destination_volume(output_root, required)
        _fresh_directory(output_root, protected_roots)
        output_root.mkdir(mode=0o700)
        published: list[dict[str, Any]] = []
        try:
            _write_reviewed_json(
                output_root / "draft.json", draft.model_dump(mode="json")
            )
            staging = output_root / ".staging"
            staging.mkdir(mode=0o700)
            for plan in draft.plans:
                self._destination_volume(output_root, required, volume)
                if self._reviewed_evidence()[2] != draft.provenance_digest:
                    raise ValueError("current evidence changed before render")
                command = compile_render(
                    plan, "ffmpeg", output_dir=staging, output_name=plan.output.filename
                )
                run_render(command, on_interrupt=lambda: None)
                validation = validate_phase2_output(
                    command.final_path, plan, crop_track_evidence=draft.tracks
                )
                if self._reviewed_evidence()[2] != draft.provenance_digest:
                    raise ValueError("current evidence changed before publication")
                self._destination_volume(output_root, 1, volume)
                final = output_root / plan.output.filename
                with command.final_path.open("rb") as handle:
                    os.fsync(handle.fileno())
                _publish_reviewed_media(command.final_path, final)
                published.append(
                    {
                        "output": str(final),
                        "sha256": _file_digest(final),
                        "validation": {
                            "probe": validation.probe.model_dump(mode="json"),
                            "warnings": [
                                {
                                    "code": warning.code,
                                    "message": warning.message,
                                    "timestamps": [
                                        str(value) for value in warning.timestamps
                                    ],
                                }
                                for warning in validation.warnings
                            ],
                            "crop_track_ids": list(validation.crop_track_ids),
                            "sampled_crop_timestamps": [
                                str(value)
                                for value in validation.sampled_crop_timestamps
                            ],
                            "subject_retention_ratio": str(
                                validation.subject_retention_ratio
                            ),
                            "unintended_black_bar_timestamps": [
                                str(value)
                                for value in validation.unintended_black_bar_timestamps
                            ],
                            "audio_boundary_timestamps": [
                                str(value)
                                for value in validation.audio_boundary_timestamps
                            ],
                            "audio_discontinuity_timestamps": [
                                str(value)
                                for value in validation.audio_discontinuity_timestamps
                            ],
                        },
                        "warnings": [warning.message for warning in command.warnings],
                    }
                )
            if self._reviewed_evidence()[2] != draft.provenance_digest:
                raise ValueError("current evidence changed after render")
            result = {
                "status": "completed",
                "draft_digest": approval.draft_digest,
                "approval_id": approval.approval_id,
                "outputs": published,
                "paid_dispatch_authorized": False,
                "final_edit_accepted": False,
                "validation_scope": "structural, crop and sampled-frame checks; not continuous audiovisual review",
            }
            _write_reviewed_json(output_root / "audit.json", result)
            return result
        except BaseException:
            _write_reviewed_json(
                output_root / "failure.json",
                {
                    "status": "failed",
                    "draft_digest": approval.draft_digest,
                    "published_outputs": published,
                    "paid_dispatch_authorized": False,
                    "final_edit_accepted": False,
                    "diagnostic": "local reviewed render failed; outputs and partials retained, no automatic retry",
                },
            )
            raise
