"""Real FFmpeg coverage for phase 2 tracked crops and transitions."""

from __future__ import annotations

import shutil
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from video_editor.analysis.models import (
    CropKeyframe,
    CropTrack,
    NormalizedSubjectBox,
    SubjectObservation,
)
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.probe import probe_media
from video_editor.models.edit_plan import EditPlanV2
from video_editor.rendering.compiler import compile_render
from video_editor.rendering.runner import run_render
from video_editor.validation import outputs

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)


def _scores() -> dict[str, Any]:
    return {
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


def _clip(
    clip_id: str,
    source_id: str,
    source_identity: str,
    *,
    timeline_start: Decimal,
    framing: dict[str, Any],
) -> dict[str, Any]:
    return {
        "clip_id": clip_id,
        "source_id": source_id,
        "source_identity": source_identity,
        "source_start": "0",
        "source_end": "2",
        "timeline_start": str(timeline_start),
        "speed": "1",
        "framing": framing,
        "selection_reason": "synthetic fixture",
        "overall_score": "0.8",
        "score_breakdown": _scores(),
        "analysis_reference": f"analysis:{clip_id}",
        "source_proxy_mapping_reference": f"mapping:{clip_id}",
        "dedup_group": f"group:{clip_id}",
        "chapter_id": f"chapter:{clip_id}",
        "event_id": f"event:{clip_id}",
        "vertical_suitability": "0.9",
        "confidence": "0.9",
        "planner_version": "test",
        "analysis_version": "test",
    }


def _plan(
    source_paths: list[Path],
    *,
    filename: str,
    transition_kind: str | None = None,
    framing: dict[str, Any] | None = None,
) -> EditPlanV2:
    duration = (
        Decimal("0.4")
        if transition_kind is not None and transition_kind != "cut"
        else Decimal(0)
    )
    relation = {
        "cut": "same_event",
        "dissolve": "same_event",
        "fade": "chapter_boundary",
        "fade_black": "time_jump",
    }
    clips = [
        _clip(
            "clip-1",
            "source-1",
            "sha256:source-1",
            timeline_start=Decimal(0),
            framing=framing or {"mode": "center_crop"},
        )
    ]
    transitions: list[dict[str, Any]] = []
    if transition_kind is not None:
        clips.append(
            _clip(
                "clip-2",
                "source-2",
                "sha256:source-2",
                timeline_start=Decimal(2) - duration,
                framing={"mode": "center_crop"},
            )
        )
        transitions.append(
            {
                "from_clip": 0,
                "to_clip": 1,
                "kind": transition_kind,
                "duration": str(duration),
                "relation": relation[transition_kind],
                "reason": "synthetic semantic boundary",
                "confidence": "0.9",
                "audio_policy": (
                    "cut"
                    if transition_kind == "cut"
                    else "crossfade"
                    if transition_kind == "dissolve"
                    else "fade_out_in"
                ),
            }
        )
    return EditPlanV2.model_validate(
        {
            "schema_version": 2,
            "planner_version": "test",
            "analysis_version": "test",
            "sources": [
                {
                    "id": f"source-{index}",
                    "path": str(path),
                    "identity": f"sha256:source-{index}",
                    "duration": "2",
                    "has_audio": True,
                }
                for index, path in enumerate(source_paths, start=1)
            ],
            "clips": clips,
            "transitions": transitions,
            "output": {
                "plan_id": filename.removesuffix(".mp4"),
                "filename": filename,
                "kind": "short",
                "width": 1080,
                "height": 1920,
                "frame_rate": "15",
                "codec": "libx264",
                "audio": "source",
                "theme_summary": "synthetic",
            },
            "provenance": {"planner": "test"},
        }
    )


def _make_source(path: Path, color: str, *, moving: bool = False) -> None:
    video = f"color=c={color}:s=320x180:r=15:d=2"
    if moving:
        video = (
            "color=c=blue:s=320x180:r=15:d=2[bg];"
            "color=c=red:s=40x70:r=15:d=2[subject];"
            "[bg][subject]overlay=x='35+95*t':y=55"
        )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            video,
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        shell=False,
    )


def _red_subject_retention(path: Path, sample_count: int = 20) -> Decimal:
    capture = cv2.VideoCapture(str(path))
    retained = 0
    decoded = 0
    try:
        frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
        for index in range(sample_count):
            frame_index = min(
                frame_count - 1,
                int((index + Decimal("0.5")) * frame_count / sample_count),
            )
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            decoded += 1
            blue, green, red = cv2.split(frame)
            mask = (
                (red > 140)
                & (red > green.astype(np.int16) + 40)
                & (red > blue.astype(np.int16) + 40)
            )
            retained += bool(np.count_nonzero(mask) >= 200)
    finally:
        capture.release()
    if decoded == 0:
        return Decimal(0)
    return Decimal(retained) / Decimal(decoded)


