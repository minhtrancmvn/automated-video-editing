"""Smoothing must leave Decimal headroom below strict motion limits."""

from decimal import Decimal as D

import pytest

from video_editor.analysis.models import CropKeyframe, CropTrack
from video_editor.analysis.tracking import smooth_crop_track, validate_crop_track
from video_editor.config import CropSettings


def _track(fps: int, count: int, start: str, target: str) -> CropTrack:
    return CropTrack(
        track_id="synthetic",
        clip_id="clip",
        source_id="s",
        source_identity="identity",
        source_width=1280,
        source_height=720,
        output_width=1080,
        output_height=1920,
        crop_width=405,
        crop_height=720,
        source_start=D(0),
        observations=(),
        short_eligible=False,
        keyframes=tuple(
            CropKeyframe(
                time=D(i) / D(fps),
                center_x=D(start if i == 0 else target),
                center_y=D("0.5"),
                fallback="tracked",
            )
            for i in range(count)
        ),
    )


@pytest.mark.parametrize("fps", [15, 30])
@pytest.mark.parametrize("count", [10, 30, 90])
@pytest.mark.parametrize("start,target", [("0.5", "0.3"), ("0.3", "0.8")])
def test_smoothed_acceleration_stays_strictly_within_limit(
    fps: int,
    count: int,
    start: str,
    target: str,
) -> None:
    settings = CropSettings()
    raw = _track(fps, count, start, target)
    smoothed = smooth_crop_track(raw, settings)
    measured = validate_crop_track(smoothed, settings=settings)
    assert measured.max_velocity <= settings.max_velocity_widths_per_second
    assert (
        measured.max_acceleration <= settings.max_acceleration_widths_per_second_squared
    )
    # Persistence must not reintroduce a numerical limit violation.
    restored = CropTrack.model_validate_json(smoothed.model_dump_json())
    assert validate_crop_track(restored, settings=settings) == measured
    assert raw.keyframes[0].center_x == D(start)


def test_validator_does_not_accept_tiny_actual_acceleration_over_limit() -> None:
    ratio = D(405) / D(1280)
    velocity = D("0.1") * ratio
    acceleration = D("0.500000001") * ratio
    track = _track(1, 3, "0.3", "0.3").model_copy(
        update={
            "keyframes": (
                CropKeyframe(
                    time=D(0), center_x=D("0.3"), center_y=D("0.5"), fallback="tracked"
                ),
                CropKeyframe(
                    time=D(1),
                    center_x=D("0.3") + velocity,
                    center_y=D("0.5"),
                    fallback="tracked",
                ),
                CropKeyframe(
                    time=D(2),
                    center_x=D("0.3") + 2 * velocity + acceleration,
                    center_y=D("0.5"),
                    fallback="tracked",
                ),
            )
        }
    )
    measured = validate_crop_track(track, settings=CropSettings())
    assert measured.max_acceleration > D("0.50")
    assert measured.eligible is False
