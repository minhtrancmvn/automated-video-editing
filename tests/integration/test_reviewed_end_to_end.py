"""Offline reviewed workflow integration test contract.

Validates explicit offline ReviewedWorkflowService and CLI prepare-reviewed /
render-reviewed subprocess execution with disposable SQLite evidence, real
generated media, immutable source/db digests, and stale evidence refusal.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Generator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.integration.test_phase2_workflow import FakeAnalysisProvider
from video_editor import workflow as workflow_module
from video_editor.analysis.evaluation import (
    Assessment,
    Claim,
    EditorialDecision,
    EditorialResult,
    EvaluationApproval,
    EvaluationBundle,
    EvaluationInput,
    EvaluationPlan,
    GroundingResult,
    Observation,
    run_evaluation,
)
from video_editor.analysis.evaluation_provider import (
    estimate_editorial_cost,
    estimate_grounding_cost,
)
from video_editor.analysis.models import (
    ProviderAttemptUsage,
    ProviderResult,
    ProviderUsage,
    ProxyManifestData,
    UploadedFile,
)
from video_editor.analysis.pricing import pricing_for_model
from video_editor.analysis.selection import (
    RefinementApproval,
    SelectionProposal,
    SelectionReview,
    evaluate_selection,
    file_digest,
    selection_digest,
)
from video_editor.config import AppConfig, GeminiSettings, PathSettings
from video_editor.evaluated_workflow import (
    ReviewedDraft,
    ReviewedRenderApproval,
    ReviewedWorkflowService,
    reviewed_draft_digest,
)
from video_editor.persistence.database import JobStore
from video_editor.workflow import WorkflowService

_BOOTSTRAP_SCRIPT = """
import os
import socket
import sys

def _audit_hook(event, args):
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname"):
        raise PermissionError(f"Network access denied in test subprocess: {event}")
sys.addaudithook(_audit_hook)

try:
    import google.auth
    def _deny_credentials(*args, **kwargs):
        raise PermissionError("Credential access denied in test subprocess")
    google.auth.default = _deny_credentials
except ImportError:
    pass

os.environ.pop("GEMINI_API_KEY", None)

from video_editor.analysis.gemini import GeminiAdapter
from video_editor.persistence.database import JobStore

def _forbid_provider_or_writable_store(*args, **kwargs):
    raise PermissionError("Provider construction or writable store forbidden")

GeminiAdapter.from_environment = _forbid_provider_or_writable_store
JobStore.__enter__ = _forbid_provider_or_writable_store

def _deny_keychain(event, args):
    if event == "subprocess.Popen" and os.path.basename(str(args[0])) == "security":
        raise PermissionError("Keychain access forbidden")
sys.addaudithook(_deny_keychain)

from video_editor.cli import app
sys.exit(app())
"""


@dataclass(frozen=True)
class ReviewedEnvironment:
    """Disposable offline test environment with registered review evidence."""

    config: AppConfig
    config_path: Path
    bundle_path: Path
    bundle: EvaluationBundle
    store_path: Path
    input_dir: Path
    output_dir: Path
    job_id: str
    source_digests: dict[str, str]
    db_digest: str


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_digests(input_dir: Path) -> dict[str, str]:
    return {path.name: _file_sha256(path) for path in sorted(input_dir.glob("*.mp4"))}


def _make_source(path: Path, color: str, seconds: int = 8) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=640x360:r=30:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=s=80x80:r=30:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-filter_complex",
            "[0:v][1:v]overlay=x='40+t*40':y=140[v]",
            "-map",
            "[v]",
            "-map",
            "2:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {completed.stderr}")
    return path


def run_cli_subprocess(
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute CLI subprocess with strict network audit hook and purged credentials."""
    env = os.environ.copy()
    env.pop("GEMINI_API_KEY", None)
    existing_pp = env.get("PYTHONPATH", "")
    src_path = str(_REPO_ROOT / "src")
    env["PYTHONPATH"] = f"{src_path}:{existing_pp}" if existing_pp else src_path
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", _BOOTSTRAP_SCRIPT, *args],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        env=env,
    )


