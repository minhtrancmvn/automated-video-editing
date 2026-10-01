"""Validate rendered media before final artifact publication."""

from __future__ import annotations

import subprocess
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray

from video_editor.analysis.models import CropTrack
from video_editor.analysis.tracking import validate_crop_track
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.probe import MediaProbe, probe_media
from video_editor.models.edit_plan import (
    EditPlanV2,
    OutputSpec,
    OutputSpecV2,
    TimelineClipV2,
    timeline_duration,
)

_ZERO = Decimal(0)
_ONE = Decimal(1)
_MINIMUM_RETENTION = Decimal("0.95")
_FRAME_SAMPLE_COUNT = 12
_BLACK_LEVEL = 18.0
_CONTENT_LEVEL = 25.0
_AUDIO_WINDOW_SECONDS = Decimal("0.20")
_AUDIO_RMS_FLOOR = 16.0


@dataclass(frozen=True, slots=True)
class Phase2ValidationWarning:
    """Recoverable phase-2 output concern with supporting timestamps."""

    code: str
    message: str
    timestamps: tuple[Decimal, ...] = ()


@dataclass(frozen=True, slots=True)
class Phase2OutputValidation:
    """Structured probe, crop, frame, and audio validation evidence."""

    probe: MediaProbe
    warnings: tuple[Phase2ValidationWarning, ...]
    crop_track_ids: tuple[str, ...]
    sampled_crop_timestamps: tuple[Decimal, ...]
    subject_retention_ratio: Decimal
    unintended_black_bar_timestamps: tuple[Decimal, ...]
    audio_boundary_timestamps: tuple[Decimal, ...]
    audio_discontinuity_timestamps: tuple[Decimal, ...]


def validate_output(
    path: Path,
    output: OutputSpec | OutputSpecV2,
    expected_duration: Decimal,
    tolerance: Decimal = Decimal("0.20"),
) -> MediaProbe:
    """Probe output and enforce dimensions, duration, and audio policy."""
    if not path.is_file():
        raise VideoEditorError(ErrorCategory.OUTPUT, f"output does not exist: {path}")
    try:
        probe = probe_media(path)
    except VideoEditorError as exc:
        raise VideoEditorError(ErrorCategory.OUTPUT, str(exc)) from exc
    if probe.video is None:
        raise VideoEditorError(
            ErrorCategory.OUTPUT, f"output has no video stream: {path}"
        )
    if output.codec == "libx264" and probe.video.codec_name != "h264":
        raise VideoEditorError(
            ErrorCategory.OUTPUT,
            f"output codec is {probe.video.codec_name}; expected h264 for libx264",
        )
    if (probe.video.width, probe.video.height) != (output.width, output.height):
        raise VideoEditorError(
            ErrorCategory.OUTPUT,
            f"output dimensions are {probe.video.width}x{probe.video.height}; "
            f"expected {output.width}x{output.height}",
        )
    if (
        probe.duration is None
        or abs(Decimal(str(probe.duration)) - expected_duration) > tolerance
    ):
        raise VideoEditorError(
            ErrorCategory.OUTPUT,
            f"output duration is {probe.duration}; expected {expected_duration} ± {tolerance}",
        )
    if output.audio in {"source", "silence"} and probe.audio is None:
        raise VideoEditorError(
            ErrorCategory.OUTPUT,
            f"output must contain audio for {output.audio} policy",
        )
    if output.audio == "none" and probe.audio is not None:
        raise VideoEditorError(ErrorCategory.OUTPUT, "output must not contain audio")
    return probe


def _output_failure(
    message: str,
    code: str,
    timestamps: Sequence[Decimal] = (),
) -> VideoEditorError:
    return VideoEditorError(
        ErrorCategory.OUTPUT,
        message,
        code=code,
        safe_details={
            "timestamps": ",".join(format(timestamp, "f") for timestamp in timestamps)
        },
    )


