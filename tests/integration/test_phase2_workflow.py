"""Offline Phase 2 workflow contract using a fake analysis provider only."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from video_editor import workflow as workflow_module
from video_editor.analysis.models import (
    AnalysisRequestContext,
    BroadScanResponse,
    CandidateRefinementResponse,
    CandidateWindow,
    ProviderAttemptUsage,
    ProviderResult,
    ProviderUsage,
    UploadedFile,
)
from video_editor.analysis.proxy_chunks import AuthorizedUpload
from video_editor.analysis.ranking import RankingSettings
from video_editor.config import AppConfig, GeminiSettings, PathSettings
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.persistence.database import JobStore
from video_editor.workflow import PHASE2_STAGES, WorkflowService

D = Decimal
SECRET = "AIza-offline-secret-never-persisted"
_DEFAULT_CAP = Decimal("1.00")
_CATEGORIES = ("action", "scenic", "human", "story")


@dataclass
class FakeAnalysisProvider:
    """Offline provider returning strict, deterministic analysis responses."""

    omit_last_range: bool = False
    auth_failure: bool = False
    uploads: int = 0
    broad_calls: int = 0
    candidate_calls: int = 0
    deleted: list[str] = field(default_factory=list)
    prompt_versions: list[str] = field(default_factory=list)

    def upload(self, authorization: AuthorizedUpload) -> UploadedFile:
        self.uploads += 1
        authorization.close()
        return UploadedFile(
            name=f"files/{authorization.manifest_id}",
            uri="gs://offline/upload",
            mime_type="video/mp4",
            state="ACTIVE",
            manifest_id=authorization.manifest_id,
        )

    def broad_scan(
        self,
        upload: UploadedFile,
        chunk: Any,
        request: AnalysisRequestContext,
        *,
        prompt_version: str,
    ) -> ProviderResult[BroadScanResponse]:
        del upload
        self.broad_calls += 1
        self.prompt_versions.append(prompt_version)
        if self.auth_failure:
            raise VideoEditorError(
                ErrorCategory.PROVIDER,
                "provider rejected credentials",
                code="provider_auth",
            )
        if self.omit_last_range and self.broad_calls > 1:
            raise VideoEditorError(
                ErrorCategory.PROVIDER,
                "provider unavailable",
                code="provider_unavailable",
            )
        start = D(str(chunk.proxy_start))
        end = D(str(chunk.proxy_end))
        step = (end - start) / 4
        candidates = [
            {
                "candidate_id": f"{request.chunk_id}-{index}",
                "start": format(start + step * index, "f"),
                "end": format(start + step * (index + 1), "f"),
                "category": _CATEGORIES[index],
                "reason": "offline fixture moment",
                "confidence": 0.9,
            }
            for index in range(4)
        ]
        return ProviderResult(
            response=BroadScanResponse.model_validate(
                {
                    "schema_version": "broad-v1",
                    "chunk_id": request.chunk_id,
                    "scenes": [],
                    "speech_presence_ranges": [],
                    "candidates": candidates,
                }
            ),
            usage=_usage(request.reservation_id),
        )

    def refine_candidate(
        self,
        upload: UploadedFile,
        candidate: CandidateWindow,
        request: AnalysisRequestContext,
        fps: int,
        *,
        prompt_version: str,
    ) -> ProviderResult[CandidateRefinementResponse]:
        del upload, fps, prompt_version
        self.candidate_calls += 1
        return ProviderResult(
            response=CandidateRefinementResponse.model_validate(
                {
                    "schema_version": "candidate-v2",
                    "chunk_id": candidate.chunk_id,
                    "candidate_id": candidate.candidate_id,
                    "start": format(candidate.start, "f"),
                    "end": format(candidate.end, "f"),
                    "action_completeness": 0.9,
                    "visual_composition": 0.9,
                    "novelty": 0.9,
                    "semantic_importance": 0.9,
                    "duplicate_similarity": 0.05,
                    "vertical_subject_priority": "center",
                    "crop_intent": "follow subject",
                    "adjacent_scene_compatibility": 0.8,
                    "speech_meaning_summary": None,
                    "confidence": 0.9,
                    "subject_boxes": [
                        {
                            "time": format(candidate.start, "f"),
                            "x": float((40 + 40 * candidate.start) / 640) - 0.01,
                            "y": 0.37,
                            "w": 0.15,
                            "h": 0.26,
                            "priority": 0,
                        }
                    ],
                }
            ),
            usage=_usage(request.reservation_id),
        )

    def maximum_request_cost(
        self, manifest_id: str, chunk: Any, *, prompt_version: str
    ) -> Decimal:
        del manifest_id, chunk, prompt_version
        return D("0.0001")

    def delete_upload(self, upload: UploadedFile) -> None:
        self.deleted.append(upload.name)


def _usage(reservation_id: str) -> ProviderUsage:
    return ProviderUsage(
        reservation_id=reservation_id,
        attempts=(
            ProviderAttemptUsage(
                request_id=f"provider-{reservation_id}",
                status="succeeded",
                prompt_tokens=0,
                media_input_tokens=0,
                text_input_tokens=0,
                candidates_tokens=0,
                thoughts_tokens=0,
                output_tokens=0,
                total_tokens=0,
            ),
        ),
        request_ids=(f"provider-{reservation_id}",),
        prompt_tokens=0,
        media_input_tokens=0,
        text_input_tokens=0,
        candidates_tokens=0,
        thoughts_tokens=0,
        output_tokens=0,
        total_tokens=0,
        has_unknown_billing=False,
        actual_cost_usd=D(0),
    )


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
        raise RuntimeError(completed.stderr)
    return path


@dataclass
class Phase2Env:
    service: WorkflowService
    provider: FakeAnalysisProvider
    input_dir: Path
    config: AppConfig
    store: JobStore


def _config(tmp_path: Path, cost_cap: Decimal = _DEFAULT_CAP) -> AppConfig:
    return AppConfig(
        PathSettings(
            *(
                tmp_path / name
                for name in ("input", "workspace", "cache", "output", "state")
            )
        ),
        0,
        cloud_enabled=True,
        gemini=GeminiSettings(enabled=True, max_cost_per_source_hour_usd=cost_cap),
    )


@pytest.fixture
def phase2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    config = _config(tmp_path)
    input_dir = config.paths.input_dir
    _make_source(input_dir / "01.mp4", "navy")
    _make_source(input_dir / "02.mp4", "darkgreen")
    provider = FakeAnalysisProvider()
    with JobStore(config.paths.state_dir / "jobs.sqlite3") as store:
        yield Phase2Env(
            WorkflowService(config, store, provider=provider),
            provider,
            input_dir,
            config,
            store,
        )


def _artifacts(state: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    return [item for item in state.get("artifacts", []) if item.get("stage") == stage]


def _source_digests(input_dir: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(input_dir.glob("*.mp4"))
    }


def test_phase2_stage_order_and_complete_outputs(phase2: Phase2Env) -> None:
    before = _source_digests(phase2.input_dir)
    result = phase2.service.run(phase2.input_dir)
    state = phase2.service.status(result["job_id"])

    assert list(state["stages"]) == list(PHASE2_STAGES)
    assert list(PHASE2_STAGES) == [
        "inspect",
        "proxy",
        "segment",
        "analyze",
        "rank",
        "plan",
        "render",
        "validate",
        "report",
    ]
    names = [Path(item["output"]).name for item in result["outputs"]]
    assert names[0] == "long.mp4"
    assert names[1:] == [f"short-{index:02d}.mp4" for index in range(1, len(names))]
    assert state["status"] == "completed"
    assert _source_digests(phase2.input_dir) == before


def test_incomplete_broad_coverage_creates_no_plan_or_media(
    phase2: Phase2Env,
) -> None:
    phase2.provider.omit_last_range = True
    with pytest.raises(VideoEditorError) as caught:
        phase2.service.run(phase2.input_dir)

    assert caught.value.code == "analysis_incomplete"
    state = phase2.service.status(caught.value.safe_details["job_id"])
    assert state["stages"]["analyze"]["error"]["code"] == "analysis_incomplete"
    assert state["stages"]["analyze"]["error"]["missing"]
    assert not _artifacts(state, "plan")
    assert not _artifacts(state, "render")
    assert not list(phase2.config.paths.output_dir.rglob("*.mp4"))


def test_budget_exhaustion_creates_no_plan_or_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    config = _config(tmp_path, cost_cap=D("0"))
    _make_source(config.paths.input_dir / "01.mp4", "navy")
    provider = FakeAnalysisProvider()
    with JobStore(config.paths.state_dir / "jobs.sqlite3") as store:
        service = WorkflowService(config, store, provider=provider)
        with pytest.raises(VideoEditorError) as caught:
            service.run(config.paths.input_dir)
        assert caught.value.category == ErrorCategory.BUDGET
        assert caught.value.code == "budget_exhausted"
        state = service.status(caught.value.safe_details["job_id"])
        assert state["stages"]["analyze"]["error"]["code"] == "budget_exhausted"
        assert provider.uploads == 0
        assert not _artifacts(state, "plan")
        assert not _artifacts(state, "render")


def test_provider_auth_failure_is_redacted_and_key_never_persisted(
    phase2: Phase2Env,
) -> None:
    phase2.provider.auth_failure = True
    with pytest.raises(VideoEditorError) as caught:
        phase2.service.run(phase2.input_dir)

    assert phase2.provider.broad_calls >= 1
    assert caught.value.code == "analysis_incomplete"
    job_id = caught.value.safe_details["job_id"]
    state = phase2.service.status(job_id)
    assert state["stages"]["analyze"]["error"]["code"] == "analysis_incomplete"
    assert SECRET not in json.dumps(state, default=str)
    assert SECRET not in str(caught.value)
    database = phase2.config.paths.state_dir / "jobs.sqlite3"
    assert SECRET.encode() not in database.read_bytes()


def test_report_is_safe_and_records_authoritative_final_status(
    phase2: Phase2Env,
) -> None:
    result = phase2.service.run(phase2.input_dir)
    report_json = next(Path(p) for p in result["reports"] if p.endswith(".json"))
    report = json.loads(report_json.read_text())
    text = report_json.read_text() + Path(result["reports"][1]).read_text()

    assert report["status"] == "completed"
    assert report["snapshot_status"] == "final"
    assert report["analysis"]["provider"] == "gemini"
    assert report["analysis"]["model"] == "gemini-2.5-flash"
    assert report["analysis"]["coverage"]["complete"] is True
    assert report["analysis"]["manifests"]
    assert all("source_path" not in item for item in report["analysis"]["manifests"])
    assert report["ai_quality_disclaimer"]
    assert SECRET not in text
    assert "GEMINI_API_KEY" not in text
    assert str(phase2.config.paths.cache_dir) not in json.dumps(
        report["analysis"]["manifests"]
    )


def test_resume_reuses_uploads_and_results(phase2: Phase2Env) -> None:
    result = phase2.service.run(phase2.input_dir)
    uploads = phase2.provider.uploads
    broad = phase2.provider.broad_calls
    candidate = phase2.provider.candidate_calls

    resumed = phase2.service.resume(result["job_id"])

    assert resumed["outputs"] == result["outputs"]
    assert phase2.provider.uploads == uploads
    assert phase2.provider.broad_calls == broad
    assert phase2.provider.candidate_calls == candidate


def test_changed_prompt_invalidates_analyze_onward_only(
    phase2: Phase2Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = phase2.service.run(phase2.input_dir)
    before = phase2.service.status(result["job_id"])["stages"]

    monkeypatch.setattr(workflow_module, "BROAD_PROMPT_VERSION", "broad-v1-test")
    phase2.service.resume(result["job_id"])
    after = phase2.service.status(result["job_id"])["stages"]

    for name in ("inspect", "proxy", "segment"):
        assert after[name]["started_at"] == before[name]["started_at"]
    for name in ("analyze", "rank", "plan"):
        assert after[name]["started_at"] != before[name]["started_at"]
    assert phase2.provider.prompt_versions[-1] == "broad-v1-test"


def test_changed_ranking_settings_invalidate_rank_onward_only(
    phase2: Phase2Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = phase2.service.run(phase2.input_dir)
    before = phase2.service.status(result["job_id"])["stages"]
    calls = phase2.provider.broad_calls

    monkeypatch.setattr(
        workflow_module,
        "RANKING_SETTINGS",
        RankingSettings(selection_threshold=D("0.40")),
    )
    phase2.service.resume(result["job_id"])
    after = phase2.service.status(result["job_id"])["stages"]

    for name in ("inspect", "proxy", "segment", "analyze"):
        assert after[name]["started_at"] == before[name]["started_at"]
    assert after["rank"]["started_at"] != before["rank"]["started_at"]
    assert phase2.provider.broad_calls == calls


def test_missing_short_rerenders_only_that_output(phase2: Phase2Env) -> None:
    result = phase2.service.run(phase2.input_dir)
    outputs = {
        Path(item["output"]).name: Path(item["output"]) for item in result["outputs"]
    }
    assert "short-01.mp4" in outputs
    long_mtime = outputs["long.mp4"].stat().st_mtime_ns
    outputs["short-01.mp4"].unlink()

    resumed = phase2.service.resume(result["job_id"])

    assert outputs["short-01.mp4"].is_file()
    assert outputs["long.mp4"].stat().st_mtime_ns == long_mtime
    assert [Path(item["output"]).name for item in resumed["outputs"]] == list(outputs)


def test_changed_short_plan_rerenders_only_that_output_and_drops_orphans(
    phase2: Phase2Env,
) -> None:
    result = phase2.service.run(phase2.input_dir)
    job_id = result["job_id"]
    outputs = {Path(item["output"]).name: item for item in result["outputs"]}
    short = Path(outputs["short-01.mp4"]["output"])
    long = Path(outputs["long.mp4"]["output"])
    short_mtime = short.stat().st_mtime_ns
    long_mtime = long.stat().st_mtime_ns
    plan_path = Path(outputs["short-01.mp4"]["plan"])
    plan = json.loads(plan_path.read_text())
    plan["output"]["theme_summary"] = "edited theme, same clips and duration"
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    orphan = long.parent / "short-09.mp4"
    orphan.write_bytes(b"stale output from an older plan set")
    phase2.store.save_artifact(
        job_id, "render", orphan, {"plan": str(plan_path), "plan_digest": "old"}
    )

    phase2.service.resume(job_id)

    assert short.stat().st_mtime_ns != short_mtime
    assert long.stat().st_mtime_ns == long_mtime
    state = phase2.service.status(job_id)
    render_paths = {Path(item["path"]).name for item in _artifacts(state, "render")}
    assert "short-09.mp4" not in render_paths
    assert orphan.is_file()


def test_reports_never_contain_generated_or_upload_paths(phase2: Phase2Env) -> None:
    result = phase2.service.run(phase2.input_dir)
    job_id = result["job_id"]
    texts = [Path(path).read_text() for path in result["reports"]]
    manifests = phase2.store.get_job(job_id)["proxy_manifests"]
    assert manifests
    forbidden = {
        str(phase2.config.paths.cache_dir / job_id),
        str(phase2.config.paths.workspace_dir / job_id),
    }
    for item in manifests:
        forbidden.update(
            str(item["data"][key]) for key in ("artifact_path", "generated_root")
        )
    for text in texts:
        for value in forbidden:
            assert value not in text
        assert "proxy_manifests" not in text
        assert "analysis_results" not in text