class _FakeEvaluationProvider:
    """Deterministic offline evaluation provider generating grounded editorial decisions."""

    def __init__(self) -> None:
        self.uploads: list[str] = []

    def upload(self, authorization: Any) -> UploadedFile:
        self.uploads.append(authorization.manifest_id)
        authorization.close()
        return UploadedFile(
            name=f"files/{authorization.manifest_id}",
            uri="gs://offline/test-upload",
            mime_type="video/mp4",
            state="ACTIVE",
            manifest_id=authorization.manifest_id,
        )

    def delete_upload(self, upload: UploadedFile) -> None:
        del upload

    def ground(
        self,
        upload: UploadedFile,
        *,
        request_id: str,
        proposal: SelectionProposal,
    ) -> ProviderResult[GroundingResult]:
        del upload
        result = GroundingResult(
            proposal_digest=selection_digest(proposal),
            observations=(
                Observation(
                    observation_id="obs-1",
                    start=cast(Decimal, format(proposal.broad.start, "f")),
                    end=cast(Decimal, format(proposal.broad.end, "f")),
                    modality="visual",
                    description="Synthetic offline video observation.",
                ),
            ),
            assessments=tuple(
                Assessment(
                    field=field_name,
                    text=getattr(proposal.broad, field_name),
                    claims=(
                        Claim(
                            text="Synthetic grounding claim.",
                            support="supported",
                            observation_ids=("obs-1",),
                        ),
                    ),
                )
                for field_name in ("category", "reason")
            ),
        )
        usage = ProviderUsage(
            reservation_id=request_id,
            attempts=(
                ProviderAttemptUsage(
                    request_id=f"attempt-{request_id}",
                    status="succeeded",
                    prompt_tokens=10,
                    media_input_tokens=10,
                    text_input_tokens=0,
                    candidates_tokens=10,
                    thoughts_tokens=0,
                    output_tokens=10,
                    total_tokens=40,
                ),
            ),
            request_ids=(f"attempt-{request_id}",),
            prompt_tokens=10,
            media_input_tokens=10,
            text_input_tokens=0,
            candidates_tokens=10,
            thoughts_tokens=0,
            output_tokens=10,
            total_tokens=40,
            has_unknown_billing=False,
            actual_cost_usd=Decimal("0.0001"),
        )
        return ProviderResult(response=result, usage=usage)

    def compare(
        self,
        *,
        request_id: str,
        set_digest: str,
        groundings: tuple[GroundingResult, ...],
    ) -> ProviderResult[EditorialResult]:
        result = EditorialResult(
            set_digest=set_digest,
            decisions=tuple(
                EditorialDecision(
                    proposal_digest=g.proposal_digest,
                    disposition="primary",
                    reason="Synthetic editorial preference.",
                    observation_refs=(f"{g.proposal_digest}:obs-1",),
                    basis="incident_value",
                )
                for g in groundings
            ),
        )
        usage = ProviderUsage(
            reservation_id=request_id,
            attempts=(
                ProviderAttemptUsage(
                    request_id=f"attempt-{request_id}",
                    status="succeeded",
                    prompt_tokens=10,
                    media_input_tokens=10,
                    text_input_tokens=0,
                    candidates_tokens=10,
                    thoughts_tokens=0,
                    output_tokens=10,
                    total_tokens=40,
                ),
            ),
            request_ids=(f"attempt-{request_id}",),
            prompt_tokens=10,
            media_input_tokens=10,
            text_input_tokens=0,
            candidates_tokens=10,
            thoughts_tokens=0,
            output_tokens=10,
            total_tokens=40,
            has_unknown_billing=False,
            actual_cost_usd=Decimal("0.0001"),
        )
        return ProviderResult(response=result, usage=usage)


