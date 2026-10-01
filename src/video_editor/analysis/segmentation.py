"""Deterministic local segmentation over derived analysis media."""

from __future__ import annotations

import hashlib
import json
import wave
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray

from video_editor.analysis.models import (
    BoundarySuitabilityEvidence,
    EvidenceRange,
    IntervalEvidence,
    LocalSegmentation,
    ScoredEvidence,
    SourceRange,
)
from video_editor.media.proxies import ProxyMapping

_IMPLEMENTATION_VERSION = "local-segmentation-v3"
_ZERO = Decimal(0)


@dataclass(frozen=True)
class SegmentationSettings:
    """Versioned thresholds and sampling used by local segmentation."""

    sample_fps: int = 10
    audio_window_seconds: Decimal = Decimal("0.1")
    scene_threshold: float = 0.45
    silence_rms: float = 0.03
    transient_delta: float = 0.15
    blur_variance_threshold: float = 120.0
    exposure_luma_threshold: float = 245.0
    obstruction_luma_threshold: float = 12.0
    obstruction_flat_stddev: float = 8.0
    peak_merge_seconds: Decimal = Decimal("0.4")
    lead_seconds: Decimal = Decimal("0.3")
    resolution_seconds: Decimal = Decimal("0.3")

    def __post_init__(self) -> None:
        """Reject invalid settings before media processing."""
        decimals = (
            self.audio_window_seconds,
            self.peak_merge_seconds,
            self.lead_seconds,
            self.resolution_seconds,
        )
        floats = (
            self.scene_threshold,
            self.silence_rms,
            self.transient_delta,
            self.blur_variance_threshold,
            self.exposure_luma_threshold,
            self.obstruction_luma_threshold,
            self.obstruction_flat_stddev,
        )
        if (
            not np.isfinite(self.sample_fps)
            or any(not value.is_finite() for value in decimals)
            or any(not np.isfinite(value) for value in floats)
        ):
            raise ValueError("segmentation settings must be finite")
        if self.sample_fps <= 0 or self.audio_window_seconds <= 0:
            raise ValueError("segmentation sampling settings must be positive")
        if any(
            not 0 <= value <= 1
            for value in (
                self.scene_threshold,
                self.silence_rms,
                self.transient_delta,
            )
        ):
            raise ValueError("normalized segmentation thresholds must be within 0..1")
        if any(
            not 0 <= value <= 255
            for value in (
                self.exposure_luma_threshold,
                self.obstruction_luma_threshold,
            )
        ):
            raise ValueError("luminance thresholds must be within 0..255")
        if self.blur_variance_threshold <= 0 or self.obstruction_flat_stddev <= 0:
            raise ValueError("segmentation quality thresholds must be positive")
        if any(
            value < 0
            for value in (
                self.peak_merge_seconds,
                self.lead_seconds,
                self.resolution_seconds,
            )
        ):
            raise ValueError("segmentation window settings must be non-negative")