def _track_for_clip(
    clip: TimelineClipV2,
    tracks: dict[str, CropTrack],
) -> CropTrack:
    track_id = clip.framing.track_id
    if track_id is None or track_id not in tracks:
        raise _output_failure(
            f"crop track evidence does not match planned track for {clip.clip_id}",
            "crop_track_identity_mismatch",
        )
    track = tracks[track_id]
    if (
        track.clip_id != clip.clip_id
        or track.source_id != clip.source_id
        or track.source_identity != clip.source_identity
        or clip.framing.track_clip_id != track.clip_id
        or clip.framing.track_source_identity != track.source_identity
    ):
        raise _output_failure(
            f"crop track identity does not match planned path for {clip.clip_id}",
            "crop_track_identity_mismatch",
        )
    planned_keyframes = tuple(
        (
            keyframe.time,
            keyframe.center_x,
            keyframe.center_y,
            keyframe.subject_box_id,
            keyframe.fallback,
        )
        for keyframe in clip.framing.keyframes
    )
    evidence_keyframes = tuple(
        (
            keyframe.time,
            keyframe.center_x,
            keyframe.center_y,
            keyframe.subject_box_id,
            keyframe.fallback,
        )
        for keyframe in track.keyframes
    )
    if planned_keyframes != evidence_keyframes:
        raise _output_failure(
            f"crop track path does not match planned keyframes for {clip.clip_id}",
            "crop_track_path_mismatch",
            tuple(keyframe.time for keyframe in clip.framing.keyframes),
        )
    return track


def _validate_crop_tracks(
    plan: EditPlanV2,
    crop_track_evidence: Sequence[CropTrack],
) -> tuple[tuple[str, ...], tuple[Decimal, ...], Decimal]:
    tracks = {track.track_id: track for track in crop_track_evidence}
    if len(tracks) != len(crop_track_evidence):
        raise _output_failure(
            "crop track evidence contains duplicate track IDs",
            "duplicate_crop_track_id",
        )
    sampled = 0
    retained = 0
    track_ids: list[str] = []
    timestamps: list[Decimal] = []
    sources = {source.id: source for source in plan.sources}
    for clip in plan.clips:
        if clip.framing.mode != "tracked_crop":
            continue
        track = _track_for_clip(clip, tracks)
        source_probe = probe_media(sources[clip.source_id].path)
        source_size = (
            (source_probe.video.width, source_probe.video.height)
            if source_probe.video is not None
            else (None, None)
        )
        if source_size != (track.source_width, track.source_height):
            raise _output_failure(
                f"crop track source dimensions do not match {clip.source_id}",
                "crop_track_source_mismatch",
            )
        if (track.output_width, track.output_height) != (
            plan.output.width,
            plan.output.height,
        ):
            raise _output_failure(
                f"crop track output dimensions do not match {plan.output.filename}",
                "crop_track_output_mismatch",
            )
        validation = validate_crop_track(track, source_size=source_size)
        if not validation.identity_matches:
            raise _output_failure(
                f"crop track source identity does not match {clip.clip_id}",
                "crop_track_identity_mismatch",
            )
        if not validation.monotonic or not validation.bounded:
            invalid_times = tuple(keyframe.time for keyframe in track.keyframes)
            raise _output_failure(
                f"crop track path is not monotonic and bounded for {clip.clip_id}",
                "crop_track_out_of_bounds",
                invalid_times,
            )
        sampled += validation.sampled_observations
        retained += validation.retained_samples
        track_ids.append(track.track_id)
        timestamps.extend(keyframe.time for keyframe in track.keyframes)
    ratio = Decimal(retained) / Decimal(sampled) if sampled else _ONE
    if track_ids and ratio < _MINIMUM_RETENTION:
        raise _output_failure(
            f"crop subject retention is {ratio}; expected at least {_MINIMUM_RETENTION}",
            "crop_subject_retention",
            timestamps,
        )
    return tuple(track_ids), tuple(timestamps), ratio


def _sample_frames(
    path: Path,
    duration: Decimal,
) -> tuple[tuple[Decimal, NDArray[np.uint8]], ...]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise _output_failure(
            f"cannot decode output frames: {path}", "frame_sampling_failed"
        )
    frames: list[tuple[Decimal, NDArray[np.uint8]]] = []
    try:
        for index in range(_FRAME_SAMPLE_COUNT):
            timestamp = (
                duration * Decimal(2 * index + 1) / Decimal(2 * _FRAME_SAMPLE_COUNT)
            )
            capture.set(cv2.CAP_PROP_POS_MSEC, float(timestamp * 1000))
            ok, decoded = capture.read()
            if ok and decoded is not None:
                frames.append((timestamp, cast(NDArray[np.uint8], decoded)))
    finally:
        capture.release()
    if not frames:
        raise _output_failure(
            f"output has no decodable sampled frames: {path}",
            "frame_sampling_failed",
        )
    return tuple(frames)


