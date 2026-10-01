from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from video_editor.analysis.tracking import (
    best_static_crop,
    smooth_crop_track,
    track_subject,
    validate_crop_track,
)

from video_editor.analysis.models import (
    CropKeyframe,
    CropTrack,
    NormalizedSubjectBox,
    SubjectObservation,
)
from video_editor.config import CropSettings
from video_editor.media.proxies import ProxyMapping

_fixture_spec = importlib.util.spec_from_file_location(
    "video_editor_test_fixtures", Path(__file__).parents[1] / "fixtures.py"
)
assert _fixture_spec is not None and _fixture_spec.loader is not None
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)
create_tracking_fixture = _fixture_module.create_tracking_fixture

D = Decimal
SOURCE_SIZE = (1920, 1080)
OUTPUT_SIZE = (1080, 1920)
IDENTITY = "bounded-v1:tracking-fixture"


def _mapping(*, source_identity: str = IDENTITY) -> ProxyMapping:
    return ProxyMapping(
        source_id="source-1",
        source_start=D("100"),
        source_end=D("106"),
        proxy_start=D("0"),
        proxy_end=D("6"),
        source_identity=source_identity,
        settings_hash="proxy-settings-hash",
        tool_version="fixture-v1",
    )


def _subject(
    observation_id: str,
    time: str,
    x_min: str,
    x_max: str,
    *,
    priority: int = 1,
    source_identity: str = IDENTITY,
    origin: str = "gemini_seed",
) -> SubjectObservation:
    return SubjectObservation(
        observation_id=observation_id,
        source_id="source-1",
        source_identity=source_identity,
        source_time=D(time),
        box=NormalizedSubjectBox(
            x_min=D(x_min),
            y_min=D("0.30"),
            x_max=D(x_max),
            y_max=D("0.62"),
        ),
        priority=priority,
        origin=origin,
    )


def _raw_track(*keyframes: CropKeyframe) -> CropTrack:
    return CropTrack(
        track_id="track-1",
        clip_id="clip-1",
        source_id="source-1",
        source_identity=IDENTITY,
        source_width=SOURCE_SIZE[0],
        source_height=SOURCE_SIZE[1],
        output_width=OUTPUT_SIZE[0],
        output_height=OUTPUT_SIZE[1],
        crop_width=608,
        crop_height=1080,
        observations=(),
        keyframes=keyframes,
        short_eligible=False,
    )


def test_tracker_records_local_boxes_instead_of_treating_gemini_seed_as_proof(
    tmp_path: Path,
) -> None:
    proxy = create_tracking_fixture(tmp_path)

    track = track_subject(
        proxy,
        _mapping(),
        clip_id="clip-1",
        candidate_start=D("100"),
        candidate_end=D("106"),
        subject_observations=(_subject("seed-1", "100", "0.08", "0.25", priority=2),),
        source_size=SOURCE_SIZE,
        output_size=OUTPUT_SIZE,
        settings=CropSettings(),
    )

    assert track.observations
    assert all(item.origin == "local_track" for item in track.observations)
    assert {item.observation_id for item in track.observations}.isdisjoint({"seed-1"})


def test_tracker_reanchors_to_highest_priority_conflicting_box(tmp_path: Path) -> None:
    proxy = create_tracking_fixture(tmp_path)

    track = track_subject(
        proxy,
        _mapping(),
        clip_id="clip-1",
        candidate_start=D("100"),
        candidate_end=D("106"),
        subject_observations=(
            _subject("wrong", "100", "0.70", "0.90", priority=1),
            _subject("subject", "100", "0.08", "0.25", priority=5),
        ),
        source_size=SOURCE_SIZE,
        output_size=OUTPUT_SIZE,
        settings=CropSettings(),
    )

    assert track.observations[0].box.center_x < D("0.30")
    assert track.observations[0].seed_observation_id == "subject"


def test_fast_raw_track_is_smoothed_below_velocity_and_acceleration_limits() -> None:
    raw = _raw_track(
        CropKeyframe(
            time=D("0"),
            center_x=D("0.2"),
            center_y=D("0.5"),
            subject_box_id="box-1",
            fallback="tracked",
        ),
        CropKeyframe(
            time=D("1"),
            center_x=D("0.8"),
            center_y=D("0.5"),
            subject_box_id="box-2",
            fallback="tracked",
        ),
        CropKeyframe(
            time=D("2"),
            center_x=D("0.2"),
            center_y=D("0.5"),
            subject_box_id="box-3",
            fallback="tracked",
        ),
    )

    track = smooth_crop_track(raw, CropSettings())
    evidence = validate_crop_track(track, settings=CropSettings())

    assert evidence.max_velocity <= D("0.25")
    assert evidence.max_acceleration <= D("0.50")


