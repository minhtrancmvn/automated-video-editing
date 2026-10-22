"""Tracking loss must not turn a moving crop into an instantaneous stop."""

from decimal import Decimal as D

from test_tracking import _raw_track

from video_editor.analysis.models import CropKeyframe
from video_editor.analysis.tracking import smooth_crop_track, validate_crop_track
from video_editor.config import CropSettings


def test_tracking_loss_decelerates_within_existing_motion_limits() -> None:
    frames = []
    for i in range(60):
        frames.append(
            CropKeyframe(
                time=D(i) / D(15),
                center_x=D("0.8"),
                center_y=D("0.5"),
                subject_box_id="box" if i < 8 else None,
                fallback="tracked" if i < 8 else "hold",
            )
        )
    frames[0] = frames[0].model_copy(update={"center_x": D("0.3")})
    raw = _raw_track(*frames)
    smoothed = smooth_crop_track(raw, CropSettings())
    evidence = validate_crop_track(smoothed, settings=CropSettings())
    assert evidence.max_velocity <= D("0.25")
    assert evidence.max_acceleration <= D("0.50")
    # Losing a subject must not move toward the distant requested center.
    assert smoothed.keyframes[-1].center_x < D("0.4")
    assert raw.keyframes[0].center_x == D("0.3")
