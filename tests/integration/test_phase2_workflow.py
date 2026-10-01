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
from video_editor.media.storage import inspect_volume
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
    invalid_broad: bool = False
    timeout_broad: bool = False
    multiple_shorts: bool = False
    unknown_billing: bool = False
    uploads: int = 0
    broad_calls: int = 0
    candidate_calls: int = 0
    upload_attempts: int = 0
    refinement_candidate_ids: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    prompt_versions: list[str] = field(default_factory=list)

    def upload(self, authorization: AuthorizedUpload) -> UploadedFile:
        self.uploads += 1
        self.upload_attempts += 1
        authorization.close()
        return UploadedFile(
            name=f"files/{authorization.manifest_id}",
            uri="gs://offline/upload",
            mime_type="video/mp4",
            state="ACTIVE",
            manifest_id=authorization.manifest_id,
        )

    def _fixture_json(self, name: str) -> dict[str, Any]:
        payload = json.loads(
            (Path(__file__).parents[1] / "fixtures" / "gemini" / name).read_text()
        )
        assert isinstance(payload, dict)
        return payload

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
        if self.timeout_broad:
            raise VideoEditorError(
                ErrorCategory.PROVIDER,
                "provider timed out",
                code="provider_timeout",
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
        if self.invalid_broad:
            return ProviderResult(
                response=BroadScanResponse.model_validate(
                    {
                        "schema_version": "broad-v1",
                        "chunk_id": f"{request.chunk_id}-unexpected",
                        "scenes": [],
                        "speech_presence_ranges": [],
                        "candidates": [],
                    }
                ),
                usage=_usage(request.reservation_id),
            )
        if self.multiple_shorts:
            fixture = self._fixture_json("multi-short-broad.json")
            candidates = [
                {
                    "candidate_id": f"{request.chunk_id}-{index}",
                    "start": format(start + step * index, "f"),
                    "end": format(start + step * (index + 1), "f"),
                    "category": str(item["category"]),
                    "reason": str(item["reason"]),
                    "confidence": float(item["confidence"]),
                }
                for index, item in enumerate(fixture["candidates"])
            ]
            scenes = [
                {
                    **scene,
                    "scene_id": f"{request.chunk_id}-{scene['scene_id']}",
                    "start": format(start + step * D(str(scene["start"])), "f"),
                    "end": format(start + step * D(str(scene["end"])), "f"),
                }
                for scene in fixture["scenes"]
            ]
        else:
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
            scenes = []
        return ProviderResult(
            response=BroadScanResponse.model_validate(
                {
                    "schema_version": "broad-v1",
                    "chunk_id": request.chunk_id,
                    "scenes": scenes,
                    "speech_presence_ranges": [],
                    "candidates": candidates,
                }
            ),
            usage=(
                _unknown_usage(request.reservation_id)
                if self.unknown_billing
                else _usage(request.reservation_id)
            ),
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
        self.refinement_candidate_ids.append(candidate.candidate_id)
        fixture = (
            self._fixture_json("multi-short-refinements.json")
            if self.multiple_shorts
            else {}
        )
        return ProviderResult(
            response=CandidateRefinementResponse.model_validate(
                {
                    **fixture,
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


def _unknown_usage(reservation_id: str) -> ProviderUsage:
    usage = _usage(reservation_id)
    return usage.model_copy(
        update={
            "attempts": (
                ProviderAttemptUsage(
                    request_id=f"provider-{reservation_id}",
                    status="failed_unknown_billing",
                ),
            ),
            "has_unknown_billing": True,
            "actual_cost_usd": None,
        }
    )


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


def test_multiple_short_fixtures_create_distinct_bounded_outputs(
    phase2: Phase2Env,
) -> None:
    phase2.provider.multiple_shorts = True

    result = phase2.service.run(phase2.input_dir)

    names = [Path(item["output"]).name for item in result["outputs"]]
    assert names[:3] == ["long.mp4", "short-01.mp4", "short-02.mp4"]
    assert len(names) == 5
    assert len(set(names)) == len(names)
    assert names[1:] == [f"short-{index:02d}.mp4" for index in range(1, len(names))]
    assert (
        len(phase2.provider.refinement_candidate_ids) == phase2.provider.candidate_calls
    )
    assert len(set(phase2.provider.refinement_candidate_ids)) == len(
        phase2.provider.refinement_candidate_ids
    )
    state = phase2.service.status(result["job_id"])
    report_path = next(
        Path(path) for path in result["reports"] if path.endswith(".json")
    )
    report = json.loads(report_path.read_text())
    assert report["analysis"]["coverage"]["complete"] is True
    assert report["analysis"]["candidate_count"] == phase2.provider.candidate_calls
    artifacts = _artifacts(state, "plan")
    plans = [json.loads(Path(item["path"]).read_text()) for item in artifacts]
    assert all(plan["schema_version"] == 2 for plan in plans)
    assert all(len(plan["clips"]) >= 2 for plan in plans[1:])
    assert all(
        plan["output"]["filename"] == name
        for plan, name in zip(plans, names, strict=True)
    )
    assert all(plan["output"]["audio"] == "source" for plan in plans)
    short_plans = plans[1:]
    for first_index, first_plan in enumerate(short_plans):
        for second_plan in short_plans[first_index + 1 :]:
            for first_clip in first_plan["clips"]:
                for second_clip in second_plan["clips"]:
                    assert first_clip["dedup_group"] != second_clip["dedup_group"]
                    if first_clip["source_identity"] != second_clip["source_identity"]:
                        continue
                    overlap = max(
                        Decimal(0),
                        min(
                            Decimal(first_clip["source_end"]),
                            Decimal(second_clip["source_end"]),
                        )
                        - max(
                            Decimal(first_clip["source_start"]),
                            Decimal(second_clip["source_start"]),
                        ),
                    )
                    shorter_interval = min(
                        Decimal(first_clip["source_end"])
                        - Decimal(first_clip["source_start"]),
                        Decimal(second_clip["source_end"])
                        - Decimal(second_clip["source_start"]),
                    )
                    assert overlap / shorter_interval <= Decimal("0.10")
    assert Decimal(
        str(
            sum(
                Decimal(clip["source_end"]) - Decimal(clip["source_start"])
                for clip in plans[0]["clips"]
            )
        )
    ) <= Decimal(1800)
    for plan in plans[1:]:
        assert Decimal(
            str(
                sum(
                    Decimal(clip["source_end"]) - Decimal(clip["source_start"])
                    for clip in plan["clips"]
                )
            )
        ) <= Decimal(180)
        clips = plan["clips"]
        assert clips == sorted(
            clips,
            key=lambda clip: (
                next(
                    index
                    for index, source in enumerate(plan["sources"])
                    if source["id"] == clip["source_id"]
                ),
                Decimal(clip["source_start"]),
            ),
        )
        assert len({clip["dedup_group"] for clip in clips}) == len(clips)
        for transition in plan["transitions"]:
            allowed = {
                "cut": {"continuous_action", "matched_motion", "same_event"},
                "dissolve": {"same_event", "same_place_time_shift"},
                "fade": {"chapter_boundary", "story_open_close"},
                "fade_black": {"chapter_boundary", "time_jump", "location_change"},
            }
            assert transition["relation"] in allowed[transition["kind"]]
    assert all(Path(item["output"]).is_file() for item in result["outputs"])
    report_outputs = report["output"]["outputs"]
    assert [item["plan_id"] for item in report_outputs] == [
        plan["output"]["plan_id"] for plan in plans
    ]
    assert [item["path"] for item in report_outputs] == [
        item["output"] for item in result["outputs"]
    ]
    assert all(item["crop_track_ids"] for item in report_outputs[1:])
    broad_fixture = phase2.provider._fixture_json("multi-short-broad.json")
    refinement_fixture = phase2.provider._fixture_json("multi-short-refinements.json")
    assert all(
        "speech_meaning_summary" not in item for item in broad_fixture["candidates"]
    )
    assert refinement_fixture["speech_meaning_summary"] is None
    assert len(set(phase2.provider.refinement_candidate_ids)) == len(
        phase2.provider.refinement_candidate_ids
    )
    assert all(
        candidate_id.startswith("analysis-chunk-")
        for candidate_id in phase2.provider.refinement_candidate_ids
    )


def test_phase2_missing_output_volume_fails_and_resumes_completed_stages(
    phase2: Phase2Env,
) -> None:
    source_before = _source_digests(phase2.input_dir)
    job_id = phase2.service._new_job(phase2.input_dir)
    phase2.service._ensure_inspect(job_id, phase2.input_dir)
    proxied = phase2.service._ensure_proxy(job_id)
    segmented = phase2.service._ensure_segment(job_id, proxied)
    analyzed = phase2.service._ensure_analyze(job_id, segmented)
    ranked = phase2.service._ensure_rank(job_id, segmented, analyzed)
    planned = phase2.service._ensure_highlight_plan(job_id, proxied, analyzed, ranked)
    before_failure = phase2.service.status(job_id)
    completed_stage_times = {
        name: before_failure["stages"][name]["completed_at"]
        for name in ("inspect", "proxy", "segment", "analyze", "rank", "plan")
    }
    provider_calls = (
        phase2.provider.uploads,
        phase2.provider.broad_calls,
        phase2.provider.candidate_calls,
    )
    configured_reserve = phase2.config.storage_reserve_bytes
    job = phase2.store.get_job(job_id)
    missing_mount = Path("/Volumes") / f"phase2-unmounted-{job_id}"
    recorded = job["config"]["destination_volumes"]["output"]
    recorded["mount_point"] = str(missing_mount)
    phase2.store.connection.execute(
        "UPDATE jobs SET config_json = ? WHERE id = ?",
        (json.dumps(job["config"]), job_id),
    )
    phase2.store.connection.commit()

    with pytest.raises(VideoEditorError) as caught:
        phase2.service._ensure_render_v2(job_id, planned)

    assert caught.value.category == ErrorCategory.STORAGE
    assert not missing_mount.exists()
    failed = phase2.service.status(job_id)
    assert failed["status"] == "failed"
    assert failed["stages"]["render"]["status"] == "failed"
    assert all(
        failed["stages"][name]["status"] == "completed"
        for name in completed_stage_times
    )
    assert phase2.config.storage_reserve_bytes == configured_reserve

    restored_volume = inspect_volume(phase2.config.paths.output_dir)
    failed["config"]["destination_volumes"]["output"] = {
        "device": restored_volume.device,
        "mount_point": str(restored_volume.mount_point),
        "filesystem": restored_volume.filesystem,
    }
    phase2.store.connection.execute(
        "UPDATE jobs SET config_json = ? WHERE id = ?",
        (json.dumps(failed["config"]), job_id),
    )
    phase2.store.connection.commit()
    resumed = phase2.service.resume(job_id)
    completed = phase2.service.status(job_id)

    assert completed["status"] == "completed"
    assert all(
        completed["stages"][name]["completed_at"] == completed_at
        for name, completed_at in completed_stage_times.items()
    )
    assert (
        phase2.provider.uploads,
        phase2.provider.broad_calls,
        phase2.provider.candidate_calls,
    ) == provider_calls
    assert len(resumed["outputs"]) == 5
    assert phase2.config.storage_reserve_bytes == configured_reserve
    assert _source_digests(phase2.input_dir) == source_before


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


def test_tampered_proxy_is_rejected_before_provider_upload(
    phase2: Phase2Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_segment = phase2.service._ensure_segment

    def tamper_after_segmentation(
        job_id: str, proxied: dict[str, Any]
    ) -> dict[str, Any]:
        segmented = ensure_segment(job_id, proxied)
        manifest = phase2.store.get_job(job_id)["proxy_manifests"][0]
        proxy_path = Path(manifest["data"]["artifact_path"])
        proxy_path.write_bytes(proxy_path.read_bytes() + b"tampered")
        return segmented

    monkeypatch.setattr(phase2.service, "_ensure_segment", tamper_after_segmentation)
    before = _source_digests(phase2.input_dir)

    with pytest.raises(VideoEditorError):
        phase2.service.run(phase2.input_dir)

    state = phase2.service.status(
        phase2.store.connection.execute("SELECT id FROM jobs").fetchone()["id"]
    )
    assert phase2.provider.uploads == 0
    assert phase2.provider.broad_calls == 0
    assert phase2.provider.candidate_calls == 0
    assert all(
        state["stages"][name]["status"] == "completed"
        for name in ("inspect", "proxy", "segment")
    )
    assert not _artifacts(state, "plan")
    assert not _artifacts(state, "render")
    assert not list(phase2.config.paths.output_dir.rglob("*.mp4"))
    assert _source_digests(phase2.input_dir) == before


def test_provider_timeout_stops_before_candidate_planning_or_rendering(
    phase2: Phase2Env,
) -> None:
    phase2.provider.timeout_broad = True

    with pytest.raises(VideoEditorError):
        phase2.service.run(phase2.input_dir)

    state = phase2.service.status(
        phase2.store.connection.execute("SELECT id FROM jobs").fetchone()["id"]
    )
    rows = phase2.store.connection.execute(
        "SELECT status FROM analysis_requests WHERE job_id = ?",
        (state["job_id"],),
    ).fetchall()
    statuses = [row["status"] for row in rows]
    assert phase2.provider.broad_calls == 1
    assert phase2.provider.candidate_calls == 0
    assert "billing_unknown" in statuses
    assert phase2.store.budget_state(state["job_id"]).reserved_usd > 0
    assert all(
        state["stages"][name]["status"] == "completed"
        for name in ("inspect", "proxy", "segment")
    )
    assert not _artifacts(state, "plan")
    assert not _artifacts(state, "render")
    assert not list(phase2.config.paths.output_dir.rglob("*.mp4"))


def test_invalid_broad_response_fails_closed_before_candidate_planning(
    phase2: Phase2Env,
) -> None:
    phase2.provider.invalid_broad = True

    with pytest.raises(VideoEditorError) as caught:
        phase2.service.run(phase2.input_dir)

    state = phase2.service.status(caught.value.safe_details["job_id"])
    rows = phase2.store.connection.execute(
        "SELECT status FROM analysis_requests WHERE job_id = ?",
        (state["job_id"],),
    ).fetchall()
    assert phase2.provider.uploads == 1
    assert phase2.provider.broad_calls == 1
    assert phase2.provider.candidate_calls == 0
    assert "billing_unknown" in [row["status"] for row in rows]
    assert phase2.store.budget_state(state["job_id"]).reserved_usd > 0
    assert all(
        state["stages"][name]["status"] == "completed"
        for name in ("inspect", "proxy", "segment")
    )
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


def test_failed_rerender_never_attests_stale_output(
    phase2: Phase2Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = phase2.service.run(phase2.input_dir)
    job_id = result["job_id"]
    outputs = {Path(item["output"]).name: item for item in result["outputs"]}
    short = Path(outputs["short-01.mp4"]["output"])
    short_mtime = short.stat().st_mtime_ns
    plan_path = Path(outputs["short-01.mp4"]["plan"])
    plan = json.loads(plan_path.read_text())
    plan["output"]["theme_summary"] = "edited theme, same clips and duration"
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    real_run_render = workflow_module.__dict__["run_render"]

    def failing_render(*args: Any, **kwargs: Any) -> None:
        raise VideoEditorError(ErrorCategory.RENDER, "simulated ffmpeg failure")

    monkeypatch.setattr(workflow_module, "run_render", failing_render)
    with pytest.raises(VideoEditorError):
        phase2.service.resume(job_id)
    assert short.stat().st_mtime_ns == short_mtime
    monkeypatch.setattr(workflow_module, "run_render", real_run_render)

    phase2.service.resume(job_id)

    assert short.stat().st_mtime_ns != short_mtime
