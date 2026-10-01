from decimal import Decimal
from pathlib import Path

import pytest

from video_editor.models.edit_plan import (
    EditPlan,
    EditPlanV2,
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
            codec="libx264",
            audio=audio,
        ),
        provenance=Provenance(planner="test"),
    )


def _graph(plan: EditPlan | EditPlanV2) -> str:
    command = compile_render(plan, "ffmpeg")
    return command.args[command.args.index("-filter_complex") + 1]


def _v2_plan(
    tmp_path: Path,
    *,
    filename: str = "short-01.mp4",
    transition_kind: str = "cut",
    audio_policy: str = "cut",
    framing: dict[str, object] | None = None,
    speed: Decimal = Decimal(1),
) -> EditPlanV2:
    transition_duration = Decimal(0) if transition_kind == "cut" else Decimal("0.5")
    relation = {
        "cut": "same_event",
        "dissolve": "same_event",
        "fade": "chapter_boundary",
        "fade_black": "time_jump",
    }[transition_kind]
    scores = {
        "positive": {
            "action": "0.8",
            "scenic": "0.2",
            "human": "0.5",
            "story": "0.7",
            "technical": "0.9",
            "novelty": "0.6",
            "completeness": "0.8",
            "long_story": "0.7",
            "short": "0.8",
            "vertical": "0.9",
        },
        "penalties": {
            "blur_exposure": "0",
            "shake_obstruction": "0",
            "incomplete": "0",
            "weak_boundary": "0",
            "repetition": "0",
            "overlap": "0",
        },
    }
    first_framing = framing or {"mode": "center_crop"}
    return EditPlanV2.model_validate(
        {
            "schema_version": 2,
            "planner_version": "test",
            "analysis_version": "test",
            "sources": [
                {
                    "id": "first",
                    "path": str(tmp_path / "source.mp4"),
                    "identity": "sha256:first",
                    "duration": "10",
                    "has_audio": True,
                },
                {
                    "id": "second",
                    "path": str(tmp_path / "second.mp4"),
                    "identity": "sha256:second",
                    "duration": "10",
                    "has_audio": True,
                },
            ],
            "clips": [
                {
                    "clip_id": "clip-1",
                    "source_id": "first",
                    "source_identity": "sha256:first",
                    "source_start": "1",
                    "source_end": "5",
                    "timeline_start": "0",
                    "speed": str(speed),
                    "framing": first_framing,
                    "selection_reason": "test",
                    "overall_score": "0.8",
                    "score_breakdown": scores,
                    "analysis_reference": "analysis:1",
                    "source_proxy_mapping_reference": "mapping:1",
                    "dedup_group": "group-1",
                    "chapter_id": "chapter-1",
                    "event_id": "event-1",
                    "vertical_suitability": "0.9",
                    "confidence": "0.9",
                    "planner_version": "test",
                    "analysis_version": "test",
                },
                {
                    "clip_id": "clip-2",
                    "source_id": "second",
                    "source_identity": "sha256:second",
                    "source_start": "2",
                    "source_end": "6",
                    "timeline_start": str(Decimal(4) / speed - transition_duration),
                    "speed": "1",
                    "framing": {"mode": "center_crop"},
                    "selection_reason": "test",
                    "overall_score": "0.8",
                    "score_breakdown": scores,
                    "analysis_reference": "analysis:2",
                    "source_proxy_mapping_reference": "mapping:2",
                    "dedup_group": "group-2",
                    "chapter_id": "chapter-2",
                    "event_id": "event-2",
                    "vertical_suitability": "0.9",
                    "confidence": "0.9",
                    "planner_version": "test",
                    "analysis_version": "test",
                },
            ],
            "transitions": [
                {
                    "from_clip": 0,
                    "to_clip": 1,
                    "kind": transition_kind,
                    "duration": str(transition_duration),
                    "relation": relation,
                    "reason": "semantic boundary",
                    "confidence": "0.9",
                    "audio_policy": audio_policy,
                }
            ],
            "output": {
                "plan_id": "plan-short-01",
                "filename": filename,
                "kind": "short",
                "width": 1080,
                "height": 1920,
                "frame_rate": "30",
                "codec": "libx264",
                "audio": "source",
                "theme_summary": "test",
            },
            "provenance": {"planner": "test"},
        }
    )


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
        transition=Transition(
            from_clip=0, to_clip=1, kind="dissolve", duration=Decimal("1.5")
        ),
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
                codec="libx264",
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