def test_occlusion_holds_then_eases_center_without_jump(tmp_path: Path) -> None:
    proxy = create_tracking_fixture(tmp_path, occlusion=(20, 50))

    track = track_subject(
        proxy,
        _mapping(),
        clip_id="clip-1",
        candidate_start=D("100"),
        candidate_end=D("106"),
        subject_observations=(_subject("seed-1", "100", "0.08", "0.25", priority=2),),
        source_size=SOURCE_SIZE,
        output_size=OUTPUT_SIZE,
        settings=CropSettings(),
    )
    loss_index = next(
        index
        for index, keyframe in enumerate(track.keyframes)
        if keyframe.fallback == "hold"
    )
    after_loss = track.keyframes[loss_index:]
    hold_times = [item.time for item in after_loss if item.fallback == "hold"]

    assert max(hold_times) - min(hold_times) <= D("2")
    assert after_loss[0].fallback == "hold"
    assert "ease_center" in [item.fallback for item in after_loss]
    assert after_loss[0].center_x == track.keyframes[loss_index - 1].center_x


def test_validation_requires_95_percent_local_retention_inside_five_percent_margin() -> (
    None
):
    observations = tuple(
        _subject(
            f"local-{index}",
            str(100 + index),
            "0.42" if index < 19 else "0.80",
            "0.52" if index < 19 else "0.90",
            origin="local_track",
        )
        for index in range(20)
    )
    track = _raw_track(
        *(
            CropKeyframe(
                time=D(index),
                center_x=D("0.47"),
                center_y=D("0.5"),
                subject_box_id=observation.observation_id,
                fallback="tracked",
            )
            for index, observation in enumerate(observations)
        )
    ).model_copy(update={"observations": observations})

    evidence = validate_crop_track(track, settings=CropSettings())

    assert evidence.retained_samples == 19
    assert evidence.sampled_observations == 20
    assert evidence.retention_ratio == D("0.95")
    assert evidence.eligible is True


def test_crop_dimensions_centers_timestamps_and_bounds_are_deterministic() -> None:
    keyframes = (
        CropKeyframe(
            time=D("0"),
            center_x=D("0"),
            center_y=D("1"),
            subject_box_id=None,
            fallback="ease_center",
        ),
        CropKeyframe(
            time=D("1"),
            center_x=D("1"),
            center_y=D("0"),
            subject_box_id=None,
            fallback="ease_center",
        ),
    )

    first = smooth_crop_track(_raw_track(*keyframes), CropSettings())
    second = smooth_crop_track(_raw_track(*keyframes), CropSettings())

    assert (first.crop_width, first.crop_height) == (608, 1080)
    assert first == second
    assert [item.time for item in first.keyframes] == sorted(
        item.time for item in first.keyframes
    )
    half_crop_width = D(first.crop_width) / D(first.source_width) / 2
    half_crop_height = D(first.crop_height) / D(first.source_height) / 2
    assert all(
        half_crop_width <= item.center_x <= 1 - half_crop_width
        and half_crop_height <= item.center_y <= 1 - half_crop_height
        for item in first.keyframes
    )


def test_tracker_rejects_source_identity_mismatch(tmp_path: Path) -> None:
    proxy = create_tracking_fixture(tmp_path)

    with pytest.raises(ValueError, match="source identity"):
        track_subject(
            proxy,
            _mapping(),
            clip_id="clip-1",
            candidate_start=D("100"),
            candidate_end=D("106"),
            subject_observations=(
                _subject(
                    "seed-1",
                    "100",
                    "0.08",
                    "0.25",
                    source_identity="bounded-v1:other",
                ),
            ),
            source_size=SOURCE_SIZE,
            output_size=OUTPUT_SIZE,
            settings=CropSettings(),
        )


def test_empty_detections_are_short_ineligible(tmp_path: Path) -> None:
    proxy = create_tracking_fixture(tmp_path)

    track = track_subject(
        proxy,
        _mapping(),
        clip_id="clip-1",
        candidate_start=D("100"),
        candidate_end=D("106"),
        subject_observations=(),
        source_size=SOURCE_SIZE,
        output_size=OUTPUT_SIZE,
        settings=CropSettings(),
    )

    assert track.short_eligible is False
    assert track.observations == ()


def test_best_static_fallback_is_deterministic_and_validated() -> None:
    observations = tuple(
        _subject(
            f"local-{index}",
            str(100 + index),
            "0.42",
            "0.52",
            origin="local_track",
        )
        for index in range(4)
    )
    track = _raw_track().model_copy(update={"observations": observations})

    first = best_static_crop(track, CropSettings())
    second = best_static_crop(track, CropSettings())

    assert first == second
    assert first.short_eligible is True
    assert {item.fallback for item in first.keyframes} == {"static"}
    assert validate_crop_track(first, settings=CropSettings()).eligible is True


def test_static_fallback_marks_short_ineligible_when_margin_cannot_retain_subject() -> (
    None
):
    observations = (
        _subject(
            "too-wide",
            "100",
            "0.05",
            "0.95",
            origin="local_track",
        ),
    )
    track = _raw_track().model_copy(update={"observations": observations})

    result = best_static_crop(track, CropSettings())

    assert result.short_eligible is False
    assert validate_crop_track(result, settings=CropSettings()).eligible is False
