"""Public CLI acceptance coverage using generated local media only."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from video_editor import cli as cli_module
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
from video_editor.cli import app
from video_editor.config import resolve_config
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.storage import inspect_volume
from video_editor.models.edit_plan import load_plan, timeline_duration
from video_editor.persistence.database import JobStore
from video_editor.workflow import WorkflowService

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)


@dataclass(frozen=True)
class MediaBatch:
    input_dir: Path
    output_dir: Path


def hash_tree(root: Path) -> dict[str, str]:
    """Hash source names and bytes so acceptance verifies originals stay untouched."""

    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def ffprobe_size(path: Path) -> tuple[int, int]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    stream = json.loads(completed.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def _make_media(
    path: Path, *, tone: bool, frequency: int = 440, duration: int = 1
) -> None:
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=320x240:rate=10:duration={duration}",
    ]
    if tone:
        args.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=16000:duration={duration}",
            ]
        )
    args.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    if tone:
        args.extend(["-c:a", "aac", "-shortest"])
    else:
        args.append("-an")
    args.append(str(path))
    subprocess.run(args, check=True, capture_output=True, text=True, shell=False)


@pytest.fixture
def media_batch(tmp_path: Path) -> MediaBatch:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    # One recording split into GoPro chapters, then a second session with no audio.
    _make_media(input_dir / "GOPR0001.MP4", tone=True)
    _make_media(input_dir / "GH020001.MP4", tone=True, frequency=880)
    _make_media(input_dir / "GOPR0002.MP4", tone=False)
    (input_dir / "unreadable.mp4").write_bytes(b"not media")
    return MediaBatch(input_dir=input_dir, output_dir=tmp_path / "output")


@pytest.fixture
def config_file(tmp_path: Path, media_batch: MediaBatch) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[paths]",
                f'input_dir = "{media_batch.input_dir}"',
                f'workspace_dir = "{tmp_path / "workspace"}"',
                f'cache_dir = "{tmp_path / "cache"}"',
                f'output_dir = "{media_batch.output_dir}"',
                f'state_dir = "{tmp_path / "state"}"',
                "[settings]",
                "storage_reserve_bytes = 0",
                "cloud_enabled = false",
                "render_concurrency = 1",
                "",
            ]
        )
    )
    return path


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def _job_id(output: str) -> str:
    match = re.search(r"'job_id': '([^']+)'", output)
    assert match, output
    return match.group(1)


def _source_names(plan_path: Path) -> list[str]:
    return [source.path.name for source in load_plan(plan_path).sources]


def _assert_no_network(address: tuple[object, ...]) -> None:
    raise AssertionError(f"unexpected network access: {address}")


@dataclass
class _OfflineProvider:
    upload_calls: int = 0
    broad_calls: int = 0
    candidate_calls: int = 0
    maximum_request_cost_calls: int = 0

    def upload(self, authorization: AuthorizedUpload) -> UploadedFile:
        self.upload_calls += 1
        authorization.close()
        return UploadedFile(
            name=f"files/{authorization.manifest_id}",
            uri="gs://offline/upload",
            mime_type="video/mp4",
            state="ACTIVE",
            manifest_id=authorization.manifest_id,
        )

    def maximum_request_cost(
        self, manifest_id: str, chunk: Any, *, prompt_version: str
    ) -> Decimal:
        del manifest_id, chunk, prompt_version
        self.maximum_request_cost_calls += 1
        return Decimal("0.0001")

    def broad_scan(
        self,
        upload: UploadedFile,
        chunk: object,
        request: AnalysisRequestContext,
        *,
        prompt_version: str,
    ) -> ProviderResult[BroadScanResponse]:
        del upload, prompt_version
        self.broad_calls += 1
        start = Decimal(str(chunk.proxy_start))  # type: ignore[attr-defined]
        end = Decimal(str(chunk.proxy_end))  # type: ignore[attr-defined]
        step = (end - start) / 8
        categories = ("action", "scenic")
        candidates = tuple(
            {
                "candidate_id": f"{request.chunk_id}-{index}",
                "start": format(start + step * index, "f"),
                "end": format(start + step * (index + 1), "f"),
                "category": categories[index % len(categories)],
                "reason": "visible offline event",
                "confidence": 0.9,
            }
            for index in range(4)
        )
        response = BroadScanResponse.model_validate(
            {
                "schema_version": "broad-v1",
                "chunk_id": request.chunk_id,
                "scenes": [],
                "speech_presence_ranges": [],
                "candidates": candidates,
            }
        )
        return ProviderResult(
            response=response,
            usage=_offline_usage(request.reservation_id),
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
        response = CandidateRefinementResponse.model_validate(
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
                "crop_intent": "follow visible subject",
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
        )
        return ProviderResult(
            response=response, usage=_offline_usage(request.reservation_id)
        )

    def delete_upload(self, upload: UploadedFile) -> None:
        del upload


def _offline_usage(reservation_id: str) -> ProviderUsage:
    attempt = ProviderAttemptUsage(
        request_id=f"offline-{reservation_id}",
        status="succeeded",
        prompt_tokens=0,
        media_input_tokens=0,
        text_input_tokens=0,
        candidates_tokens=0,
        thoughts_tokens=0,
        output_tokens=0,
        total_tokens=0,
    )
    return ProviderUsage(
        reservation_id=reservation_id,
        attempts=(attempt,),
        request_ids=(attempt.request_id,),
        prompt_tokens=0,
        media_input_tokens=0,
        text_input_tokens=0,
        candidates_tokens=0,
        thoughts_tokens=0,
        output_tokens=0,
        total_tokens=0,
        has_unknown_billing=False,
        actual_cost_usd=Decimal(0),
    )


def test_missing_external_volume_fails_without_internal_fallback(
    tmp_path: Path, media_batch: MediaBatch, cli: CliRunner
) -> None:
    config = tmp_path / "missing-volume.toml"
    missing_volume = Path("/Volumes") / f"video-editor-task12-{tmp_path.name}"
    config.write_text(
        "\n".join(
            [
                "[paths]",
                f'input_dir = "{media_batch.input_dir}"',
                f'workspace_dir = "{missing_volume / "workspace"}"',
                f'cache_dir = "{missing_volume / "cache"}"',
                f'output_dir = "{missing_volume / "output"}"',
                f'state_dir = "{tmp_path / "state"}"',
                "[settings]",
                "storage_reserve_bytes = 0",
                "cloud_enabled = false",
                "render_concurrency = 1",
                "",
            ]
        )
    )
    result = cli.invoke(
        app, ["run", str(media_batch.input_dir), "--config", str(config)]
    )
    assert result.exit_code == 11, result.output
    assert "storage" in result.output
    assert not missing_volume.exists()
    config.write_text(config.read_text().replace(str(missing_volume), str(tmp_path)))
    restored = cli.invoke(
        app, ["run", str(media_batch.input_dir), "--config", str(config)]
    )
    assert restored.exit_code == 0, restored.output


def test_missing_configured_output_volume_during_write_resumes_after_restore(
    tmp_path: Path, media_batch: MediaBatch, config_file: Path
) -> None:
    config = resolve_config(config_file)
    with JobStore(config.paths.state_dir / "jobs.sqlite3") as store:
        service = WorkflowService(config, store)
        job_id = service._new_job(media_batch.input_dir)
        service._ensure_inspect(job_id, media_batch.input_dir)
        planned = service._ensure_plan(job_id)
        job = store.get_job(job_id)
        configured_output = config.paths.output_dir
        missing_mount = Path("/Volumes") / f"video-editor-task12-write-{tmp_path.name}"
        recorded = job["config"]["destination_volumes"]["output"]
        recorded["mount_point"] = str(missing_mount)
        store.connection.execute(
            "UPDATE jobs SET config_json = ? WHERE id = ?",
            (json.dumps(job["config"]), job_id),
        )
        store.connection.commit()

        with pytest.raises(VideoEditorError) as caught:
            service._ensure_render(job_id, planned)
        assert caught.value.category == ErrorCategory.STORAGE
        assert not missing_mount.exists()
        failed = service.status(job_id)
        assert failed["status"] == "failed"
        assert failed["stages"]["render"]["status"] == "failed"

        restored = inspect_volume(configured_output)
        failed["config"]["destination_volumes"]["output"] = {
            "device": restored.device,
            "mount_point": str(restored.mount_point),
            "filesystem": restored.filesystem,
        }
        store.connection.execute(
            "UPDATE jobs SET config_json = ? WHERE id = ?",
            (json.dumps(failed["config"]), job_id),
        )
        store.connection.commit()
        rendered = service.resume(job_id)
        assert len(rendered["outputs"]) == 2
        assert service.status(job_id)["stages"]["render"]["status"] == "completed"


def test_real_ffmpeg_interruption_persists_and_resumes_without_rerendering_valid_output(
    tmp_path: Path, media_batch: MediaBatch, config_file: Path
) -> None:
    config = resolve_config(config_file)
    with JobStore(config.paths.state_dir / "jobs.sqlite3") as store:
        service = WorkflowService(config, store)
        job_id = service._new_job(media_batch.input_dir)
        service._ensure_inspect(job_id, media_batch.input_dir)
        service._ensure_proxy(job_id)
        planned = service._ensure_plan(job_id)
        plan_paths = [Path(value) for value in planned["plans"]]
        first = service._render_plan(job_id, plan_paths[0])
        first_output = Path(first["output"])
        first_digest = hashlib.sha256(first_output.read_bytes()).hexdigest()
        config_json = json.dumps(
            {"job_id": job_id, "config": str(config_file), "plans": planned}
        )

    script = """
