import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.probe import probe_media
from video_editor.models.edit_plan import (
    EditPlan,
    Framing,
    OutputSpec,
    PlanSource,
    Provenance,
    TimelineClip,
    Transition,
)
from video_editor.rendering.compiler import compile_render
from video_editor.rendering.runner import run_render
from video_editor.validation.outputs import validate_output

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)


def _plan(
    tmp_path: Path,
    *,
    frame_rate: Decimal = Decimal(30),
    transition: Transition | None = None,
) -> EditPlan:
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
                timeline_start=Decimal(1)
                - (transition.duration if transition else Decimal(0)),
                speed=Decimal(1),
                framing=Framing(mode="fit_background", background="black"),
                selection_reason="test",
            ),
        ],
        transitions=[transition] if transition is not None else [],
        output=OutputSpec(
            kind="short",
            width=1920,
            height=1080,
            frame_rate=frame_rate,
            codec="h264",
            audio="source",
        ),
        provenance=Provenance(planner="test"),
    )


def _make_media(path: Path, *, audio: bool, frame_rate: int = 30) -> None:
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=320x240:rate={frame_rate}:duration=2",
    ]
    if audio:
        args.extend(
            ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2"]
        )
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


def test_render_dissolve_with_libx264(tmp_path: Path) -> None:
    transition = Transition(
        from_clip=0, to_clip=1, kind="dissolve", duration=Decimal("0.5")
    )
    plan = _plan(tmp_path, transition=transition)
    _make_media(plan.sources[0].path, audio=True)
    _make_media(plan.sources[1].path, audio=True)

    command = compile_render(plan, "ffmpeg", encoder="libx264")
    run_render(command, lambda: None)

    result = probe_media(command.final_path)
    assert result.video is not None
    assert result.audio is not None
    assert abs(Decimal(str(result.duration)) - command.expected_duration) <= Decimal(
        "0.20"
    )


def test_render_mixed_source_frame_rates(tmp_path: Path) -> None:
    plan = _plan(tmp_path, frame_rate=Decimal(30))
    _make_media(plan.sources[0].path, audio=True, frame_rate=24)
    _make_media(plan.sources[1].path, audio=False, frame_rate=60)

    command = compile_render(plan, "ffmpeg", encoder="libx264")
    run_render(command, lambda: None)

    result = probe_media(command.final_path)
    assert result.video is not None
    assert result.video.avg_frame_rate == pytest.approx(30, abs=0.1)


def test_validate_output_rejects_missing_audio_for_silence_policy(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    _make_media(plan.sources[0].path, audio=False)

    with pytest.raises(VideoEditorError) as exc:
        validate_output(
            plan.sources[0].path,
            plan.output.model_copy(
                update={"audio": "silence", "width": 320, "height": 240}
            ),
            Decimal(2),
        )

    assert exc.value.category == ErrorCategory.OUTPUT
    assert "must contain audio for silence policy" in str(exc.value)
