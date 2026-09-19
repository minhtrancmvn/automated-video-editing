"""Local analysis proxy and speech-audio generation."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.probe import MediaProbe, probe_media


@dataclass(frozen=True)
class ProxySettings:
    """Deterministic settings for bounded analysis video."""

    max_width: int = 960
    fps: int = 15
    video_codec: str = "libx264"


@dataclass(frozen=True)
class ProxyMapping:
    """Explicit mapping between source and derived-media timestamps."""

    source_id: str
    source_start: Decimal
    source_end: Decimal
    proxy_start: Decimal
    proxy_end: Decimal
    settings_hash: str
    tool_version: str


def _decimal(value: Decimal | float | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(value)


def identity_mapping(
    source_id: str,
    duration: Decimal | float | str,
    settings_hash: str,
    tool_version: str,
) -> ProxyMapping:
    """Create explicit full-duration identity mapping for constant-rate output."""

    end = _decimal(duration)
    if end < 0:
        raise ValueError("duration must be non-negative")
    return ProxyMapping(
        source_id=source_id,
        source_start=Decimal(0),
        source_end=end,
        proxy_start=Decimal(0),
        proxy_end=end,
        settings_hash=settings_hash,
        tool_version=tool_version,
    )


def settings_hash(settings: ProxySettings) -> str:
    """Hash canonical proxy settings for cache invalidation."""

    payload = json.dumps(
        {
            "fps": settings.fps,
            "max_width": settings.max_width,
            "video_codec": settings.video_codec,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


_DEFAULT_SETTINGS = ProxySettings()


def _expected_stream_codec(encoder: str) -> str:
    """Return codec name ffprobe reports for an FFmpeg encoder."""

    return {"libx264": "h264"}.get(encoder, encoder)


def build_proxy_args(
    source: Path,
    output: Path,
    settings: ProxySettings = _DEFAULT_SETTINGS,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Build shell-free FFmpeg arguments for muted, bounded analysis video."""

    if settings.max_width <= 0 or settings.fps <= 0:
        raise ValueError("proxy max_width and fps must be positive")
    return [
        ffmpeg,
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        f"scale='min({settings.max_width},iw)':-2",
        "-r",
        str(settings.fps),
        "-c:v",
        settings.video_codec,
        "-pix_fmt",
        "yuv420p",
        "-an",
        "-f",
        "mp4",
        str(output),
    ]