import json
import sys
from pathlib import Path
from video_editor.config import resolve_config
from video_editor.persistence.database import JobStore
from video_editor.workflow import WorkflowService
payload = json.loads(sys.argv[1])
config = resolve_config(Path(payload["config"]))
with JobStore(config.paths.state_dir / "jobs.sqlite3") as store:
    service = WorkflowService(config, store)
    service._ensure_render(payload["job_id"], payload["plans"])
"""
    process = subprocess.Popen([sys.executable, "-c", script, config_json])
    second_output = config.paths.output_dir / job_id / "short-01.mp4"
    partial = second_output.with_name(second_output.name + ".partial")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if partial.exists() and partial.stat().st_size > 0:
            break
        if process.poll() is not None:
            pytest.fail(f"workflow exited before interruption: {process.returncode}")
        time.sleep(0.05)
    else:
        process.kill()
        pytest.fail("real FFmpeg render did not create partial output")

    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=20) != 0
    with JobStore(config.paths.state_dir / "jobs.sqlite3") as store:
        service = WorkflowService(config, store)
        interrupted = service.status(job_id)
        assert interrupted["status"] == "interrupted"
        assert interrupted["stages"]["render"]["status"] == "interrupted"
        assert partial.exists()
        assert first_output.exists()
        result = service.resume(job_id)
        completed = service.status(job_id)

    assert completed["status"] == "completed"
    assert completed["stages"]["render"]["status"] == "completed"
    assert len(result["outputs"]) == 2
    assert first_digest == hashlib.sha256(first_output.read_bytes()).hexdigest()
    assert not partial.exists()
    assert second_output.exists()
    connection = sqlite3.connect(config.paths.state_dir / "jobs.sqlite3")
    try:
        rows = connection.execute(
            "SELECT path FROM artifacts WHERE job_id = ? AND stage_name = 'render'",
            (job_id,),
        ).fetchall()
    finally:
        connection.close()
    assert {Path(row[0]).name for row in rows} == {"long.mp4", "short-01.mp4"}


def test_phase2_public_cli_creates_full_offline_outputs_and_resumes(
    tmp_path: Path, cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dir = tmp_path / "phase2-input"
    input_dir.mkdir()
    source = input_dir / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=640x360:r=30:d=8",
            "-f",
            "lavfi",
            "-i",
            "testsrc=s=80x80:r=30:d=8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=8",
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
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    before = hash_tree(input_dir)
    config = tmp_path / "phase2-config.toml"
    output_dir = tmp_path / "phase2-output"
    config.write_text(
        "\n".join(
            [
                "[paths]",
                f'input_dir = "{input_dir}"',
                f'workspace_dir = "{tmp_path / "phase2-workspace"}"',
                f'cache_dir = "{tmp_path / "phase2-cache"}"',
                f'output_dir = "{output_dir}"',
                f'state_dir = "{tmp_path / "phase2-state"}"',
                "[settings]",
                "storage_reserve_bytes = 0",
                "cloud_enabled = true",
                "render_concurrency = 1",
                "[gemini]",
                "enabled = true",
                'max_cost_per_source_hour_usd = "1.00"',
                "",
            ]
        )
    )
    provider = _OfflineProvider()
    original_service = cli_module._service

    def offline_service(
        config_path: Path, progress: Callable[[str], None] | None = None
    ) -> tuple[WorkflowService, JobStore]:
        service, store = original_service(config_path, progress)
        service.provider = provider
        return service, store

    monkeypatch.setattr(cli_module, "_service", offline_service)
    monkeypatch.setenv("GEMINI_API_KEY", "offline-placeholder-not-a-secret")
    with monkeypatch.context() as network_guard:
        network_guard.setattr(socket, "socket", _assert_no_network)
        result = cli.invoke(app, ["run", str(input_dir), "--config", str(config)])

    assert result.exit_code == 0, result.output
    job_id = _job_id(result.output)
    job_output = output_dir / job_id
    output_paths = sorted(job_output.glob("*.mp4"))
    assert [path.name for path in output_paths] == [
        "long.mp4",
        "short-01.mp4",
        "short-02.mp4",
    ]
    assert ffprobe_size(output_paths[0]) == (1920, 1080)
    assert all(ffprobe_size(path) == (1080, 1920) for path in output_paths[1:])
    assert hash_tree(input_dir) == before
    report = json.loads((job_output / "report.json").read_text())
    assert report["status"] == "completed"
    assert report["analysis"]["coverage"]["complete"] is True
    assert Decimal(report["analysis"]["budget"]["spent_usd"]) <= (
        Decimal(report["analysis"]["coverage"]["expected_seconds"])
        / Decimal(3600)
        * Decimal("1.00")
    )
    assert provider.upload_calls > 0
    assert provider.broad_calls > 0
    assert provider.candidate_calls > 0
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in output_paths
    }
    call_counts = (
        provider.upload_calls,
        provider.broad_calls,
        provider.candidate_calls,
    )
    with monkeypatch.context() as network_guard:
        network_guard.setattr(socket, "socket", _assert_no_network)
        resumed = cli.invoke(app, ["resume", job_id, "--config", str(config)])
    assert resumed.exit_code == 0, resumed.output
    assert call_counts == (
        provider.upload_calls,
        provider.broad_calls,
        provider.candidate_calls,
    )
    assert hashes == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in output_paths
    }


def test_run_produces_validated_outputs_without_changing_originals(
    media_batch: MediaBatch,
    cli: CliRunner,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = hash_tree(media_batch.input_dir)
    with monkeypatch.context() as network_guard:
        network_guard.setattr(socket, "socket", _assert_no_network)
        result = cli.invoke(
            app, ["run", str(media_batch.input_dir), "--config", str(config_file)]
        )

    assert result.exit_code == 0, result.output
    job_id = _job_id(result.output)
    output_dir = media_batch.output_dir / job_id
    horizontal_plan = output_dir / "edit-plan-horizontal.json"
    vertical_plan = output_dir / "edit-plan-vertical.json"
    horizontal_output = output_dir / "long.mp4"
    vertical_output = output_dir / "short-01.mp4"

    assert ffprobe_size(horizontal_output) == (1920, 1080)
    assert ffprobe_size(vertical_output) == (1080, 1920)
    assert horizontal_plan.exists()
    assert vertical_plan.exists()
    assert (output_dir / "report.json").exists()
    assert (output_dir / "report.md").exists()
    assert hash_tree(media_batch.input_dir) == before

    horizontal = load_plan(horizontal_plan)
    vertical = load_plan(vertical_plan)
    assert horizontal.provenance.planner == "phase1-sample-v2"
    assert vertical.provenance.planner == "phase1-sample-v2"
    assert all(clip.framing.mode == "center_crop" for clip in vertical.clips)
    assert _source_names(horizontal_plan) == [
        "GOPR0001.MP4",
        "GH020001.MP4",
        "GOPR0002.MP4",
    ]
    assert _source_names(vertical_plan) == _source_names(horizontal_plan)
    assert timeline_duration(horizontal) < Decimal(3600)
    assert timeline_duration(vertical) < Decimal(3600)

    status = cli.invoke(app, ["status", job_id, "--config", str(config_file)])
    assert status.exit_code == 0, status.output
    assert "completed" in status.output
    assert str(horizontal_output) in status.output
    assert str(vertical_output) in status.output

    rendered_before_resume = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (horizontal_output, vertical_output)
    }
    resumed = cli.invoke(app, ["resume", job_id, "--config", str(config_file)])
    assert resumed.exit_code == 0, resumed.output
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (horizontal_output, vertical_output)
    } == rendered_before_resume
    assert len(list(output_dir.glob("*.mp4"))) == 2