def _has_black_bars(frame: NDArray[np.uint8]) -> bool:
    height, width = frame.shape[:2]
    edge_x = max(1, width // 32)
    edge_y = max(1, height // 32)
    center = frame[height // 4 : 3 * height // 4, width // 4 : 3 * width // 4]
    center_level = float(np.mean(center))
    vertical = (
        float(np.mean(frame[:, :edge_x])) <= _BLACK_LEVEL
        and float(np.mean(frame[:, width - edge_x :])) <= _BLACK_LEVEL
    )
    horizontal = (
        float(np.mean(frame[:edge_y, :])) <= _BLACK_LEVEL
        and float(np.mean(frame[height - edge_y :, :])) <= _BLACK_LEVEL
    )
    return center_level >= _CONTENT_LEVEL and (vertical or horizontal)


def _fade_black_windows(plan: EditPlanV2) -> tuple[tuple[Decimal, Decimal], ...]:
    return tuple(
        (
            max(
                _ZERO,
                plan.clips[transition.to_clip].timeline_start - transition.duration,
            ),
            plan.clips[transition.to_clip].timeline_start + transition.duration,
        )
        for transition in plan.transitions
        if transition.kind == "fade_black"
    )


def _unintended_black_bars(
    path: Path,
    plan: EditPlanV2,
) -> tuple[Decimal, ...]:
    intended = _fade_black_windows(plan)
    return tuple(
        timestamp
        for timestamp, frame in _sample_frames(path, timeline_duration(plan))
        if not any(start <= timestamp <= end for start, end in intended)
        and _has_black_bars(frame)
    )


def _audio_rms(path: Path, boundary: Decimal) -> float | None:
    start = max(_ZERO, boundary - _AUDIO_WINDOW_SECONDS / 2)
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            format(start, "f"),
            "-t",
            format(_AUDIO_WINDOW_SECONDS, "f"),
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-",
        ],
        capture_output=True,
        shell=False,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    samples = array("h")
    samples.frombytes(result.stdout)
    if not samples:
        return None
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return float(mean_square**0.5)


def _audio_discontinuities(
    path: Path,
    plan: EditPlanV2,
) -> tuple[tuple[Decimal, ...], tuple[Phase2ValidationWarning, ...]]:
    if plan.output.audio != "source":
        return (), ()
    boundaries = tuple(
        plan.clips[transition.to_clip].timeline_start
        for transition in plan.transitions
        if transition.kind != "fade_black"
    )
    discontinuities: list[Decimal] = []
    warnings: list[Phase2ValidationWarning] = []
    for boundary in boundaries:
        try:
            rms = _audio_rms(path, boundary)
        except OSError as exc:
            warnings.append(
                Phase2ValidationWarning(
                    code="audio_sampling_failed",
                    message=f"could not sample transition audio: {exc}",
                    timestamps=(boundary,),
                )
            )
            continue
        if rms is None:
            warnings.append(
                Phase2ValidationWarning(
                    code="audio_sampling_failed",
                    message="could not decode audio around transition boundary",
                    timestamps=(boundary,),
                )
            )
        elif rms < _AUDIO_RMS_FLOOR:
            discontinuities.append(boundary)
    if discontinuities:
        warnings.append(
            Phase2ValidationWarning(
                code="audio_discontinuity",
                message="source audio is unexpectedly silent around transition boundaries",
                timestamps=tuple(discontinuities),
            )
        )
    return tuple(discontinuities), tuple(warnings)


def validate_phase2_output(
    path: Path,
    plan: EditPlanV2,
    crop_track_evidence: Sequence[CropTrack],
    tolerance: Decimal = Decimal("0.20"),
) -> Phase2OutputValidation:
    """Validate one version-2 render and return timestamped quality evidence."""
    probe = validate_output(path, plan.output, timeline_duration(plan), tolerance)
    track_ids, crop_timestamps, retention = _validate_crop_tracks(
        plan, crop_track_evidence
    )
    black_bar_timestamps = _unintended_black_bars(path, plan)
    warnings: list[Phase2ValidationWarning] = []
    if len(black_bar_timestamps) >= max(2, _FRAME_SAMPLE_COUNT // 2):
        warnings.append(
            Phase2ValidationWarning(
                code="persistent_black_bars",
                message="sampled frames contain persistent unintended black bars",
                timestamps=black_bar_timestamps,
            )
        )
    audio_discontinuities, audio_warnings = _audio_discontinuities(path, plan)
    warnings.extend(audio_warnings)
    boundaries = tuple(
        plan.clips[transition.to_clip].timeline_start for transition in plan.transitions
    )
    return Phase2OutputValidation(
        probe=probe,
        warnings=tuple(warnings),
        crop_track_ids=track_ids,
        sampled_crop_timestamps=crop_timestamps,
        subject_retention_ratio=retention,
        unintended_black_bar_timestamps=black_bar_timestamps,
        audio_boundary_timestamps=boundaries,
        audio_discontinuity_timestamps=audio_discontinuities,
    )
