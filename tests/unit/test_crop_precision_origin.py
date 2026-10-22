"""Crop paths retain exact motion and explicit clip-local time alignment."""

from decimal import Decimal as D

from test_tracking import _raw_track, _subject

from video_editor.analysis.models import CropKeyframe, CropTrack
from video_editor.analysis.tracking import (
    _keyframe_for_observation,
    best_static_crop,
    validate_crop_track,
)
from video_editor.config import CropSettings
from video_editor.models.edit_plan import CropKeyframe as PlanKeyframe
from video_editor.workflow import _plain_track


def test_plain_track_never_rounds_motion_above_existing_velocity_limit() -> None:
    width_ratio = D(608) / D(1920)
    frames = tuple(
        CropKeyframe(
            time=D(i) / D(15),
            center_x=D("0.3") + (D("0.25") - D("1e-20")) * width_ratio * (D(i) / D(15)),
            center_y=D("0.5"),
            subject_box_id=None,
            fallback="tracked",
        )
        for i in range(20)
    )
    original = _raw_track(*frames)
    assert validate_crop_track(original).max_velocity <= D("0.25")
    plain = _plain_track(original)
    assert plain.keyframes == original.keyframes
    assert validate_crop_track(plain).max_velocity <= D("0.25")
    assert CropTrack.model_validate_json(plain.model_dump_json()).keyframes == frames


def test_plan_crop_json_is_plain_decimal_without_precision_loss() -> None:
    frame = PlanKeyframe(
        time=D("0.00000001"),
        center_x=D("0.123456789123456789"),
        center_y=D("0.5"),
        fallback="tracked",
    )
    payload = frame.model_dump(mode="json")
    assert payload["time"] == "0.00000001"
    assert payload["center_x"] == "0.123456789123456789"
    assert PlanKeyframe.model_validate(payload) == frame


def test_delayed_observation_uses_candidate_origin_for_retention() -> None:
    observed = _subject("local", "105", "0.72", "0.82", origin="local_track")
    frames = (
        CropKeyframe(
            time=D(0), center_x=D("0.2"), center_y=D("0.5"), fallback="ease_center"
        ),
        CropKeyframe(
            time=D(5),
            center_x=D("0.77"),
            center_y=D("0.5"),
            subject_box_id="local",
            fallback="tracked",
        ),
    )
    raw = _raw_track(*frames).model_copy(update={"observations": (observed,)})
    track = CropTrack.model_validate({**raw.model_dump(), "source_start": D(100)})
    assert _keyframe_for_observation(track, observed).time == D(5)
    assert validate_crop_track(track).retention_ratio == 1
    legacy = CropTrack.model_validate(raw.model_dump())
    assert legacy.source_start is None
    assert _keyframe_for_observation(legacy, observed).time == 0


def test_static_fallback_keeps_explicit_candidate_origin() -> None:
    observed = _subject("local", "105", "0.42", "0.52", origin="local_track")
    raw = _raw_track().model_copy(update={"observations": (observed,)})
    track = CropTrack.model_validate({**raw.model_dump(), "source_start": D(100)})
    result = best_static_crop(track, CropSettings())
    assert result.source_start == D(100)
    assert result.keyframes[0].time == D(5)
    assert validate_crop_track(result).retention_ratio == 1