@pytest.fixture
def reviewed_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[ReviewedEnvironment, None, None]:
    """Build a complete offline SQLite database, real proxies, and verified evaluation bundle."""
    paths = PathSettings(
        *(
            tmp_path / name
            for name in ("input", "workspace", "cache", "output", "state")
        )
    )
    for directory in (
        paths.input_dir,
        paths.workspace_dir,
        paths.cache_dir,
        paths.output_dir,
        paths.state_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    _make_source(paths.input_dir / "01.mp4", "navy", seconds=8)
    _make_source(paths.input_dir / "02.mp4", "darkgreen", seconds=8)

    config = AppConfig(
        paths,
        0,
        cloud_enabled=True,
        gemini=GeminiSettings(
            enabled=True,
            max_cost_per_source_hour_usd=Decimal("3.00"),
        ),
    )

    config_path = tmp_path / "reviewed_test_config.toml"
    config_path.write_text(
        f"""[paths]
input_dir = "{paths.input_dir}"
workspace_dir = "{paths.workspace_dir}"
cache_dir = "{paths.cache_dir}"
output_dir = "{paths.output_dir}"
state_dir = "{paths.state_dir}"

[settings]
storage_reserve_bytes = 0
cloud_enabled = true
render_concurrency = 1

[gemini]
enabled = true
max_cost_per_source_hour_usd = "3.00"
"""
    )

    evidence_bin = tmp_path / "synthetic_selection_evidence.bin"
    evidence_bin.write_bytes(b"synthetic offline test evidence")
    proposals_captured: list[SelectionProposal] = []

    def synthetic_evaluate(
        proposals: tuple[SelectionProposal, ...],
        reviews: tuple[SelectionReview, ...],
    ) -> Any:
        del reviews
        proposals_captured.extend(proposals)
        synthetic_reviews = tuple(
            SelectionReview.model_validate(
                {
                    "proposal_digest": selection_digest(p),
                    "evaluator": "synthetic-offline-test",
                    "evaluator_version": "1",
                    "observations": ["Synthetic test assertion; offline evidence."],
                    "factual_support": "supported",
                    "editorial": "primary",
                    "reason": "Synthetic admission for reviewed workflow fixture.",
                    "evidence": [
                        {
                            "path": evidence_bin,
                            "sha256": file_digest(evidence_bin),
                            "scope": "video",
                            "source_start": p.source_start,
                            "source_end": p.source_end,
                        }
                    ],
                }
            )
            for p in proposals
        )
        if proposals:
            service.refinement_approval = RefinementApproval(
                approval_id="synthetic-reviewed-approval",
                job_id=proposals[0].job_id,
                proposal_digests=tuple(selection_digest(p) for p in proposals),
                maximum_cost_usd=Decimal(100),
                maximum_requests=len(proposals),
            )
        return evaluate_selection(proposals, synthetic_reviews)

    monkeypatch.setattr(workflow_module, "evaluate_selection", synthetic_evaluate)

    db_path = paths.state_dir / "jobs.sqlite3"
    with JobStore(db_path) as store:
        service = WorkflowService(config, store, provider=FakeAnalysisProvider())
        job_id = service._new_job(paths.input_dir)
        service._ensure_inspect(job_id, paths.input_dir)
        proxied = service._ensure_proxy(job_id)
        segmented = service._ensure_segment(job_id, proxied)
        service._ensure_analyze(job_id, segmented)

        store.connection.execute(
            "UPDATE budget_accounts SET limit_microusd = ? WHERE job_id = ?",
            (100_000_000, job_id),
        )
        store.connection.commit()

        manifest_rows = store.connection.execute(
            "SELECT manifest_id, data_json FROM proxy_manifests WHERE job_id = ?",
            (job_id,),
        ).fetchall()
        manifests_by_chunk = {}
        for row in manifest_rows:
            data = json.loads(row[1])
            manifests_by_chunk[data["chunk_id"]] = (
                row[0],
                ProxyManifestData.model_validate(data),
            )

        inputs = []
        for prop in proposals_captured[:2]:
            manifest_id, manifest_data = manifests_by_chunk[prop.chunk_id]
            inputs.append(
                EvaluationInput(
                    proposal=prop,
                    manifest_id=manifest_id,
                    manifest=manifest_data,
                )
            )

        pricing = pricing_for_model(config.gemini.model)
        v_costs = tuple(
            estimate_grounding_cost(inp.proposal, pricing) for inp in inputs
        )
        e_cost = estimate_editorial_cost(pricing)
        plan = EvaluationPlan(
            model="gemini-3.8-flash",
            inputs=tuple(inputs),
            video_costs=v_costs,
            editorial_cost=e_cost,
        )
        approval = EvaluationApproval(
            approval_id="synthetic-eval-approval-reviewed",
            plan_digest=selection_digest(plan),
            maximum_generations=plan.maximum_generations,
            maximum_cost_usd=plan.maximum_cost_usd,
        )

        bundle = run_evaluation(
            plan=plan,
            approval=approval,
            provider_factory=_FakeEvaluationProvider,
            store=store,
            paths=paths,
        )
        assert bundle.complete

    bundle_path = tmp_path / "evaluation_bundle.json"
    bundle_path.write_text(bundle.model_dump_json(indent=2))

    yield ReviewedEnvironment(
        config=config,
        config_path=config_path,
        bundle_path=bundle_path,
        bundle=bundle,
        store_path=db_path,
        input_dir=paths.input_dir,
        output_dir=paths.output_dir,
        job_id=job_id,
        source_digests=_source_digests(paths.input_dir),
        db_digest=_file_sha256(db_path),
    )


def test_reviewed_service_direct_lifecycle(
    reviewed_env: ReviewedEnvironment,
    tmp_path: Path,
) -> None:
    """Verify ReviewedWorkflowService programmatic API: prepare draft and render with approval."""
    with JobStore(reviewed_env.store_path) as store:
        service = ReviewedWorkflowService(
            reviewed_env.config,
            store,
            evaluation_bundle=reviewed_env.bundle,
        )
        draft = service.prepare_reviewed_plans()
        assert isinstance(draft, ReviewedDraft)
        draft_digest = reviewed_draft_digest(draft)
        assert isinstance(draft_digest, str)
        assert len(draft_digest) == 64

        approval = ReviewedRenderApproval(
            approval_id="test-approval-service",
            draft_digest=draft_digest,
        )
        output_root = tmp_path / "service_render_output"
        result = service.render_reviewed_plans(draft, approval, output_root)
        assert isinstance(result, dict)
        long_output = output_root / "long.mp4"
        assert long_output.is_file()
        assert long_output.stat().st_size > 0

    assert _source_digests(reviewed_env.input_dir) == reviewed_env.source_digests
    assert _file_sha256(reviewed_env.store_path) == reviewed_env.db_digest


def test_reviewed_cli_full_prepare_and_render_lifecycle(
    reviewed_env: ReviewedEnvironment,
    tmp_path: Path,
) -> None:
    """Verify end-to-end prepare-reviewed and render-reviewed CLI subprocess calls."""
    draft_path = tmp_path / "reviewed_draft.json"
    proc_prepare = run_cli_subprocess(
        [
            "prepare-reviewed",
            str(reviewed_env.bundle_path),
            "--config",
            str(reviewed_env.config_path),
            "--output",
            str(draft_path),
        ]
    )
    assert proc_prepare.returncode == 0, proc_prepare.stderr
    prepare_payload = json.loads(proc_prepare.stdout)
    assert prepare_payload["draft"] == str(draft_path)
    assert prepare_payload["render_authorized"] is False
    assert prepare_payload["paid_dispatch_authorized"] is False
    draft_digest = prepare_payload["draft_digest"]
    assert isinstance(draft_digest, str) and len(draft_digest) == 64
    assert draft_path.is_file()

    approval_path = tmp_path / "approval.json"
    approval = ReviewedRenderApproval(
        approval_id="cli-approved-attempt",
        draft_digest=draft_digest,
    )
    approval_path.write_text(approval.model_dump_json(indent=2))

    render_out_dir = tmp_path / "cli_rendered_output"
    proc_render = run_cli_subprocess(
        [
            "render-reviewed",
            str(draft_path),
            "--evaluation-bundle",
            str(reviewed_env.bundle_path),
            "--approval",
            str(approval_path),
            "--config",
            str(reviewed_env.config_path),
            "--output",
            str(render_out_dir),
        ]
    )
    assert proc_render.returncode == 0, proc_render.stderr
    render_payload = json.loads(proc_render.stdout)
    assert isinstance(render_payload, dict)

    long_video = render_out_dir / "long.mp4"
    assert long_video.is_file()
    assert long_video.stat().st_size > 0

    assert _source_digests(reviewed_env.input_dir) == reviewed_env.source_digests
    assert _file_sha256(reviewed_env.store_path) == reviewed_env.db_digest


def test_reviewed_cli_refuses_when_output_draft_already_exists(
    reviewed_env: ReviewedEnvironment,
    tmp_path: Path,
) -> None:
    """CLI prepare-reviewed must refuse when target draft file already exists."""
    draft_path = tmp_path / "preexisting_draft.json"
    draft_path.write_text("existing content")
    proc = run_cli_subprocess(
        [
            "prepare-reviewed",
            str(reviewed_env.bundle_path),
            "--config",
            str(reviewed_env.config_path),
            "--output",
            str(draft_path),
        ]
    )
    assert proc.returncode != 0
    assert draft_path.read_text() == "existing content"
    assert _file_sha256(reviewed_env.store_path) == reviewed_env.db_digest


def test_reviewed_cli_refuses_missing_or_mismatched_render_approval(
    reviewed_env: ReviewedEnvironment,
    tmp_path: Path,
) -> None:
    """CLI render-reviewed must refuse missing approval or mismatched draft digest."""
    draft_path = tmp_path / "reviewed_draft.json"
    prepare_res = run_cli_subprocess(
        [
            "prepare-reviewed",
            str(reviewed_env.bundle_path),
            "--config",
            str(reviewed_env.config_path),
            "--output",
            str(draft_path),
        ]
    )
    assert prepare_res.returncode == 0, prepare_res.stderr

    render_out_dir = tmp_path / "unapproved_output"
    missing_approval_proc = run_cli_subprocess(
        [
            "render-reviewed",
            str(draft_path),
            "--evaluation-bundle",
            str(reviewed_env.bundle_path),
            "--config",
            str(reviewed_env.config_path),
            "--output",
            str(render_out_dir),
        ]
    )
    assert missing_approval_proc.returncode != 0

    mismatched_approval_path = tmp_path / "bad_approval.json"
    bad_approval = ReviewedRenderApproval(
        approval_id="tampered-approval",
        draft_digest="0" * 64,
    )
    mismatched_approval_path.write_text(bad_approval.model_dump_json())

    mismatched_proc = run_cli_subprocess(
        [
            "render-reviewed",
            str(draft_path),
            "--evaluation-bundle",
            str(reviewed_env.bundle_path),
            "--approval",
            str(mismatched_approval_path),
            "--config",
            str(reviewed_env.config_path),
            "--output",
            str(render_out_dir),
        ]
    )
    assert mismatched_proc.returncode != 0
    assert not (render_out_dir / "long.mp4").exists()
    assert _file_sha256(reviewed_env.store_path) == reviewed_env.db_digest


def test_reviewed_cli_refuses_stale_or_tampered_bundle(
    reviewed_env: ReviewedEnvironment,
    tmp_path: Path,
) -> None:
    """CLI prepare-reviewed must refuse tampered evaluation bundles or altered proxy media."""
    tampered_bundle_path = tmp_path / "tampered_bundle.json"
    bundle_data = json.loads(reviewed_env.bundle_path.read_text())
    bundle_data["audit_digest"] = "f" * 64
    tampered_bundle_path.write_text(json.dumps(bundle_data))

    proc_tampered_bundle = run_cli_subprocess(
        [
            "prepare-reviewed",
            str(tampered_bundle_path),
            "--config",
            str(reviewed_env.config_path),
            "--output",
            str(tmp_path / "refused_draft_1.json"),
        ]
    )
    assert proc_tampered_bundle.returncode != 0

    # Corrupt proxy media file on disk
    target_proxy = Path(reviewed_env.bundle.audit.plan.inputs[0].manifest.artifact_path)
    assert target_proxy.is_file()
    original_bytes = target_proxy.read_bytes()
    try:
        target_proxy.write_bytes(original_bytes + b"corrupt")
        proc_tampered_proxy = run_cli_subprocess(
            [
                "prepare-reviewed",
                str(reviewed_env.bundle_path),
                "--config",
                str(reviewed_env.config_path),
                "--output",
                str(tmp_path / "refused_draft_2.json"),
            ]
        )
        assert proc_tampered_proxy.returncode != 0
    finally:
        target_proxy.write_bytes(original_bytes)


def test_reviewed_cli_audit_socket_guard_traps_network_attempts() -> None:
    """Audit socket hook in CLI subprocess must intercept and fail network access."""
    test_network_code = """
import socket
s = socket.socket()
s.connect(("127.0.0.1", 80))
"""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _BOOTSTRAP_SCRIPT.replace(
                "from video_editor.cli import app\nsys.exit(app())", test_network_code
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "Network access denied in test subprocess" in proc.stderr
