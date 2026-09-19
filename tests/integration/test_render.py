import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from video_editor.media.probe import probe_media
from video_editor.models.edit_plan import (
    EditPlan,
    Framing,
    OutputSpec,
    PlanSource,
    Provenance,
    TimelineClip,
)
from video_editor.rendering.compiler import compile_render
from video_editor.rendering.runner import run_render

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)


def _plan(tmp_path: Path) -> EditPlan:
    source = tmp_path / "tone source.mp4"
    return EditPlan(
        schema_version=1,
        planner_version="test",
        sources=[
            PlanSource(
                id="tone",
                path=source,
                identity="tone",
                duration=Decimal(2),
                has_audio=True,
            ),
            PlanSource(
                id="silent",
                path=tmp_path / "silent source.mp4",
                identity="silent",
                duration=Decimal(2),
                has_audio=False,
            ),
        ],
        clips=[
            TimelineClip(
                source_id="tone",
                source_start=Decimal(0),
                source_end=Decimal(1),
                timeline_start=Decimal(0),
                speed=Decimal(1),
                framing=Framing(mode="fit_background", background="black"),
                selection_reason="test",
            ),
            TimelineClip(
                source_id="silent",
                source_start=Decimal(0),
                source_end=Decimal(1),
                timeline_start=Decimal(1),
                speed=Decimal(1),
                framing=Framing(mode="fit_background", background="black"),
                selection_reason="test",
            ),
        ],
        output=OutputSpec(
            kind="short",
            width=1920,
            height=1080,
            frame_rate=Decimal(30),
            codec="h264",
            audio="source",
        ),
        provenance=Provenance(planner="test"),
    )


def _make_media(path: Path, *, audio: bool) -> None:
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=30:duration=2",
    ]
    if audio:
        args.extend(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2"])
    args.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    if audio:
        args.extend(["-c:a", "aac", "-shortest"])
    else:
        args.append("-an")
    args.append(str(path))
    subprocess.run(args, check=True, shell=False)


def test_render_tone_and_silent_clips_with_libx264(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    _make_media(plan.sources[0].path, audio=True)
    _make_media(plan.sources[1].path, audio=False)

    command = compile_render(plan, "ffmpeg", encoder="libx264")
    run_render(command, lambda: None)
    result = probe_media(command.final_path)

    assert command.final_path.exists()
    assert result.video is not None
    assert (result.video.width, result.video.height) == (1920, 1080)
    assert result.video.codec_name == "h264"
    assert result.audio is not None
    assert result.audio.sample_rate == 48000
    assert result.audio.channel_layout == "stereo"
