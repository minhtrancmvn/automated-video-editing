"""Decimal source timestamps associate with nearest clip-local sample."""

from decimal import Decimal as D

import pytest
from test_tracking import _subject

from video_editor.analysis.tracking import TRACKING_VERSION, _raw_keyframes
from video_editor.config import CropSettings


@pytest.mark.parametrize("fps", [15, 30])
@pytest.mark.parametrize("start,index", [("770", 77), ("490", 638), ("245", 776)])
def test_decoded_observation_recovers_frame_after_decimal_roundoff(
    fps: int,
    start: str,
    index: int,
) -> None:
    origin = D(start)
    timestamp = origin + D(index) / D(fps)
    observation = _subject("local", str(timestamp), "0.4", "0.5", origin="local_track")
    frames = _raw_keyframes(
        [observation],
        candidate_start=origin,
        candidate_end=origin + D(index + 3) / D(fps),
        fps=D(fps),
        settings=CropSettings(),
    )
    assert frames[index].subject_box_id == observation.observation_id
    assert frames[index].fallback == "tracked"
    assert frames[index - 1].subject_box_id is None


def test_adjacent_observations_do_not_collide_from_integer_truncation() -> None:
    origin = D(770)
    observations = [
        _subject(
            "first", str(origin + D(76) / D(15)), "0.4", "0.5", origin="local_track"
        ),
        _subject(
            "second", str(origin + D(77) / D(15)), "0.5", "0.6", origin="local_track"
        ),
    ]
    frames = _raw_keyframes(
        observations,
        candidate_start=origin,
        candidate_end=D(782),
        fps=D(15),
        settings=CropSettings(),
    )
    assert frames[76].subject_box_id == "first"
    assert frames[77].subject_box_id == "second"


@pytest.mark.parametrize("timestamp,index", [("0.049", 0), ("0.05", 1), ("0.051", 1)])
def test_non_grid_observation_uses_nearest_frame_with_half_ties_up(
    timestamp: str, index: int
) -> None:
    observation = _subject("local", timestamp, "0.4", "0.5", origin="local_track")
    frames = _raw_keyframes(
        [observation],
        candidate_start=D(0),
        candidate_end=D(1),
        fps=D(10),
        settings=CropSettings(),
    )
    assert frames[index].subject_box_id == "local"
    assert sum(frame.subject_box_id == "local" for frame in frames) == 1


@pytest.mark.parametrize("timestamp", ["99.999", "101", "101.001"])
def test_outside_source_interval_is_excluded_before_rounding(timestamp: str) -> None:
    observation = _subject("outside", timestamp, "0.4", "0.5", origin="local_track")
    frames = _raw_keyframes(
        [observation],
        candidate_start=D(100),
        candidate_end=D(101),
        fps=D(10),
        settings=CropSettings(),
    )
    assert all(frame.subject_box_id is None for frame in frames)
    assert all(frame.fallback == "ease_center" for frame in frames)


def test_frame_assignment_algorithm_has_new_cache_provenance() -> None:
    assert TRACKING_VERSION == "crop-tracking-v5"


def test_exact_interval_start_is_associated_with_first_frame() -> None:
    observation = _subject("start", "100", "0.4", "0.5", origin="local_track")
    frames = _raw_keyframes(
        [observation],
        candidate_start=D(100),
        candidate_end=D(101),
        fps=D(10),
        settings=CropSettings(),
    )
    assert frames[0].subject_box_id == "start"