def _settings_hash(settings: SegmentationSettings) -> str:
    payload = json.dumps(
        {
            key: format(value, "f") if isinstance(value, Decimal) else value
            for key, value in asdict(settings).items()
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _clamp(value: float) -> float:
    if not np.isfinite(value):
        raise ValueError("computed score must be finite")
    return float(min(1.0, max(0.0, value)))


def _map_time(value: Decimal, mapping: ProxyMapping) -> Decimal:
    proxy_duration = mapping.proxy_end - mapping.proxy_start
    source_duration = mapping.source_end - mapping.source_start
    if proxy_duration <= 0 or source_duration <= 0:
        raise ValueError("proxy mapping intervals must be positive")
    return mapping.source_start + (
        (value - mapping.proxy_start) * source_duration / proxy_duration
    )


def _evidence_id(
    kind: str,
    proxy_start: Decimal,
    proxy_end: Decimal,
    score: float | None = None,
) -> str:
    payload = f"{kind}|{proxy_start:f}|{proxy_end:f}"
    if score is not None:
        payload += f"|{score:.12f}"
    return f"local-{kind}-{hashlib.sha256(payload.encode()).hexdigest()[:20]}"


def _interval(
    kind: str,
    start: Decimal,
    end: Decimal,
    mapping: ProxyMapping,
) -> IntervalEvidence:
    return IntervalEvidence(
        evidence_id=_evidence_id(kind, start, end),
        proxy_range=EvidenceRange(start=start, end=end),
        source_range=SourceRange(
            source_id=mapping.source_id,
            start=_map_time(start, mapping),
            end=_map_time(end, mapping),
        ),
    )


def _scored(
    kind: str,
    start: Decimal,
    end: Decimal,
    score: float,
    mapping: ProxyMapping,
) -> ScoredEvidence:
    normalized = _clamp(score)
    return ScoredEvidence(
        evidence_id=_evidence_id(kind, start, end, normalized),
        proxy_range=EvidenceRange(start=start, end=end),
        source_range=SourceRange(
            source_id=mapping.source_id,
            start=_map_time(start, mapping),
            end=_map_time(end, mapping),
        ),
        score=normalized,
    )


def _read_video(
    proxy: Path, settings: SegmentationSettings
) -> tuple[list[tuple[Decimal, NDArray[np.uint8]]], Decimal]:
    capture = cv2.VideoCapture(str(proxy))
    if not capture.isOpened():
        raise ValueError(f"cannot decode proxy: {proxy}")
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        if (
            not np.isfinite(fps)
            or fps <= 0
            or not np.isfinite(frame_count)
            or frame_count <= 0
        ):
            raise ValueError("proxy has invalid frame timing")
        duration = Decimal(str(frame_count)) / Decimal(str(fps))
        step = Decimal(1) / Decimal(settings.sample_fps)
        samples: list[tuple[Decimal, NDArray[np.uint8]]] = []
        timestamp = _ZERO
        while timestamp < duration:
            capture.set(cv2.CAP_PROP_POS_MSEC, float(timestamp * 1000))
            ok, decoded = capture.read()
            if not ok:
                break
            frame = cast(NDArray[np.uint8], decoded)
            samples.append((timestamp, frame))
            timestamp += step
    finally:
        capture.release()
    if not samples:
        raise ValueError("proxy contains no decodable frames")
    return samples, duration


def _histogram(frame: NDArray[np.uint8]) -> NDArray[np.float32]:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist(
        [hsv], [0, 1, 2], None, [16, 16, 16], [0, 180, 0, 256, 0, 256]
    )
    normalized = cv2.normalize(histogram, histogram)
    return cast(NDArray[np.float32], normalized.flatten())


def _scene_ranges(
    samples: list[tuple[Decimal, NDArray[np.uint8]]],
    duration: Decimal,
    mapping: ProxyMapping,
    settings: SegmentationSettings,
) -> tuple[IntervalEvidence, ...]:
    boundaries = [mapping.proxy_start]
    previous = _histogram(samples[0][1])
    for timestamp, frame in samples[1:]:
        current = _histogram(frame)
        discontinuity = cv2.compareHist(previous, current, cv2.HISTCMP_BHATTACHARYYA)
        if discontinuity >= settings.scene_threshold:
            boundaries.append(timestamp)
        previous = current
    boundaries.append(duration)
    deduplicated = sorted(set(boundaries))
    return tuple(
        _interval("scene", start, end, mapping)
        for start, end in pairwise(deduplicated)
        if end > start
    )


def _frame_evidence(
    samples: list[tuple[Decimal, NDArray[np.uint8]]],
    duration: Decimal,
    mapping: ProxyMapping,
    settings: SegmentationSettings,
) -> dict[str, tuple[ScoredEvidence, ...]]:
    result: dict[str, list[ScoredEvidence]] = {
        name: []
        for name in (
            "motion",
            "motion-continuity",
            "blur",
            "shake",
            "exposure",
            "obstruction",
        )
    }
    previous_gray: NDArray[np.uint8] | None = None
    previous_motion = 0.0
    step = Decimal(1) / Decimal(settings.sample_fps)
    for timestamp, frame in samples:
        end = min(timestamp + step, duration)
        gray = cast(NDArray[np.uint8], cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        blur_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        blur = 1 - blur_variance / settings.blur_variance_threshold
        mean_luma = float(gray.mean())
        exposure = float(np.mean(gray >= settings.exposure_luma_threshold))
        obstruction = float(
            mean_luma <= settings.obstruction_luma_threshold
            and float(gray.std()) <= settings.obstruction_flat_stddev
        )
        motion = 0.0
        continuity = 0.0
        shake = 0.0
        if previous_gray is not None:
            flow = cv2.calcOpticalFlowFarneback(
                previous_gray,
                gray,
                np.empty((*gray.shape, 2), dtype=np.float32),
                0.5,
                3,
                15,
                3,
                5,
                1.2,
                0,
            )
            magnitude = np.linalg.norm(flow, axis=2)
            motion = _clamp(float(np.mean(magnitude)) / 8)
            continuity = _clamp(1 - abs(motion - previous_motion)) if motion > 0 else 0
            previous_points = cv2.goodFeaturesToTrack(
                previous_gray,
                maxCorners=100,
                qualityLevel=0.01,
                minDistance=5,
            )
            transform = None
            if previous_points is not None and len(previous_points) >= 3:
                current_points, status, _ = cv2.calcOpticalFlowPyrLK(
                    previous_gray,
                    gray,
                    previous_points,
                    np.empty_like(previous_points),
                )
                if current_points is not None and status is not None:
                    valid = status.ravel() == 1
                    if np.count_nonzero(valid) >= 3:
                        transform, _ = cv2.estimateAffinePartial2D(
                            previous_points[valid],
                            current_points[valid],
                            method=cv2.RANSAC,
                            ransacReprojThreshold=2.0,
                        )
            if transform is not None:
                stabilized = cv2.warpAffine(
                    previous_gray,
                    transform,
                    (gray.shape[1], gray.shape[0]),
                )
                shake = _clamp(float(np.mean(cv2.absdiff(stabilized, gray))) / 64)
        values = {
            "motion": motion,
            "motion-continuity": continuity,
            "blur": blur,
            "shake": shake,
            "exposure": exposure,
            "obstruction": obstruction,
        }
        for kind, score in values.items():
            result[kind].append(_scored(kind, timestamp, end, score, mapping))
        previous_gray = gray
        previous_motion = motion
    return {key: tuple(value) for key, value in result.items()}


def _merge_boolean_ranges(
    values: Iterable[tuple[Decimal, Decimal, bool]],
    kind: str,
    mapping: ProxyMapping,
) -> tuple[IntervalEvidence, ...]:
    ranges: list[IntervalEvidence] = []
    active_start: Decimal | None = None
    active_end: Decimal | None = None
    for start, end, active in values:
        if active and active_start is None:
            active_start = start
        if active:
            active_end = end
        elif active_start is not None and active_end is not None:
            ranges.append(_interval(kind, active_start, active_end, mapping))
            active_start = None
            active_end = None
    if active_start is not None and active_end is not None:
        ranges.append(_interval(kind, active_start, active_end, mapping))
    return tuple(ranges)


def _audio_evidence(
    audio: Path | None,
    mapping: ProxyMapping,
    settings: SegmentationSettings,
    proxy_offset: Decimal = _ZERO,
) -> tuple[
    tuple[ScoredEvidence, ...],
    tuple[IntervalEvidence, ...],
    tuple[IntervalEvidence, ...],
    tuple[ScoredEvidence, ...],
]:
    if audio is None:
        return (), (), (), ()
    try:
        with wave.open(str(audio), "rb") as source:
            if source.getnchannels() != 1 or source.getsampwidth() != 2:
                raise ValueError("analysis audio must be mono 16-bit PCM WAV")
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            samples = np.frombuffer(source.readframes(frame_count), dtype="<i2")
    except (EOFError, wave.Error) as error:
        raise ValueError(f"cannot decode analysis audio: {audio}") from error
    if sample_rate <= 0:
        raise ValueError("analysis audio has invalid sample rate")
    if samples.size != frame_count:
        raise ValueError("analysis audio decoded sample count does not match header")
    if frame_count <= 0 or samples.size == 0:
        raise ValueError("analysis audio contains no samples")
    audio_duration = Decimal(frame_count) / Decimal(sample_rate)
    mapped_duration = mapping.proxy_end - mapping.proxy_start
    sample_tolerance = Decimal(1) / Decimal(sample_rate)
    if (
        audio_duration > mapped_duration
        or mapped_duration - audio_duration > sample_tolerance
    ):
        raise ValueError("analysis audio duration does not match mapping")
    window_samples = max(1, int(sample_rate * settings.audio_window_seconds))
    energy: list[ScoredEvidence] = []
    flags: list[tuple[Decimal, Decimal, bool]] = []
    transients: list[ScoredEvidence] = []
    previous_rms = 0.0
    for offset in range(0, len(samples), window_samples):
        chunk = samples[offset : offset + window_samples].astype(np.float64)
        if chunk.size == 0:
            continue
        start = proxy_offset + Decimal(offset) / Decimal(sample_rate)
        sample_end = proxy_offset + Decimal(
            min(offset + window_samples, len(samples))
        ) / Decimal(sample_rate)
        if offset + window_samples >= len(samples):
            end = min(sample_end + sample_tolerance, mapping.proxy_end)
        else:
            end = sample_end
        rms = _clamp(float(np.sqrt(np.mean(np.square(chunk)))) / 32768)
        energy.append(_scored("audio-energy", start, end, rms, mapping))
        flags.append((start, end, rms <= settings.silence_rms))
        delta = rms - previous_rms
        if delta >= settings.transient_delta:
            transients.append(_scored("audio-transient", start, end, delta, mapping))
        previous_rms = rms
    silence = _merge_boolean_ranges(flags, "silence", mapping)
    speech = _merge_boolean_ranges(
        ((start, end, not silent) for start, end, silent in flags),
        "speech-presence",
        mapping,
    )
    return tuple(energy), silence, speech, tuple(transients)


def _boundary_suitability(
    scenes: tuple[IntervalEvidence, ...],
    motion: tuple[ScoredEvidence, ...],
    mapping: ProxyMapping,
) -> tuple[BoundarySuitabilityEvidence, ...]:
    """Score scene entry and exit boundaries independently of candidate selection."""
    records: list[BoundarySuitabilityEvidence] = []
    for scene in scenes:
        scene_motion = [
            item
            for item in motion
            if scene.proxy_range.start <= item.proxy_range.start < scene.proxy_range.end
        ]
        entry_motion = scene_motion[0].score if scene_motion else 0.0
        exit_motion = scene_motion[-1].score if scene_motion else 0.0
        entry_score = _clamp(1 - entry_motion)
        exit_score = _clamp(1 - exit_motion)
        records.append(
            BoundarySuitabilityEvidence(
                evidence_id=_evidence_id(
                    "boundary-suitability",
                    scene.proxy_range.start,
                    scene.proxy_range.end,
                    (entry_score + exit_score) / 2,
                ),
                proxy_range=scene.proxy_range,
                source_range=scene.source_range,
                entry_score=entry_score,
                exit_score=exit_score,
            )
        )
    return tuple(records)


def _candidate_windows(
    scenes: tuple[IntervalEvidence, ...],
    motion: tuple[ScoredEvidence, ...],
    transients: tuple[ScoredEvidence, ...],
    mapping: ProxyMapping,
    settings: SegmentationSettings,
) -> tuple[IntervalEvidence, ...]:
    peaks = sorted(
        [item.proxy_range.start for item in motion if item.score >= 0.15]
        + [item.proxy_range.start for item in transients]
    )
    windows: list[IntervalEvidence] = []
    for scene in scenes:
        scene_peaks = [
            peak
            for peak in peaks
            if scene.proxy_range.start <= peak < scene.proxy_range.end
        ]
        if not scene_peaks:
            continue
        groups: list[list[Decimal]] = []
        for peak in scene_peaks:
            if not groups or peak - groups[-1][-1] > settings.peak_merge_seconds:
                groups.append([peak])
            else:
                groups[-1].append(peak)
        for group in groups:
            start = max(scene.proxy_range.start, group[0] - settings.lead_seconds)
            end = min(scene.proxy_range.end, group[-1] + settings.resolution_seconds)
            if end > start:
                windows.append(_interval("candidate", start, end, mapping))
    return tuple(windows)


def _frame_rate(proxy: Path) -> Decimal:
    capture = cv2.VideoCapture(str(proxy))
    try:
        fps = capture.get(cv2.CAP_PROP_FPS) if capture.isOpened() else 0.0
    finally:
        capture.release()
    if not np.isfinite(fps) or fps <= 0:
        raise ValueError("proxy has invalid frame timing")
    return Decimal(str(fps))


def segment_media(
    proxy: Path,
    audio: Path | None,
    mapping: ProxyMapping,
    settings: SegmentationSettings | None = None,
) -> LocalSegmentation:
    """Extract deterministic evidence from proxy video and optional mono WAV."""
    active = settings or SegmentationSettings()
    samples, duration = _read_video(proxy, active)
    mapped_duration = mapping.proxy_end - mapping.proxy_start
    # Encoders may emit up to two trailing frames past the mapped end; samples at or
    # after proxy_end are dropped below, so evidence stays mapping-anchored.
    overshoot_limit = max(Decimal("0.05"), 2 / _frame_rate(proxy))
    difference = duration - mapped_duration
    if difference < Decimal("-0.05") or difference > overshoot_limit:
        raise ValueError("decoded proxy duration does not match mapping")
    samples = [
        (timestamp + mapping.proxy_start, frame)
        for timestamp, frame in samples
        if timestamp + mapping.proxy_start < mapping.proxy_end
    ]
    if not samples:
        raise ValueError("proxy contains no samples within mapped interval")
    scenes = _scene_ranges(samples, mapping.proxy_end, mapping, active)
    video = _frame_evidence(samples, mapping.proxy_end, mapping, active)
    energy, silence, speech, transients = _audio_evidence(
        audio, mapping, active, mapping.proxy_start
    )
    motion = video["motion"]
    return LocalSegmentation(
        schema_version=1,
        implementation_version=_IMPLEMENTATION_VERSION,
        settings_hash=_settings_hash(active),
        source_id=mapping.source_id,
        source_identity=mapping.source_identity,
        proxy_settings_hash=mapping.settings_hash,
        proxy_tool_version=mapping.tool_version,
        scenes=scenes,
        silence_ranges=silence,
        speech_presence_ranges=speech,
        audio_energy=energy,
        audio_transients=transients,
        motion=motion,
        motion_continuity=video["motion-continuity"],
        blur=video["blur"],
        shake=video["shake"],
        exposure=video["exposure"],
        obstruction=video["obstruction"],
        boundary_suitability=_boundary_suitability(scenes, motion, mapping),
        candidate_windows=_candidate_windows(
            scenes, motion, transients, mapping, active
        ),
    )


def canonical_segmentation_json(segmentation: LocalSegmentation) -> bytes:
    """Serialize local evidence as sorted compact UTF-8 JSON with final newline."""
    payload = json.dumps(
        segmentation.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"{payload}\n".encode()
