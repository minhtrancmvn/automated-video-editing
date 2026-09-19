from decimal import Decimal
from pathlib import Path

import pytest

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


def _plan(
    tmp_path: Path,
    *,
    audio: str = "source",
    framing: Framing | None = None,
    speed: Decimal = Decimal(1),
    transition: Transition | None = None,
    source_path: Path | None = None,
) -> EditPlan:
    source_path = source_path or tmp_path / "source.mp4"
    clip_framing = framing or Framing(mode="fit_background", background="black")
    first_duration = Decimal(4) / speed
    clips = [
        TimelineClip(
            source_id="first",
            source_start=Decimal(1),
            source_end=Decimal(5),
            timeline_start=Decimal(0),
            speed=speed,
            framing=clip_framing,
            selection_reason="test",
        )
    ]
    sources = [
        PlanSource(
            id="first",
            path=source_path,
            identity="first-id",
            duration=Decimal(10),
            has_audio=True,
        )
    ]
    transitions = []
    if transition is not None:
        sources.append(
            PlanSource(
                id="second",
                path=tmp_path / "second.mp4",
                identity="second-id",
                duration=Decimal(10),
                has_audio=True,
            )
        )
        clips.append(
            TimelineClip(
                source_id="second",
                source_start=Decimal(0),
                source_end=Decimal(4),
                timeline_start=first_duration - transition.duration,
                speed=Decimal(1),
                framing=clip_framing,
                selection_reason="test",
            )
        )
        transitions.append(transition)
    return EditPlan(
        schema_version=1,
        planner_version="test",
        sources=sources,
        clips=clips,
        transitions=transitions,
        output=OutputSpec(
            kind="short",
            width=1920,
            height=1080,
            frame_rate=Decimal(30),
            codec="h264",
            audio=audio,
        ),
        provenance=Provenance(planner="test"),
    )


def _graph(plan: EditPlan) -> str:
    command = compile_render(plan, "ffmpeg")
    return command.args[command.args.index("-filter_complex") + 1]


def test_compiler_never_returns_shell_text(tmp_path: Path) -> None:
    plan = _plan(tmp_path, source_path=tmp_path / "clip;touch owned.mp4")

    command = compile_render(plan, "ffmpeg")

    assert isinstance(command.args, tuple)
    assert command.args[0] == "ffmpeg"
    assert any(";touch owned" in arg for arg in command.args)


def test_partial_output_is_on_final_volume(tmp_path: Path) -> None:
    command = compile_render(_plan(tmp_path), "ffmpeg")

    assert command.partial_path.parent == command.final_path.parent
    assert command.partial_path.suffix == ".partial"


def test_video_normalizes_fps_and_time_base_before_concat(tmp_path: Path) -> None:
    graph = _graph(_plan(tmp_path))

    assert "fps=30,settb=AVTB" in graph
    assert "format=yuv420p,settb=AVTB[vout]" in graph


def test_trim_and_speed_apply_to_video_and_audio(tmp_path: Path) -> None:
    graph = _graph(_plan(tmp_path, speed=Decimal(2)))

    assert "trim=start=1:end=5,setpts=PTS-STARTPTS,setpts=PTS/2" in graph
    assert "atrim=start=1:end=5,asetpts=PTS-STARTPTS,atempo=2" in graph
    assert "aresample=48000" in graph
    assert "sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo" in graph


def test_dissolve_uses_transition_duration_for_video_and_audio(tmp_path: Path) -> None:
    plan = _plan(
        tmp_path,
        transition=Transition(from_clip=0, to_clip=1, kind="dissolve", duration=Decimal("1.5")),
    )
    graph = _graph(plan)

    assert "xfade=transition=fade:duration=1.5:offset=2.5" in graph
    assert "eof_action=pass" not in graph
    assert "acrossfade=d=1.5:c1=tri:c2=tri" in graph


def test_explicit_silence_never_maps_source_audio(tmp_path: Path) -> None:
    graph = _graph(_plan(tmp_path, audio="silence"))

    assert "anullsrc=r=48000:cl=stereo" in graph
    assert "[0:a]" not in graph


def test_horizontal_fit_uses_requested_dimensions(tmp_path: Path) -> None:
    graph = _graph(_plan(tmp_path))

    assert "scale=1920:1080:force_original_aspect_ratio=increase" in graph
    assert "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black" in graph


def test_vertical_fit_uses_blurred_background(tmp_path: Path) -> None:
    plan = _plan(tmp_path).model_copy(
        update={
            "output": OutputSpec(
                kind="short",
                width=1080,
                height=1920,
                frame_rate=Decimal(30),
                codec="h264",
                audio="source",
            )
        }
    )
    graph = _graph(plan)

    assert "scale=1080:1920:force_original_aspect_ratio=increase" in graph
    assert "boxblur=20:2" in graph
    assert "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black" in graph


def test_center_crop_uses_crop_not_background_overlay(tmp_path: Path) -> None:
    graph = _graph(_plan(tmp_path, framing=Framing(mode="center_crop")))

    assert "force_original_aspect_ratio=increase,crop=1920:1080" in graph
    assert "boxblur" not in graph


def test_hardware_probe_failure_records_structured_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "video_editor.rendering.compiler._hardware_probe",
        lambda _ffmpeg, _encoder: (False, "VideoToolbox unavailable"),
    )

    command = compile_render(_plan(tmp_path), "ffmpeg", encoder="h264_videotoolbox")

    assert command.args[command.args.index("-c:v") + 1] == "libx264"
    assert len(command.warnings) == 1
    warning = command.warnings[0]
    assert warning.code == "hardware_encoder_fallback"
    assert warning.reason == "VideoToolbox unavailable"
    assert warning.requested_encoder == "h264_videotoolbox"
    assert warning.selected_encoder == "libx264"
