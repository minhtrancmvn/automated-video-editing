"""Public CLI acceptance coverage using generated local media only."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

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


def _make_media(path: Path, *, tone: bool, frequency: int = 440) -> None:
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=10:duration=1",
    ]
    if tone:
        args.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=16000:duration=1",
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


def test_run_produces_validated_outputs_without_changing_originals(
    media_batch: MediaBatch, cli: CliRunner, config_file: Path
) -> None:
    before = hash_tree(media_batch.input_dir)
    original_socket = socket.socket
    socket.socket = _assert_no_network  # type: ignore[assignment]
    try:
        result = cli.invoke(
            app, ["run", str(media_batch.input_dir), "--config", str(config_file)]
        )
    finally:
        socket.socket = original_socket

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
    assert horizontal.provenance.planner == "phase1-sample-v1"
    assert vertical.provenance.planner == "phase1-sample-v1"
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
