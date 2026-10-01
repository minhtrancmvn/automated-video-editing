"""Deterministic local subject tracking and validated vertical crop paths."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Literal, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from video_editor.analysis.models import (
    CropKeyframe,
    CropTrack,
    CropValidation,
    NormalizedSubjectBox,
    SubjectObservation,
)
from video_editor.config import CropSettings
from video_editor.media.proxies import ProxyMapping

_ZERO = Decimal(0)
_ONE = Decimal(1)
_HALF = Decimal("0.5")
_MOTION_EPSILON = Decimal("1e-24")
_FB_ERROR_LIMIT = 1.5
_MIN_TRACKED_POINTS = 3


def _crop_dimensions(
    source_size: tuple[int, int], output_size: tuple[int, int]
) -> tuple[int, int]:
    source_width, source_height = source_size
    output_width, output_height = output_size
    if min(source_width, source_height, output_width, output_height) <= 0:
        raise ValueError("source and output dimensions must be positive")
    output_aspect = Decimal(output_width) / Decimal(output_height)
    source_aspect = Decimal(source_width) / Decimal(source_height)
    if source_aspect >= output_aspect:
        crop_height = source_height
        crop_width = max(
            1, int((Decimal(crop_height) * output_aspect).to_integral_value())
        )
    else:
        crop_width = source_width
        crop_height = max(
            1, int((Decimal(crop_width) / output_aspect).to_integral_value())
        )
    return min(crop_width, source_width), min(crop_height, source_height)


def _clamp(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return min(upper, max(lower, value))


def _center_bounds(track: CropTrack) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    half_width = Decimal(track.crop_width) / Decimal(track.source_width) / 2
    half_height = Decimal(track.crop_height) / Decimal(track.source_height) / 2
    return half_width, _ONE - half_width, half_height, _ONE - half_height


def _track_id(clip_id: str, source_identity: str) -> str:
    digest = hashlib.sha256(f"{clip_id}|{source_identity}".encode()).hexdigest()[:20]
    return f"crop-{digest}"


def _read_frames(proxy: Path) -> tuple[list[NDArray[np.uint8]], Decimal]:
    capture = cv2.VideoCapture(str(proxy))
    if not capture.isOpened():
        raise ValueError(f"cannot decode proxy: {proxy}")
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        if not np.isfinite(fps) or fps <= 0:
            raise ValueError("proxy has invalid frame timing")
        frames: list[NDArray[np.uint8]] = []
        while True:
            ok, decoded = capture.read()
            if not ok:
                break
            frames.append(cast(NDArray[np.uint8], decoded))
    finally:
        capture.release()
    if not frames:
        raise ValueError("proxy contains no decodable frames")
    return frames, Decimal(str(fps))


def _source_time(frame_index: int, fps: Decimal, mapping: ProxyMapping) -> Decimal:
    proxy_time = mapping.proxy_start + Decimal(frame_index) / fps
    proxy_duration = mapping.proxy_end - mapping.proxy_start
    source_duration = mapping.source_end - mapping.source_start
    return mapping.source_start + (
        (proxy_time - mapping.proxy_start) * source_duration / proxy_duration
    )


def _seed_at_frame(
    observations: Sequence[SubjectObservation],
    frame_index: int,
    fps: Decimal,
    mapping: ProxyMapping,
) -> SubjectObservation | None:
    frame_source_time = _source_time(frame_index, fps, mapping)
    source_frame_duration = (
        (mapping.source_end - mapping.source_start)
        / (mapping.proxy_end - mapping.proxy_start)
        / fps
    )
    half_frame = source_frame_duration / 2
    matching = [
        item
        for item in observations
        if abs(item.source_time - frame_source_time) <= half_frame
    ]
    if not matching:
        return None
    return min(
        matching,
        key=lambda item: (-item.priority, item.observation_id),
    )


def _feature_points(
    gray: NDArray[np.uint8], box: NormalizedSubjectBox
) -> NDArray[np.float32] | None:
    height, width = gray.shape
    x_min = max(0, min(width - 1, int(box.x_min * width)))
    x_max = max(x_min + 1, min(width, int(box.x_max * width)))
    y_min = max(0, min(height - 1, int(box.y_min * height)))
    y_max = max(y_min + 1, min(height, int(box.y_max * height)))
    mask = np.zeros_like(gray)
    mask[y_min:y_max, x_min:x_max] = 255
    return cast(
        NDArray[np.float32] | None,
        cv2.goodFeaturesToTrack(
            gray,
            maxCorners=80,
            qualityLevel=0.01,
            minDistance=3,
            mask=mask,
        ),
    )


def _propagate_points(
    previous_gray: NDArray[np.uint8],
    gray: NDArray[np.uint8],
    points: NDArray[np.float32],
) -> tuple[NDArray[np.float32], NDArray[np.float32]] | None:
    forward, status, _ = cv2.calcOpticalFlowPyrLK(
        previous_gray, gray, points, np.empty_like(points)
    )
    if forward is None or status is None:
        return None
    backward, backward_status, _ = cv2.calcOpticalFlowPyrLK(
        gray, previous_gray, forward, np.empty_like(forward)
    )
    if backward is None or backward_status is None:
        return None
    fb_error = np.linalg.norm(points.reshape(-1, 2) - backward.reshape(-1, 2), axis=1)
    valid = (
        (status.ravel() == 1)
        & (backward_status.ravel() == 1)
        & (fb_error <= _FB_ERROR_LIMIT)
    )
    if np.count_nonzero(valid) < _MIN_TRACKED_POINTS:
        return None
    previous_valid = points.reshape(-1, 2)[valid].astype(np.float32)
    current_valid = forward.reshape(-1, 2)[valid].astype(np.float32)
    transform, inliers = cv2.estimateAffinePartial2D(
        previous_valid,
        current_valid,
        method=cv2.RANSAC,
        ransacReprojThreshold=2.0,
    )
    if transform is None or inliers is None:
        return None
    accepted = inliers.ravel() == 1
    if np.count_nonzero(accepted) < _MIN_TRACKED_POINTS:
        return None
    return previous_valid[accepted], current_valid[accepted].reshape(-1, 1, 2)


def _shift_box(
    box: NormalizedSubjectBox,
    previous_points: NDArray[np.float32],
    current_points: NDArray[np.float32],
    frame_size: tuple[int, int],
) -> NormalizedSubjectBox | None:
    width, height = frame_size
    delta = np.median(current_points.reshape(-1, 2) - previous_points, axis=0)
    dx = Decimal(str(float(delta[0]))) / Decimal(width)
    dy = Decimal(str(float(delta[1]))) / Decimal(height)
    x_min = box.x_min + dx
    x_max = box.x_max + dx
    y_min = box.y_min + dy
    y_max = box.y_max + dy
    if x_min < 0 or y_min < 0 or x_max > 1 or y_max > 1:
        return None
    return NormalizedSubjectBox(
        x_min=x_min,
        y_min=y_min,
        x_max=x_max,
        y_max=y_max,
    )


def _local_observation(
    frame_index: int,
    fps: Decimal,
    mapping: ProxyMapping,
    seed: SubjectObservation,
    box: NormalizedSubjectBox,
) -> SubjectObservation:
    source_time = _source_time(frame_index, fps, mapping)
    return SubjectObservation(
        observation_id=f"local-{frame_index:08d}-{seed.observation_id}",
        source_id=mapping.source_id,
        source_identity=mapping.source_identity,
        source_time=source_time,
        box=box,
        priority=seed.priority,
        origin="local_track",
        seed_observation_id=seed.observation_id,
    )


def _raw_keyframes(
    observations: Sequence[SubjectObservation],
    *,
    candidate_start: Decimal,
    candidate_end: Decimal,
    fps: Decimal,
    settings: CropSettings,
) -> tuple[CropKeyframe, ...]:
    by_frame = {
        int((item.source_time - candidate_start) * fps): item for item in observations
    }
    frame_count = max(1, int((candidate_end - candidate_start) * fps))
    last: SubjectObservation | None = None
    loss_time: Decimal | None = None
    result: list[CropKeyframe] = []
    for frame_index in range(frame_count):
        clip_time = Decimal(frame_index) / fps
        observed = by_frame.get(frame_index)
        fallback: Literal["tracked", "hold", "ease_center", "static"]
        if observed is not None:
            last = observed
            loss_time = None
            center_x = observed.box.center_x
            center_y = observed.box.center_y
            fallback = "tracked"
            subject_box_id: str | None = observed.observation_id
        elif last is not None:
            if loss_time is None:
                loss_time = clip_time
            lost_for = clip_time - loss_time
            if lost_for <= settings.max_fallback_hold_seconds:
                center_x = last.box.center_x
                center_y = last.box.center_y
                fallback = "hold"
            else:
                progress = min(
                    _ONE,
                    (lost_for - settings.max_fallback_hold_seconds)
                    / settings.max_fallback_hold_seconds,
                )
                center_x = last.box.center_x + (_HALF - last.box.center_x) * progress
                center_y = last.box.center_y + (_HALF - last.box.center_y) * progress
                fallback = "ease_center"
            subject_box_id = None
        else:
            center_x = _HALF
            center_y = _HALF
            fallback = "ease_center"
            subject_box_id = None
        result.append(
            CropKeyframe(
                time=clip_time,
                center_x=center_x,
                center_y=center_y,
                subject_box_id=subject_box_id,
                fallback=fallback,
            )
        )
    return tuple(result)


def smooth_crop_track(track: CropTrack, settings: CropSettings) -> CropTrack:
    """Clamp crop centers and enforce configured velocity and acceleration limits."""
    if not track.keyframes:
        return track
    min_x, max_x, min_y, max_y = _center_bounds(track)
    smoothed: list[CropKeyframe] = []
    previous_velocity_x = _ZERO
    previous_velocity_y = _ZERO
    for keyframe in track.keyframes:
        target_x = _clamp(keyframe.center_x, min_x, max_x)
        target_y = _clamp(keyframe.center_y, min_y, max_y)
        if not smoothed:
            center_x, center_y = target_x, target_y
        else:
            previous = smoothed[-1]
            elapsed = keyframe.time - previous.time
            if elapsed <= 0:
                raise ValueError("crop keyframe times must be strictly increasing")
            if keyframe.fallback == "hold":
                center_x, center_y = previous.center_x, previous.center_y
                previous_velocity_x = _ZERO
                previous_velocity_y = _ZERO
            else:
                width_ratio = Decimal(track.crop_width) / Decimal(track.source_width)
                height_ratio = Decimal(track.crop_width) / Decimal(track.source_height)
                max_velocity_x = (
                    settings.max_velocity_widths_per_second * width_ratio
                    - _MOTION_EPSILON
                )
                max_velocity_y = (
                    settings.max_velocity_widths_per_second * height_ratio
                    - _MOTION_EPSILON
                )
                max_acceleration_x = (
                    settings.max_acceleration_widths_per_second_squared * width_ratio
                )
                max_acceleration_y = (
                    settings.max_acceleration_widths_per_second_squared * height_ratio
                )
                requested_velocity_x = (target_x - previous.center_x) / elapsed
                requested_velocity_y = (target_y - previous.center_y) / elapsed
                velocity_x = _clamp(
                    requested_velocity_x,
                    previous_velocity_x - max_acceleration_x * elapsed,
                    previous_velocity_x + max_acceleration_x * elapsed,
                )
                velocity_y = _clamp(
                    requested_velocity_y,
                    previous_velocity_y - max_acceleration_y * elapsed,
                    previous_velocity_y + max_acceleration_y * elapsed,
                )
                velocity_x = _clamp(velocity_x, -max_velocity_x, max_velocity_x)
                velocity_y = _clamp(velocity_y, -max_velocity_y, max_velocity_y)
                center_x = _clamp(
                    previous.center_x + velocity_x * elapsed, min_x, max_x
                )
                center_y = _clamp(
                    previous.center_y + velocity_y * elapsed, min_y, max_y
                )
                previous_velocity_x = (center_x - previous.center_x) / elapsed
                previous_velocity_y = (center_y - previous.center_y) / elapsed
        smoothed.append(
            keyframe.model_copy(update={"center_x": center_x, "center_y": center_y})
        )
    return track.model_copy(update={"keyframes": tuple(smoothed)})


def _kinematics(track: CropTrack) -> tuple[Decimal, Decimal]:
    velocities: list[tuple[Decimal, Decimal, Decimal]] = []
    width_ratio = Decimal(track.crop_width) / Decimal(track.source_width)
    for previous, current in zip(track.keyframes, track.keyframes[1:]):
        elapsed = current.time - previous.time
        if elapsed <= 0:
            continue
        vx = abs(current.center_x - previous.center_x) / elapsed / width_ratio
        vy = abs(current.center_y - previous.center_y) / elapsed / width_ratio
        velocities.append((current.time, vx, vy))
    accelerations: list[Decimal] = []
    for previous_velocity, current_velocity in pairwise(velocities):
        elapsed = current_velocity[0] - previous_velocity[0]
        accelerations.extend(
            (
                abs(current_velocity[1] - previous_velocity[1]) / elapsed,
                abs(current_velocity[2] - previous_velocity[2]) / elapsed,
            )
        )
    max_velocity = max(
        (value for _, vx, vy in velocities for value in (vx, vy)), default=_ZERO
    )
    max_acceleration = max(accelerations, default=_ZERO)
    return max_velocity, max_acceleration


def _keyframe_for_observation(
    track: CropTrack, observation: SubjectObservation
) -> CropKeyframe | None:
    clip_time = observation.source_time - min(
        (item.source_time for item in track.observations),
        default=observation.source_time,
    )
    return min(
        track.keyframes,
        key=lambda item: (abs(item.time - clip_time), item.time),
        default=None,
    )


def _retains(
    track: CropTrack,
    keyframe: CropKeyframe,
    observation: SubjectObservation,
    safe_margin: Decimal,
) -> bool:
    crop_width = Decimal(track.crop_width) / Decimal(track.source_width)
    crop_height = Decimal(track.crop_height) / Decimal(track.source_height)
    inset_x = crop_width * safe_margin
    inset_y = crop_height * safe_margin
    left = keyframe.center_x - crop_width / 2 + inset_x
    right = keyframe.center_x + crop_width / 2 - inset_x
    top = keyframe.center_y - crop_height / 2 + inset_y
    bottom = keyframe.center_y + crop_height / 2 - inset_y
    box = observation.box
    return (
        left <= box.x_min
        and box.x_max <= right
        and top <= box.y_min
        and box.y_max <= bottom
    )


def validate_crop_track(
    track: CropTrack,
    subject_observations: Sequence[SubjectObservation] | None = None,
    source_size: tuple[int, int] | None = None,
    settings: CropSettings | None = None,
) -> CropValidation:
    """Measure identity, geometry, motion limits, and local-box retention."""
    active = settings or CropSettings()
    observations = tuple(subject_observations or track.observations)
    local = tuple(item for item in observations if item.origin == "local_track")
    expected_size = source_size or (track.source_width, track.source_height)
    identity_matches = expected_size == (
        track.source_width,
        track.source_height,
    ) and all(
        item.source_id == track.source_id
        and item.source_identity == track.source_identity
        for item in observations
    )
    monotonic = all(
        current.time > previous.time
        for previous, current in zip(track.keyframes, track.keyframes[1:])
    )
    min_x, max_x, min_y, max_y = _center_bounds(track)
    bounded = all(
        min_x <= item.center_x <= max_x and min_y <= item.center_y <= max_y
        for item in track.keyframes
    )
    retained = sum(
        bool(
            keyframe
            and _retains(track, keyframe, observation, active.safe_margin_ratio)
        )
        for observation in local
        if (keyframe := _keyframe_for_observation(track, observation)) is not None
    )
    sampled = len(local)
    retention_ratio = Decimal(retained) / Decimal(sampled) if sampled else _ZERO
    max_velocity, max_acceleration = _kinematics(track)
    eligible = (
        bool(sampled)
        and identity_matches
        and monotonic
        and bounded
        and retention_ratio >= active.minimum_subject_retention_ratio
        and max_velocity <= active.max_velocity_widths_per_second
        and max_acceleration <= active.max_acceleration_widths_per_second_squared
    )
    return CropValidation(
        sampled_observations=sampled,
        retained_samples=retained,
        retention_ratio=retention_ratio,
        max_velocity=max_velocity,
        max_acceleration=max_acceleration,
        monotonic=monotonic,
        bounded=bounded,
        identity_matches=identity_matches,
        eligible=eligible,
    )


def best_static_crop(track: CropTrack, settings: CropSettings) -> CropTrack:
    """Choose deterministic static center maximizing validated local retention."""
    local = tuple(item for item in track.observations if item.origin == "local_track")
    if not local:
        return track.model_copy(update={"keyframes": (), "short_eligible": False})
    min_x, max_x, min_y, max_y = _center_bounds(track)
    candidates = sorted(
        {
            (
                _clamp(item.box.center_x, min_x, max_x),
                _clamp(item.box.center_y, min_y, max_y),
            )
            for item in local
        }
        | {(_HALF, _HALF)},
        key=lambda center: (abs(center[0] - _HALF), abs(center[1] - _HALF), center),
    )
    times = sorted({item.source_time for item in local})
    origin = times[0]
    best_track: CropTrack | None = None
    best_retained = -1
    for center_x, center_y in candidates:
        keyframes = tuple(
            CropKeyframe(
                time=time - origin,
                center_x=center_x,
                center_y=center_y,
                subject_box_id=None,
                fallback="static",
            )
            for time in times
        )
        candidate = track.model_copy(
            update={"keyframes": keyframes, "short_eligible": False}
        )
        evidence = validate_crop_track(candidate, settings=settings)
        if evidence.retained_samples > best_retained:
            best_track = candidate
            best_retained = evidence.retained_samples
    if best_track is None:
        return track.model_copy(update={"keyframes": (), "short_eligible": False})
    valid = validate_crop_track(best_track, settings=settings).eligible
    return best_track.model_copy(update={"short_eligible": valid})


def track_subject(
    proxy: Path,
    mapping: ProxyMapping,
    *,
    clip_id: str,
    candidate_start: Decimal,
    candidate_end: Decimal,
    subject_observations: Sequence[SubjectObservation],
    source_size: tuple[int, int],
    output_size: tuple[int, int],
    settings: CropSettings,
) -> CropTrack:
    """Track seeded subjects locally, smooth crop centers, and validate fallback."""
    if candidate_end <= candidate_start:
        raise ValueError("candidate end must be greater than start")
    if candidate_start < mapping.source_start or candidate_end > mapping.source_end:
        raise ValueError("candidate range must fit proxy mapping")
    if any(
        item.source_id != mapping.source_id
        or item.source_identity != mapping.source_identity
        for item in subject_observations
    ):
        raise ValueError("subject observation source identity does not match mapping")
    crop_width, crop_height = _crop_dimensions(source_size, output_size)
    base = CropTrack(
        track_id=_track_id(clip_id, mapping.source_identity),
        clip_id=clip_id,
        source_id=mapping.source_id,
        source_identity=mapping.source_identity,
        source_width=source_size[0],
        source_height=source_size[1],
        output_width=output_size[0],
        output_height=output_size[1],
        crop_width=crop_width,
        crop_height=crop_height,
        observations=(),
        keyframes=(),
        short_eligible=False,
    )
    if not subject_observations:
        return base
    frames, fps = _read_frames(proxy)
    decoded_proxy_end = mapping.proxy_start + Decimal(len(frames)) / fps
    candidate_proxy_end = mapping.proxy_start + (
        (candidate_end - mapping.source_start)
        * (mapping.proxy_end - mapping.proxy_start)
        / (mapping.source_end - mapping.source_start)
    )
    if decoded_proxy_end < candidate_proxy_end:
        return base
    active_seed: SubjectObservation | None = None
    active_box: NormalizedSubjectBox | None = None
    points: NDArray[np.float32] | None = None
    previous_gray: NDArray[np.uint8] | None = None
    local: list[SubjectObservation] = []
    for frame_index, frame in enumerate(frames):
        frame_source_time = _source_time(frame_index, fps, mapping)
        if frame_source_time < candidate_start or frame_source_time >= candidate_end:
            previous_gray = cast(
                NDArray[np.uint8], cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            )
            continue
        gray = cast(NDArray[np.uint8], cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        seed = _seed_at_frame(subject_observations, frame_index, fps, mapping)
        propagated_successfully = False
        if seed is not None:
            active_seed = seed
            active_box = seed.box
            points = _feature_points(gray, active_box)
        elif (
            active_seed is not None
            and active_box is not None
            and points is not None
            and previous_gray is not None
        ):
            propagated = _propagate_points(previous_gray, gray, points)
            if propagated is None:
                points = None
                active_box = None
            else:
                previous_points, current_points = propagated
                active_box = _shift_box(
                    active_box,
                    previous_points,
                    current_points,
                    (gray.shape[1], gray.shape[0]),
                )
                points = current_points if active_box is not None else None
                propagated_successfully = active_box is not None
        if (
            propagated_successfully
            and active_seed is not None
            and active_box is not None
            and points is not None
        ):
            local.append(
                _local_observation(
                    frame_index,
                    fps,
                    mapping,
                    active_seed,
                    active_box,
                )
            )
        previous_gray = gray
    tracked = base.model_copy(
        update={
            "observations": tuple(local),
            "keyframes": _raw_keyframes(
                local,
                candidate_start=candidate_start,
                candidate_end=candidate_end,
                fps=fps,
                settings=settings,
            ),
        }
    )
    smoothed = smooth_crop_track(tracked, settings)
    if validate_crop_track(smoothed, settings=settings).eligible:
        return smoothed.model_copy(update={"short_eligible": True})
    static = best_static_crop(smoothed, settings)
    if static.short_eligible:
        return static
    return smoothed.model_copy(update={"short_eligible": False})