def test_tracked_crop_render_retains_moving_subject_and_quality_evidence(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    source = source_dir / "moving.mp4"
    _make_source(source, "blue", moving=True)
    framing = {
        "mode": "tracked_crop",
        "track_id": "track-1",
        "track_clip_id": "clip-1",
        "track_source_identity": "sha256:source-1",
        "keyframes": [
            {
                "time": "0",
                "center_x": "0.18",
                "center_y": "0.5",
                "subject_box_id": "box-1",
                "fallback": "tracked",
            },
            {
                "time": "2",
                "center_x": "0.70",
                "center_y": "0.5",
                "subject_box_id": "box-2",
                "fallback": "tracked",
            },
        ],
    }
    plan = _plan([source], filename="short-01.mp4", framing=framing)
    track = CropTrack(
        track_id="track-1",
        clip_id="clip-1",
        source_id="source-1",
        source_identity="sha256:source-1",
        source_width=320,
        source_height=180,
        output_width=1080,
        output_height=1920,
        crop_width=101,
        crop_height=180,
        observations=(
            SubjectObservation(
                observation_id="box-1",
                source_id="source-1",
                source_identity="sha256:source-1",
                source_time="0",
                box=NormalizedSubjectBox(
                    x_min="0.109375",
                    y_min="0.3055555556",
                    x_max="0.234375",
                    y_max="0.6944444444",
                ),
                priority=0,
                origin="local_track",
            ),
            SubjectObservation(
                observation_id="box-2",
                source_id="source-1",
                source_identity="sha256:source-1",
                source_time="2",
                box=NormalizedSubjectBox(
                    x_min="0.703125",
                    y_min="0.3055555556",
                    x_max="0.828125",
                    y_max="0.6944444444",
                ),
                priority=0,
                origin="local_track",
            ),
        ),
        keyframes=(
            CropKeyframe(
                time="0",
                center_x="0.18",
                center_y="0.5",
                subject_box_id="box-1",
                fallback="tracked",
            ),
            CropKeyframe(
                time="2",
                center_x="0.70",
                center_y="0.5",
                subject_box_id="box-2",
                fallback="tracked",
            ),
        ),
        short_eligible=True,
    )
    command = compile_render(plan, "ffmpeg", output_dir=tmp_path / "renders")

    run_render(command, lambda: None)
    evidence = outputs.validate_phase2_output(command.final_path, plan, [track])
    probe = probe_media(command.final_path)

    assert (probe.video.width, probe.video.height) == (1080, 1920)
    assert probe.audio is not None
    assert evidence.crop_track_ids == ("track-1",)
    assert evidence.subject_retention_ratio >= Decimal("0.95")
    assert _red_subject_retention(command.final_path) >= Decimal("0.95")
    assert not evidence.unintended_black_bar_timestamps
    assert evidence.audio_discontinuity_timestamps == ()


def test_phase2_validation_rejects_crop_track_identity_mismatch(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    source = source_dir / "moving.mp4"
    _make_source(source, "blue", moving=True)
    framing = {
        "mode": "tracked_crop",
        "track_id": "track-1",
        "track_clip_id": "clip-1",
        "track_source_identity": "sha256:source-1",
        "keyframes": [
            {
                "time": "0",
                "center_x": "0.5",
                "center_y": "0.5",
                "subject_box_id": "box-1",
                "fallback": "static",
            }
        ],
    }
    plan = _plan([source], filename="short-05.mp4", framing=framing)
    command = compile_render(plan, "ffmpeg", output_dir=tmp_path / "renders")
    run_render(command, lambda: None)
    mismatched = CropTrack(
        track_id="different-track",
        clip_id="clip-1",
        source_id="source-1",
        source_identity="sha256:source-1",
        source_width=320,
        source_height=180,
        output_width=1080,
        output_height=1920,
        crop_width=101,
        crop_height=180,
        observations=(),
        keyframes=(
            CropKeyframe(
                time="0",
                center_x="0.5",
                center_y="0.5",
                subject_box_id="box-1",
                fallback="static",
            ),
        ),
        short_eligible=True,
    )

    with pytest.raises(VideoEditorError) as exc:
        outputs.validate_phase2_output(command.final_path, plan, [mismatched])

    assert exc.value.category == ErrorCategory.OUTPUT
    assert exc.value.code == "crop_track_identity_mismatch"


@pytest.mark.parametrize(
    ("kind", "filename"),
    [
        ("cut", "short-01.mp4"),
        ("dissolve", "short-02.mp4"),
        ("fade", "short-03.mp4"),
        ("fade_black", "short-04.mp4"),
    ],
)
def test_real_semantic_transition_renders_have_expected_media(
    tmp_path: Path, kind: str, filename: str
) -> None:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    first = source_dir / "first.mp4"
    second = source_dir / "second.mp4"
    _make_source(first, "red")
    _make_source(second, "green")
    plan = _plan([first, second], filename=filename, transition_kind=kind)
    command = compile_render(plan, "ffmpeg", output_dir=tmp_path / "renders")

    run_render(command, lambda: None)
    evidence = outputs.validate_phase2_output(command.final_path, plan, [])
    probe = probe_media(command.final_path)

    assert command.final_path.name == filename
    assert (probe.video.width, probe.video.height) == (1080, 1920)
    assert probe.audio is not None
    assert abs(Decimal(str(probe.duration)) - command.expected_duration) <= Decimal(
        "0.20"
    )
    assert not evidence.unintended_black_bar_timestamps
    assert not evidence.audio_discontinuity_timestamps