def test_multiple_center_crops_have_valid_filter_boundaries(tmp_path: Path) -> None:
    plan = _plan(
        tmp_path,
        framing=Framing(mode="center_crop"),
        transition=Transition(from_clip=0, to_clip=1, kind="cut", duration=Decimal(0)),
    )
    graph = _graph(plan)

    assert "format=yuv420p[vclip0]" in graph
    assert "format=yuv420p[vclip1]" in graph
    assert "format=yuv420p,[vclip0]" not in graph
    assert "format=yuv420p,[vclip1]" not in graph


def test_v2_uses_explicit_output_filename(tmp_path: Path) -> None:
    command = compile_render(
        _v2_plan(tmp_path, filename="short-03.mp4"),
        "ffmpeg",
        output_dir=tmp_path.parent / f"{tmp_path.name}-renders",
    )

    assert command.final_path.name == "short-03.mp4"


def test_tracked_crop_graph_interpolates_original_source_time(tmp_path: Path) -> None:
    framing = {
        "mode": "tracked_crop",
        "track_id": "track-1",
        "track_clip_id": "clip-1",
        "track_source_identity": "sha256:first",
        "keyframes": [
            {
                "time": "0",
                "center_x": "0.25",
                "center_y": "0.5",
                "subject_box_id": "box-1",
                "fallback": "tracked",
            },
            {
                "time": "2",
                "center_x": "0.75",
                "center_y": "0.5",
                "subject_box_id": "box-2",
                "fallback": "tracked",
            },
        ],
    }

    graph = _graph(_v2_plan(tmp_path, framing=framing))

    assert "crop=" in graph
    assert r"if(between(t\,1\,3)" in graph
    assert "min(max(" in graph


def test_tracked_crop_evaluates_source_time_before_speed_retiming(
    tmp_path: Path,
) -> None:
    framing = {
        "mode": "tracked_crop",
        "track_id": "track-1",
        "track_clip_id": "clip-1",
        "track_source_identity": "sha256:first",
        "keyframes": [
            {
                "time": "0",
                "center_x": "0.25",
                "center_y": "0.5",
                "subject_box_id": "box-1",
                "fallback": "tracked",
            },
            {
                "time": "2",
                "center_x": "0.75",
                "center_y": "0.5",
                "subject_box_id": "box-2",
                "fallback": "tracked",
            },
        ],
    }

    graph = _graph(_v2_plan(tmp_path, framing=framing, speed=Decimal(2)))
    tracked_filter = graph.split(";", 1)[0]

    assert tracked_filter.index("crop=") < tracked_filter.index("setpts=PTS/2")


@pytest.mark.parametrize(
    ("kind", "audio_policy", "video_fragment", "audio_fragment"),
    [
        ("cut", "cut", "concat=n=2:v=1:a=0", "concat=n=2:v=0:a=1"),
        (
            "dissolve",
            "crossfade",
            "xfade=transition=fade:duration=0.5:offset=3.5",
            "acrossfade=d=0.5:c1=tri:c2=tri",
        ),
        ("fade", "fade_out_in", "fade=t=out", "afade=t=out"),
        ("fade_black", "fade_out_in", "color=c=black", "anullsrc"),
    ],
)
def test_v2_transition_graphs_and_duration(
    tmp_path: Path,
    kind: str,
    audio_policy: str,
    video_fragment: str,
    audio_fragment: str,
) -> None:
    command = compile_render(
        _v2_plan(
            tmp_path,
            transition_kind=kind,
            audio_policy=audio_policy,
        ),
        "ffmpeg",
    )
    graph = command.args[command.args.index("-filter_complex") + 1]

    assert video_fragment in graph
    assert audio_fragment in graph
    expected = Decimal(8) if kind == "cut" else Decimal("7.5")
    assert command.expected_duration == expected


def test_v1_command_graph_remains_compatible(tmp_path: Path) -> None:
    plan = _plan(
        tmp_path,
        transition=Transition(
            from_clip=0, to_clip=1, kind="dissolve", duration=Decimal("1.5")
        ),
    )

    graph = _graph(plan)

    assert "xfade=transition=fade:duration=1.5:offset=2.5" in graph
    assert "acrossfade=d=1.5:c1=tri:c2=tri" in graph
    assert "color=c=black" not in graph


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