def build_audio_args(
    source: Path,
    output: Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Build shell-free FFmpeg arguments for mono 16 kHz Whisper WAV."""

    return [
        ffmpeg,
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-ac",
        "1",
        "-f",
        "wav",
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        str(output),
    ]


def _run(args: list[str]) -> None:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
    except OSError as exc:
        raise VideoEditorError(
            ErrorCategory.RENDER, f"media generation failed: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "unknown ffmpeg error"
        raise VideoEditorError(
            ErrorCategory.RENDER, f"media generation failed: {detail}"
        )


def _ensure_cache_output(cache_root: Path, path: Path) -> None:
    root = cache_root.resolve()
    if path.resolve().parent != root:
        raise VideoEditorError(
            ErrorCategory.STORAGE, f"derived output escapes cache root: {path}"
        )


def _validated_rename(
    partial: Path,
    final: Path,
    settings: ProxySettings,
    *,
    ffprobe: str,
) -> Path:
    try:
        inspected = probe_media(partial, ffprobe=ffprobe)
    except VideoEditorError:
        partial.unlink(missing_ok=True)
        raise
    video = inspected.video
    valid_frame_rate = (
        video is not None
        and video.avg_frame_rate is not None
        and abs(video.avg_frame_rate - settings.fps) <= 1e-6
    )
    valid = (
        partial.is_file()
        and partial.stat().st_size > 0
        and video is not None
        and video.width is not None
        and video.width <= settings.max_width
        and video.codec_name == _expected_stream_codec(settings.video_codec)
        and valid_frame_rate
        and inspected.audio is None
    )
    if not valid:
        partial.unlink(missing_ok=True)
        raise VideoEditorError(
            ErrorCategory.OUTPUT, f"invalid generated proxy: {partial}"
        )
    try:
        partial.replace(final)
    except OSError:
        partial.unlink(missing_ok=True)
        raise
    return final


def _validated_audio_rename(partial: Path, final: Path, *, ffprobe: str) -> Path:
    try:
        inspected = probe_media(partial, ffprobe=ffprobe)
    except VideoEditorError:
        partial.unlink(missing_ok=True)
        raise
    audio = inspected.audio
    valid = (
        partial.is_file()
        and partial.stat().st_size > 0
        and inspected.format_name == "wav"
        and audio is not None
        and audio.codec_name == "pcm_s16le"
        and audio.channels == 1
        and audio.sample_rate == 16000
    )
    if not valid:
        partial.unlink(missing_ok=True)
        raise VideoEditorError(
            ErrorCategory.OUTPUT, f"invalid generated audio: {partial}"
        )
    try:
        partial.replace(final)
    except OSError:
        partial.unlink(missing_ok=True)
        raise
    return final


def _run_and_cleanup(args: list[str], partial: Path) -> None:
    try:
        _run(args)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _name(
    source: Path,
    source_id: str,
    suffix: str,
    settings: ProxySettings,
    tool_version: str,
) -> str:
    cache_key = json.dumps(
        {
            "source_id": source_id,
            "source_name": source.name,
            "settings_hash": settings_hash(settings),
            "tool_version": tool_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(cache_key.encode()).hexdigest()[:16]
    return f"{digest}{suffix}"


def valid_cached_media(
    path: Path,
    settings: ProxySettings,
    *,
    kind: str,
    ffprobe: str,
) -> bool:
    """Validate reusable derived media, rejecting missing and truncated files."""
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    try:
        inspected = probe_media(path, ffprobe=ffprobe)
    except VideoEditorError:
        return False
    if kind == "proxy":
        video = inspected.video
        return bool(
            video is not None
            and video.width is not None
            and video.width <= settings.max_width
            and video.codec_name == _expected_stream_codec(settings.video_codec)
            and video.avg_frame_rate is not None
            and abs(video.avg_frame_rate - settings.fps) <= 1e-6
            and inspected.audio is None
        )
    audio = inspected.audio
    return bool(
        inspected.format_name == "wav"
        and audio is not None
        and audio.codec_name == "pcm_s16le"
        and audio.channels == 1
        and audio.sample_rate == 16000
    )


def create_analysis_media(
    source: Path,
    source_id: str,
    cache_root: Path,
    duration: Decimal | float | str | None = None,
    *,
    settings: ProxySettings = _DEFAULT_SETTINGS,
    tool_version: str = "ffmpeg",
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> tuple[Path, Path | None, ProxyMapping]:
    """Generate validated proxy and optional Whisper audio wholly under cache root."""

    if (
        source.resolve() == cache_root.resolve()
        or cache_root.resolve() in source.resolve().parents
    ):
        raise VideoEditorError(
            ErrorCategory.STORAGE, "source path cannot be inside cache root"
        )
    cache_root.mkdir(parents=True, exist_ok=True)
    try:
        source_probe: MediaProbe = probe_media(source, ffprobe=ffprobe)
    except VideoEditorError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise VideoEditorError(
            ErrorCategory.INSPECTION, f"cannot inspect source for analysis media: {exc}"
        ) from exc
    actual_duration = duration if duration is not None else source_probe.duration
    if actual_duration is None:
        raise VideoEditorError(
            ErrorCategory.INSPECTION, f"source duration unavailable: {source}"
        )
    final_proxy = cache_root / _name(
        source, source_id, ".proxy.mp4", settings, tool_version
    )
    partial_proxy = final_proxy.with_name(final_proxy.name + ".partial")
    _ensure_cache_output(cache_root, final_proxy)
    _ensure_cache_output(cache_root, partial_proxy)
    if valid_cached_media(final_proxy, settings, kind="proxy", ffprobe=ffprobe):
        proxy = final_proxy
    else:
        _run_and_cleanup(
            build_proxy_args(source, partial_proxy, settings, ffmpeg=ffmpeg),
            partial_proxy,
        )
        proxy = _validated_rename(partial_proxy, final_proxy, settings, ffprobe=ffprobe)

    audio: Path | None = None
    if source_probe.audio is not None:
        final_audio = cache_root / _name(
            source, source_id, ".audio.wav", settings, tool_version
        )
        partial_audio = final_audio.with_name(final_audio.name + ".partial")
        _ensure_cache_output(cache_root, final_audio)
        _ensure_cache_output(cache_root, partial_audio)
        if valid_cached_media(final_audio, settings, kind="audio", ffprobe=ffprobe):
            audio = final_audio
        else:
            _run_and_cleanup(
                build_audio_args(source, partial_audio, ffmpeg=ffmpeg), partial_audio
            )
            audio = _validated_audio_rename(partial_audio, final_audio, ffprobe=ffprobe)

    mapping = identity_mapping(
        source_id, actual_duration, settings_hash(settings), tool_version
    )
    return proxy, audio, mapping
