"""ffprobe-backed media inspection and typed metadata models."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from video_editor.errors import ErrorCategory, VideoEditorError


class InspectionWarning(BaseModel):
    """One stable, actionable warning produced during source inspection."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class VideoStream(BaseModel):
    """Relevant video stream metadata returned by ffprobe."""

    model_config = ConfigDict(extra="forbid")

    codec_name: str | None = None
    profile: str | None = None
    pix_fmt: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    time_base: str | None = None
    avg_frame_rate: float | None = None
    r_frame_rate: float | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_space: str | None = None
    color_range: str | None = None
    rotation: int | None = None


class AudioStream(BaseModel):
    """Relevant audio stream metadata returned by ffprobe."""

    model_config = ConfigDict(extra="forbid")

    codec_name: str | None = None
    profile: str | None = None
    channels: int | None = None
    channel_layout: str | None = None
    sample_rate: int | None = None
    duration: float | None = None
    time_base: str | None = None


class MediaProbe(BaseModel):
    """Typed container and warnings for one media source."""

    model_config = ConfigDict(extra="forbid")

    path: Path
    format_name: str | None = None
    duration: float | None = None
    creation_time: datetime | None = None
    video: VideoStream | None = None
    audio: AudioStream | None = None
    warnings: list[InspectionWarning] = Field(default_factory=list)


_KNOWN_COLOR_VALUES = {
    "bt709", "bt2020", "bt2020nc", "bt2020ncl", "bt2020cl", "smpte170m",
    "smpte240m", "smpte2084", "arib-std-b67", "iec61966-2-1", "rgb",
    "unknown", "unspecified", "reserved", "gbr", "smpte428", "log", "tv", "full",
}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: Any) -> float | None:
    if value is None or value == "" or value == "N/A":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rational(value: Any) -> float | None:
    if value is None or value in {"", "N/A", "0/0"}:
        return None
    try:
        if isinstance(value, str) and "/" in value:
            numerator, denominator = value.split("/", 1)
            return float(Fraction(int(numerator), int(denominator)))
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _creation_time(value: Any) -> datetime | None:
    raw = _text(value)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _rotation(stream: dict[str, Any]) -> int | None:
    tags = stream.get("tags")
    if isinstance(tags, dict) and tags.get("rotate") is not None:
        rotation = _integer(tags.get("rotate"))
    else:
        rotation = None
    for side_data in stream.get("side_data_list", []):
        if not isinstance(side_data, dict):
            continue
        if "rotation" in side_data:
            side_rotation = _integer(side_data["rotation"])
            if side_rotation is not None:
                rotation = side_rotation
    return rotation


def _warning(code: str, message: str) -> InspectionWarning:
    return InspectionWarning(code=code, message=message)


def _inspect_warnings(video: VideoStream | None, audio: AudioStream | None) -> list[InspectionWarning]:
    warnings: list[InspectionWarning] = []
    if video is not None:
        if video.codec_name == "hevc":
            warnings.append(_warning("hevc", "source uses HEVC video"))
        if video.pix_fmt is not None and re.search(r"(?:10|12|14|16)le?$", video.pix_fmt):
            warnings.append(_warning("ten_bit", "source uses high bit-depth video"))
        if video.rotation not in (None, 0):
            warnings.append(_warning("rotation", "source contains rotation metadata"))
        if video.color_transfer in {"smpte2084", "arib-std-b67"}:
            warnings.append(_warning("hdr", "source contains HDR transfer metadata"))
        if (
            video.avg_frame_rate is not None
            and video.r_frame_rate is not None
            and abs(video.avg_frame_rate - video.r_frame_rate) > 1e-6
        ):
            warnings.append(_warning("possible_vfr", "average and nominal frame rates differ"))
        for value in (video.color_primaries, video.color_transfer, video.color_space, video.color_range):
            if value is not None and value.lower() not in _KNOWN_COLOR_VALUES:
                warnings.append(_warning("unknown_color", f"unknown color metadata: {value}"))
                break
    if audio is None:
        warnings.append(_warning("missing_audio", "source has no audio stream"))
    return warnings


def _stream_metadata(stream: dict[str, Any]) -> VideoStream | AudioStream:
    codec_name = _text(stream.get("codec_name"))
    profile = _text(stream.get("profile"))
    duration = _number(stream.get("duration"))
    time_base = _text(stream.get("time_base"))
    if stream.get("codec_type") == "video":
        return VideoStream(
            codec_name=codec_name,
            profile=profile,
            duration=duration,
            time_base=time_base,
            pix_fmt=_text(stream.get("pix_fmt")),
            width=_integer(stream.get("width")),
            height=_integer(stream.get("height")),
            avg_frame_rate=_rational(stream.get("avg_frame_rate")),
            r_frame_rate=_rational(stream.get("r_frame_rate")),
            color_primaries=_text(stream.get("color_primaries")),
            color_transfer=_text(stream.get("color_transfer")),
            color_space=_text(stream.get("color_space")),
            color_range=_text(stream.get("color_range")),
            rotation=_rotation(stream),
        )
    return AudioStream(
        codec_name=codec_name,
        profile=profile,
        duration=duration,
        time_base=time_base,
        channels=_integer(stream.get("channels")),
        channel_layout=_text(stream.get("channel_layout")),
        sample_rate=_integer(stream.get("sample_rate")),
    )


def probe_media(path: Path, ffprobe: str = "ffprobe") -> MediaProbe:
    """Run ffprobe without a shell and parse its JSON output."""

    args = [ffprobe, "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(path)]
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, shell=False, check=False
        )
    except OSError as exc:
        raise VideoEditorError(ErrorCategory.INSPECTION, f"ffprobe failed for {path}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "unknown ffprobe error"
        raise VideoEditorError(ErrorCategory.INSPECTION, f"ffprobe failed for {path}: {detail}")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VideoEditorError(ErrorCategory.INSPECTION, f"ffprobe returned invalid JSON for {path}") from exc
    if not isinstance(payload, dict):
        raise VideoEditorError(ErrorCategory.INSPECTION, f"ffprobe returned invalid data for {path}")

    streams = payload.get("streams")
    video: VideoStream | None = None
    audio: AudioStream | None = None
    if isinstance(streams, list):
        for raw_stream in streams:
            if not isinstance(raw_stream, dict) or raw_stream.get("codec_type") not in {"video", "audio"}:
                continue
            parsed = _stream_metadata(raw_stream)
            if isinstance(parsed, VideoStream) and video is None:
                video = parsed
            elif isinstance(parsed, AudioStream) and audio is None:
                audio = parsed
    fmt = payload.get("format")
    fmt = fmt if isinstance(fmt, dict) else {}
    tags = fmt.get("tags")
    tags = tags if isinstance(tags, dict) else {}
    return MediaProbe(
        path=path,
        format_name=_text(fmt.get("format_name")),
        duration=_number(fmt.get("duration")) or (video.duration if video else None),
        creation_time=_creation_time(tags.get("creation_time")),
        video=video,
        audio=audio,
        warnings=_inspect_warnings(video, audio),
    )


def summarize_batch_warnings(probes: Sequence[MediaProbe]) -> list[InspectionWarning]:
    """Return first occurrence of each warning code in batch order."""

    result: list[InspectionWarning] = []
    seen: set[str] = set()
    for probe in probes:
        for warning in probe.warnings:
            if warning.code not in seen:
                seen.add(warning.code)
                result.append(warning)
    return result
