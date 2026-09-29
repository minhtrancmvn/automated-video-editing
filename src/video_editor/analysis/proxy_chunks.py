"""Registered cloud proxy chunk creation and upload provenance validation."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from video_editor.analysis.models import (
    AnalysisBoundaryKind,
    AnalysisChunkData,
    ProxyManifestData,
)
from video_editor.config import PathSettings
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.discovery import IDENTITY_VERSION, bounded_fingerprint
from video_editor.media.probe import MediaProbe, probe_media
from video_editor.media.proxies import ProxyMapping
from video_editor.persistence.database import JobStore

_IMPLEMENTATION_VERSION = "cloud-proxy-v1"
_MAPPING_VERSION = "cloud-proxy-mapping-v1"
_DURATION_TOLERANCE = Decimal("0.2")


@dataclass(frozen=True)
class CloudProxySettings:
    """Deterministic cloud analysis proxy settings."""

    max_width: int = 640
    fps: int = 15
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    audio_bitrate: str = "64k"
    target_seconds: Decimal = Decimal(720)
    minimum_seconds: Decimal = Decimal(600)
    maximum_seconds: Decimal = Decimal(900)

    def __post_init__(self) -> None:
        """Reject invalid or unsupported cloud proxy settings."""
        durations = (
            self.minimum_seconds,
            self.target_seconds,
            self.maximum_seconds,
        )
        if (
            self.max_width <= 0
            or self.fps <= 0
            or any(not value.is_finite() or value <= 0 for value in durations)
            or not self.minimum_seconds <= self.target_seconds <= self.maximum_seconds
        ):
            raise ValueError("cloud proxy settings must be positive and ordered")
        if self.video_codec != "libx264":
            raise ValueError("cloud proxy video codec must be libx264")
        if self.audio_codec != "aac" or self.audio_bitrate != "64k":
            raise ValueError("cloud proxy audio must be mono AAC at 64k")


@dataclass(frozen=True)
class ProxyManifest:
    """One generated cloud proxy and its typed persistence records."""

    manifest_id: str
    chunk_id: str
    job_id: str
    source_id: str
    path: Path
    digest: str
    data: ProxyManifestData
    chunk_data: AnalysisChunkData


def plan_chunk_ranges(
    source_duration: Decimal,
    safe_boundaries: list[Decimal],
    settings: CloudProxySettings | None = None,
) -> list[tuple[Decimal, Decimal]]:
    """Cover one source exactly using scene-safe 10–15 minute chunk targets."""
    active = settings or CloudProxySettings()
    if not source_duration.is_finite() or source_duration <= 0:
        raise ValueError("source duration must be finite and positive")
    previous = Decimal(0)
    for boundary in safe_boundaries:
        if not boundary.is_finite():
            raise ValueError("safe boundaries must be finite")
        if boundary <= previous:
            raise ValueError("safe boundaries must be strictly monotonic")
        if boundary >= source_duration:
            raise ValueError("safe boundaries must be inside source duration")
        previous = boundary

    def can_partition(duration: Decimal) -> bool:
        if duration == 0:
            return True
        minimum_chunks = int(
            (duration / active.maximum_seconds).to_integral_value(
                rounding="ROUND_CEILING"
            )
        )
        maximum_chunks = int(duration // active.minimum_seconds)
        return minimum_chunks <= maximum_chunks

    def fixed_end(start: Decimal) -> Decimal:
        remaining = source_duration - start
        target_duration = active.target_seconds
        feasible_chunks = [
            count
            for count in range(2, int(remaining // active.minimum_seconds) + 1)
            if remaining <= count * active.maximum_seconds
        ]
        if not feasible_chunks:
            return start + target_duration
        count = min(
            feasible_chunks,
            key=lambda value: (abs(remaining / value - target_duration), value),
        )
        lower = max(
            active.minimum_seconds,
            remaining - (count - 1) * active.maximum_seconds,
        )
        upper = min(
            active.maximum_seconds,
            remaining - (count - 1) * active.minimum_seconds,
        )
        return start + min(max(target_duration, lower), upper)

    ranges: list[tuple[Decimal, Decimal]] = []
    start = Decimal(0)
    while source_duration - start > active.maximum_seconds:
        minimum = start + active.minimum_seconds
        maximum = start + active.maximum_seconds
        target = start + active.target_seconds
        boundaries = [
            boundary for boundary in safe_boundaries if minimum <= boundary <= maximum
        ]
        partitioned = [
            boundary
            for boundary in boundaries
            if can_partition(source_duration - boundary)
        ]
        eligible = partitioned or boundaries
        end = (
            min(eligible, key=lambda value: (abs(value - target), value))
            if eligible
            else fixed_end(start)
        )
        if end <= start or end >= source_duration:
            raise ValueError("chunk planner produced invalid coverage")
        ranges.append((start, end))
        start = end
    ranges.append((start, source_duration))
    if ranges[0][0] != 0 or ranges[-1][1] != source_duration:
        raise ValueError("chunk ranges do not cover source duration")
    if any(left[1] != right[0] for left, right in pairwise(ranges)):
        raise ValueError("chunk ranges contain a gap or overlap")
    return ranges


def _audio_bitrate_bps(value: str) -> int:
    normalized = value.lower()
    try:
        if normalized.endswith("k"):
            return int(normalized[:-1]) * 1000
        return int(normalized)
    except ValueError as exc:
        raise ValueError(
            "audio bitrate must be an integer or k-suffixed integer"
        ) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise VideoEditorError(
            ErrorCategory.STORAGE, f"cannot digest generated proxy: {exc}"
        ) from exc
    return digest.hexdigest()


def _bounded_fingerprint_fd(fd: int, chunk_bytes: int = 1_048_576) -> str:
    """Hash stable source facts and bounded content from one open descriptor."""
    source_stat = os.fstat(fd)
    size = source_stat.st_size
    first = os.pread(fd, chunk_bytes, 0)
    last = (
        first if size <= chunk_bytes else os.pread(fd, chunk_bytes, size - chunk_bytes)
    )
    digest = hashlib.sha256()
    digest.update(IDENTITY_VERSION.encode("ascii"))
    digest.update(b"\0size\0")
    digest.update(str(size).encode("ascii"))
    digest.update(b"\0first\0")
    digest.update(len(first).to_bytes(8, "big"))
    digest.update(first)
    digest.update(b"\0last\0")
    digest.update(len(last).to_bytes(8, "big"))
    digest.update(last)
    return digest.hexdigest()


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _same_source_facts(left: os.stat_result, right: os.stat_result) -> bool:
    return _same_identity(left, right) and left.st_size == right.st_size


def _cleanup_private_temp(partial: Path, root: Path, root_stat: os.stat_result) -> None:
    """Remove only expected random temporary output from stable private root."""
    if partial.parent != root or not _same_identity(root_stat, root.lstat()):
        raise VideoEditorError(
            ErrorCategory.STORAGE, "refusing cleanup outside stable generated root"
        )
    try:
        partial.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise VideoEditorError(
            ErrorCategory.STORAGE, f"cannot clean generated temporary proxy: {exc}"
        ) from exc


def _run_ffmpeg(args: list[str], *, source_fd: int, output_fd: int) -> None:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            pass_fds=(source_fd, output_fd),
        )
    except OSError as exc:
        raise VideoEditorError(
            ErrorCategory.RENDER, f"cloud proxy generation failed: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "unknown ffmpeg error"
        raise VideoEditorError(
            ErrorCategory.RENDER, f"cloud proxy generation failed: {detail}"
        )


def _expected_video_codec(encoder: str) -> str:
    return {"libx264": "h264"}.get(encoder, encoder)


def _decimal_probe(value: float | None, field: str) -> Decimal:
    if value is None:
        raise VideoEditorError(ErrorCategory.OUTPUT, f"generated proxy missing {field}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise VideoEditorError(
            ErrorCategory.OUTPUT, f"generated proxy has invalid {field}"
        ) from exc
    if not result.is_finite():
        raise VideoEditorError(
            ErrorCategory.OUTPUT, f"generated proxy has invalid {field}"
        )
    return result


def _probe_audio_bitrate(path: Path, ffprobe: str) -> int:
    args = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=bit_rate",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
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
            ErrorCategory.OUTPUT, f"cannot probe generated proxy audio bitrate: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "unknown ffprobe error"
        raise VideoEditorError(
            ErrorCategory.OUTPUT,
            f"cannot probe generated proxy audio bitrate: {detail}",
        )
    try:
        bitrate = int(completed.stdout.strip())
    except ValueError as exc:
        raise VideoEditorError(
            ErrorCategory.OUTPUT, "generated proxy has invalid audio bitrate"
        ) from exc
    if bitrate <= 0:
        raise VideoEditorError(
            ErrorCategory.OUTPUT, "generated proxy has invalid audio bitrate"
        )
    return bitrate


def _validated_probe(
    path: Path,
    expected_duration: Decimal,
    settings: CloudProxySettings,
    *,
    ffprobe: str,
) -> tuple[MediaProbe, int]:
    inspected = probe_media(path, ffprobe=ffprobe)
    video = inspected.video
    audio = inspected.audio
    duration = _decimal_probe(inspected.duration, "duration")
    valid = (
        path.is_file()
        and path.stat().st_size > 0
        and video is not None
        and video.width is not None
        and video.height is not None
        and video.width <= settings.max_width
        and video.codec_name == _expected_video_codec(settings.video_codec)
        and video.avg_frame_rate is not None
        and abs(video.avg_frame_rate - settings.fps) <= 1e-6
        and audio is not None
        and audio.codec_name == settings.audio_codec
        and audio.channels == 1
        and abs(duration - expected_duration) <= _DURATION_TOLERANCE
    )
    if not valid:
        raise VideoEditorError(
            ErrorCategory.OUTPUT, f"invalid generated cloud proxy: {path}"
        )
    return inspected, _probe_audio_bitrate(path, ffprobe)


def _proxy_args(
    source_fd_path: str,
    output_fd_path: str,
    source_start: Decimal,
    duration: Decimal,
    settings: CloudProxySettings,
    ffmpeg: str,
) -> list[str]:
    return [
        ffmpeg,
        "-v",
        "error",
        "-n",
        "-ss",
        format(source_start, "f"),
        "-i",
        source_fd_path,
        "-t",
        format(duration, "f"),
        "-vf",
        f"scale='min({settings.max_width},iw)':-2",
        "-r",
        str(settings.fps),
        "-c:v",
        settings.video_codec,
        "-pix_fmt",
        "yuv420p",
        "-ac",
        "1",
        "-c:a",
        settings.audio_codec,
        "-b:a",
        settings.audio_bitrate,
        "-movflags",
        "frag_keyframe+empty_moov",
        "-f",
        "mp4",
        output_fd_path,
    ]


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _validate_generation_inputs(
    source: Path,
    generated_root: Path,
    paths: PathSettings,
    mapping: ProxyMapping,
    source_start: Decimal,
    source_end: Decimal,
) -> tuple[Path, Path]:
    if not source_start.is_finite() or not source_end.is_finite():
        raise ValueError("chunk range must be finite")
    if source_start < mapping.source_start or source_end > mapping.source_end:
        raise ValueError("chunk range must stay inside source mapping")
    if source_end <= source_start:
        raise ValueError("chunk range must be positive")
    source_resolved = source.resolve(strict=True)
    root_resolved = generated_root.resolve(strict=False)
    allowed_roots = (paths.cache_dir.resolve(), paths.workspace_dir.resolve())
    if any(
        _paths_overlap(left, right)
        for left in allowed_roots
        for right in allowed_roots
        if left != right
    ):
        raise VideoEditorError(
            ErrorCategory.STORAGE, "configured cache and workspace roots overlap"
        )
    matched_roots = [
        allowed for allowed in allowed_roots if root_resolved.parent == allowed
    ]
    if not matched_roots:
        raise VideoEditorError(
            ErrorCategory.STORAGE,
            "generated job root must be a direct child of a configured generated root",
        )
    protected_roots = {paths.input_dir.resolve(), source_resolved.parent}
    if any(
        _paths_overlap(allowed, protected)
        for allowed in allowed_roots
        for protected in protected_roots
    ):
        raise VideoEditorError(
            ErrorCategory.STORAGE,
            "configured generated root overlaps original source tree",
        )
    if any(_paths_overlap(root_resolved, protected) for protected in protected_roots):
        raise VideoEditorError(
            ErrorCategory.STORAGE, "generated root overlaps original source tree"
        )
    generated_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        generated_root.mkdir(mode=0o700)
    except FileExistsError:
        root_stat = generated_root.lstat()
    else:
        root_stat = generated_root.lstat()
        os.chmod(generated_root, 0o700)
        root_stat = generated_root.lstat()
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or stat.S_IMODE(root_stat.st_mode) != 0o700
        or root_stat.st_uid != os.getuid()
        or root_stat.st_nlink < 1
    ):
        raise VideoEditorError(
            ErrorCategory.STORAGE, "generated job root is not private and owned"
        )
    return source_resolved, generated_root.resolve(strict=True)


def create_cloud_proxy_chunk(
    source: Path,
    generated_root: Path,
    mapping: ProxyMapping,
    source_start: Decimal,
    source_end: Decimal,
    *,
    job_id: str,
    source_fingerprint: str,
    paths: PathSettings,
    settings: CloudProxySettings | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> ProxyManifest:
    """Create, inspect, hash, and describe one cloud proxy chunk."""
    active = settings or CloudProxySettings()
    if generated_root.name != job_id:
        raise VideoEditorError(
            ErrorCategory.STORAGE, "generated root must be job-specific"
        )
    source_resolved, root_resolved = _validate_generation_inputs(
        source,
        generated_root,
        paths,
        mapping,
        source_start,
        source_end,
    )
    identity_payload = json.dumps(
        {
            "implementation_version": _IMPLEMENTATION_VERSION,
            "job_id": job_id,
            "mapping_version": _MAPPING_VERSION,
            "source_end": format(source_end, "f"),
            "source_id": mapping.source_id,
            "source_identity": mapping.source_identity,
            "source_start": format(source_start, "f"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    identity = hashlib.sha256(identity_payload).hexdigest()
    manifest_id = f"proxy-manifest-{identity}"
    chunk_id = f"analysis-chunk-{identity}"
    final = root_resolved / f"{identity}.cloud-proxy.mp4"
    partial = root_resolved / f".{secrets.token_hex(24)}.mp4"
    root_stat = root_resolved.lstat()
    partial_owned = False
    duration = source_end - source_start
    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, open_flags)
    except OSError as exc:
        raise VideoEditorError(
            ErrorCategory.INSPECTION, f"cannot open source securely: {exc}"
        ) from exc
    try:
        source_stat = os.fstat(source_fd)
        path_stat = source.lstat()
        if not _same_identity(source_stat, path_stat):
            raise VideoEditorError(
                ErrorCategory.INSPECTION, "source path identity changed"
            )
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
            raise VideoEditorError(
                ErrorCategory.INSPECTION,
                "source must be one regular file without hard links",
            )
        actual_fingerprint = _bounded_fingerprint_fd(source_fd)
        actual_identity = f"{IDENTITY_VERSION}:{actual_fingerprint}"
        if source_fingerprint != actual_fingerprint:
            raise VideoEditorError(ErrorCategory.STATE, "source fingerprint mismatch")
        if mapping.source_identity != actual_identity:
            raise VideoEditorError(ErrorCategory.STATE, "source identity mismatch")

        root_stat = root_resolved.lstat()
        reserve_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        partial_fd = os.open(partial, reserve_flags, 0o600)
        partial_owned = True
        try:
            reserved_stat = os.fstat(partial_fd)
            if (
                not stat.S_ISREG(reserved_stat.st_mode)
                or reserved_stat.st_nlink != 1
                or stat.S_IMODE(reserved_stat.st_mode) != 0o600
            ):
                raise VideoEditorError(
                    ErrorCategory.STORAGE, "temporary proxy reservation is unsafe"
                )
            if not _same_identity(root_stat, root_resolved.lstat()):
                raise VideoEditorError(
                    ErrorCategory.STORAGE, "generated root identity changed"
                )

            _run_ffmpeg(
                _proxy_args(
                    f"/dev/fd/{source_fd}",
                    f"pipe:{partial_fd}",
                    source_start,
                    duration,
                    active,
                    ffmpeg,
                ),
                source_fd=source_fd,
                output_fd=partial_fd,
            )
            os.fsync(partial_fd)
        finally:
            os.close(partial_fd)
        current_source_stat = os.fstat(source_fd)
        if (
            not _same_source_facts(source_stat, current_source_stat)
            or _bounded_fingerprint_fd(source_fd) != actual_fingerprint
        ):
            raise VideoEditorError(ErrorCategory.STATE, "source identity changed")
        if not _same_identity(root_stat, root_resolved.lstat()):
            raise VideoEditorError(
                ErrorCategory.STORAGE, "generated root identity changed"
            )
        partial_stat = partial.lstat()
        partial_read_fd = os.open(partial, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened_partial_stat = os.fstat(partial_read_fd)
            if (
                not stat.S_ISREG(partial_stat.st_mode)
                or partial_stat.st_nlink != 1
                or not _same_identity(partial_stat, opened_partial_stat)
            ):
                raise VideoEditorError(
                    ErrorCategory.STORAGE, "generated temporary proxy is unsafe"
                )
        finally:
            os.close(partial_read_fd)
        inspected, probed_audio_bitrate = _validated_probe(
            partial,
            duration,
            active,
            ffprobe=ffprobe,
        )
        current_partial_stat = partial.lstat()
        if (
            not _same_identity(partial_stat, current_partial_stat)
            or current_partial_stat.st_nlink != 1
        ):
            raise VideoEditorError(
                ErrorCategory.STORAGE, "generated temporary proxy identity changed"
            )
        if not _same_identity(root_stat, root_resolved.lstat()):
            raise VideoEditorError(
                ErrorCategory.STORAGE, "generated root identity changed"
            )
        try:
            os.link(partial, final, follow_symlinks=False)
        except FileExistsError as exc:
            raise VideoEditorError(
                ErrorCategory.STORAGE, "final proxy path already exists"
            ) from exc
        final_stat = final.lstat()
        if not _same_identity(partial_stat, final_stat):
            raise VideoEditorError(
                ErrorCategory.STORAGE, "final proxy identity changed during publication"
            )
        partial.unlink()
        partial_owned = False
    except BaseException:
        if partial_owned:
            _cleanup_private_temp(partial, root_resolved, root_stat)
        raise
    finally:
        os.close(source_fd)

    file_stat = final.stat(follow_symlinks=False)
    root_stat = root_resolved.stat(follow_symlinks=False)
    video = inspected.video
    audio = inspected.audio
    assert video is not None and video.width is not None and video.height is not None
    assert video.avg_frame_rate is not None and audio is not None
    manifest_data = ProxyManifestData(
        schema_version=1,
        job_id=job_id,
        source_id=mapping.source_id,
        chunk_id=chunk_id,
        source_fingerprint=source_fingerprint,
        source_identity=mapping.source_identity,
        source_path=str(source_resolved),
        source_device=source_stat.st_dev,
        source_inode=source_stat.st_ino,
        source_size_bytes=source_stat.st_size,
        artifact_path=str(final),
        generated_root=str(root_resolved),
        generated_root_device=root_stat.st_dev,
        generated_root_inode=root_stat.st_ino,
        mapping_version=_MAPPING_VERSION,
        upstream_settings_hash=mapping.settings_hash,
        upstream_tool_version=mapping.tool_version,
        source_start=source_start,
        source_end=source_end,
        proxy_start=Decimal(0),
        proxy_end=duration,
        media_duration=_decimal_probe(inspected.duration, "duration"),
        file_size_bytes=file_stat.st_size,
        file_digest_sha256=_sha256(final),
        file_device=file_stat.st_dev,
        file_inode=file_stat.st_ino,
        video_codec=_expected_video_codec(active.video_codec),
        video_width=video.width,
        video_height=video.height,
        video_fps=Decimal(str(video.avg_frame_rate)),
        audio_codec=active.audio_codec,
        audio_channels=audio.channels or 0,
        audio_bitrate_bps=_audio_bitrate_bps(active.audio_bitrate),
        audio_probe_bitrate_bps=probed_audio_bitrate,
        implementation_version=_IMPLEMENTATION_VERSION,
    )
    chunk_data = AnalysisChunkData(
        schema_version=1,
        job_id=job_id,
        source_id=mapping.source_id,
        source_identity=mapping.source_identity,
        mapping_version=_MAPPING_VERSION,
        source_start=source_start,
        source_end=source_end,
        proxy_start=Decimal(0),
        proxy_end=duration,
        boundary_kind=AnalysisBoundaryKind.SCENE,
        implementation_version=_IMPLEMENTATION_VERSION,
    )
    return ProxyManifest(
        manifest_id=manifest_id,
        chunk_id=chunk_id,
        job_id=job_id,
        source_id=mapping.source_id,
        path=final,
        digest=manifest_data.file_digest_sha256,
        data=manifest_data,
        chunk_data=chunk_data,
    )


def register_proxy_manifest(manifest: ProxyManifest, store: JobStore) -> None:
    """Persist manifest and mapped chunk as one registered upload identity."""
    if manifest.data.job_id != manifest.job_id:
        raise ValueError("manifest job identity mismatch")
    if manifest.data.source_id != manifest.source_id:
        raise ValueError("manifest source identity mismatch")
    if manifest.data.chunk_id != manifest.chunk_id:
        raise ValueError("manifest chunk identity mismatch")
    if manifest.data.file_digest_sha256 != manifest.digest:
        raise ValueError("manifest digest mismatch")
    connection = store.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        store.save_proxy_manifest(
            manifest.job_id,
            manifest.manifest_id,
            manifest.digest,
            manifest.data,
            connection=connection,
        )
        store.save_analysis_chunk(
            manifest.job_id,
            manifest.chunk_id,
            manifest.manifest_id,
            source_id=manifest.source_id,
            source_start=manifest.data.source_start,
            source_end=manifest.data.source_end,
            data=manifest.chunk_data,
            connection=connection,
        )
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _upload_error(message: str) -> VideoEditorError:
    return VideoEditorError(
        ErrorCategory.ANALYSIS, message, code="unsafe_upload_candidate"
    )


def _reject_symlinks(path: Path) -> None:
    current = path
    while True:
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise _upload_error(f"cannot inspect upload candidate path: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise _upload_error("upload candidate links to original or untrusted media")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _stored_source(job: dict[str, Any], source_id: str) -> dict[str, Any]:
    sources = [
        source
        for source in job.get("sources", [])
        if source.get("source_id") == source_id
    ]
    if len(sources) != 1:
        raise _upload_error("manifest source identity is not registered for job")
    source = sources[0]
    if not isinstance(source, dict):
        raise _upload_error("persisted source record is invalid")
    return source


def _validate_persisted_identity(
    record: dict[str, Any],
    manifest: ProxyManifestData,
    chunk: dict[str, Any],
    chunk_data: AnalysisChunkData,
) -> None:
    if record["job_id"] != manifest.job_id or chunk["job_id"] != manifest.job_id:
        raise _upload_error("manifest job identity mismatch")
    if chunk["manifest_id"] != record["manifest_id"]:
        raise _upload_error("manifest and chunk identity mismatch")
    if chunk["source_id"] != manifest.source_id:
        raise _upload_error("manifest source identity mismatch")
    if manifest.chunk_id != chunk["chunk_id"]:
        raise _upload_error("manifest chunk identity mismatch")
    if (
        chunk_data.job_id != manifest.job_id
        or chunk_data.source_id != manifest.source_id
        or chunk_data.source_identity != manifest.source_identity
        or chunk_data.mapping_version != manifest.mapping_version
        or chunk_data.source_start != manifest.source_start
        or chunk_data.source_end != manifest.source_end
        or chunk_data.proxy_start != manifest.proxy_start
        or chunk_data.proxy_end != manifest.proxy_end
        or chunk["source_start"] != manifest.source_start
        or chunk["source_end"] != manifest.source_end
    ):
        raise _upload_error("manifest mapping does not match registered chunk")
    if (
        manifest.source_end <= manifest.source_start
        or manifest.proxy_end <= manifest.proxy_start
        or manifest.source_end - manifest.source_start
        != manifest.proxy_end - manifest.proxy_start
    ):
        raise _upload_error("manifest mapping is not monotonic")


def _validate_candidate_path(
    path: Path,
    manifest: ProxyManifestData,
    paths: PathSettings,
    source_paths: list[Path],
) -> tuple[Path, os.stat_result]:
    _reject_symlinks(path)
    try:
        resolved = path.resolve(strict=True)
        candidate_stat = path.stat(follow_symlinks=False)
        root = Path(manifest.generated_root)
        _reject_symlinks(root)
        resolved_root = root.resolve(strict=True)
        root_stat = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise _upload_error(f"cannot inspect upload candidate: {exc}") from exc
    allowed_roots = (paths.cache_dir.resolve(), paths.workspace_dir.resolve())
    if not any(
        resolved_root == allowed or resolved_root.is_relative_to(allowed)
        for allowed in allowed_roots
    ):
        raise _upload_error("manifest generated root is not configured")
    input_root = paths.input_dir.resolve()
    if resolved == input_root or resolved.is_relative_to(input_root):
        raise _upload_error("upload candidate is inside input root")
    if resolved != resolved_root and not resolved.is_relative_to(resolved_root):
        raise _upload_error("upload candidate escapes generated root")
    if (
        root_stat.st_dev != manifest.generated_root_device
        or root_stat.st_ino != manifest.generated_root_inode
    ):
        raise _upload_error("generated root identity changed")
    for source in source_paths:
        source_resolved = source.resolve(strict=True)
        source_stat = source.stat()
        if (
            resolved == source_resolved
            or resolved.is_relative_to(source_resolved)
            or (candidate_stat.st_dev, candidate_stat.st_ino)
            == (source_stat.st_dev, source_stat.st_ino)
        ):
            raise _upload_error("upload candidate is original source media")
    return resolved, candidate_stat


def _validate_file_facts(
    path: Path,
    file_stat: os.stat_result,
    record: dict[str, Any],
    manifest: ProxyManifestData,
) -> None:
    digest = _sha256(path)
    if record["digest"] != manifest.file_digest_sha256:
        raise _upload_error("registered manifest digest mismatch")
    if digest != manifest.file_digest_sha256:
        raise _upload_error("upload candidate digest mismatch")
    if file_stat.st_size != manifest.file_size_bytes:
        raise _upload_error("upload candidate size mismatch")
    if (
        file_stat.st_dev != manifest.file_device
        or file_stat.st_ino != manifest.file_inode
    ):
        raise _upload_error("upload candidate file identity changed")
    if file_stat.st_nlink != 1:
        raise _upload_error(
            "upload candidate is a hard link to original or untrusted media"
        )


def _validate_source_identity(
    source: dict[str, Any], manifest: ProxyManifestData
) -> Path:
    source_path_value = source.get("path")
    fingerprint = source.get("fingerprint")
    identity_version = source.get("identity_version")
    if not all(
        isinstance(value, str) and value
        for value in (source_path_value, fingerprint, identity_version)
    ):
        raise _upload_error("persisted source identity is invalid")
    assert isinstance(source_path_value, str)
    source_path = Path(source_path_value)
    try:
        source_resolved = source_path.resolve(strict=True)
        manifest_source_resolved = Path(manifest.source_path).resolve(strict=True)
    except OSError as exc:
        raise _upload_error(f"cannot inspect registered source: {exc}") from exc
    if source_resolved != manifest_source_resolved:
        raise _upload_error("manifest source path mismatch")
    if fingerprint != manifest.source_fingerprint:
        raise _upload_error("manifest source fingerprint mismatch")
    try:
        current_fingerprint = bounded_fingerprint(source_path)
    except VideoEditorError as exc:
        raise _upload_error(f"cannot verify registered source: {exc}") from exc
    current_identity = f"{identity_version}:{current_fingerprint}"
    if current_identity != manifest.source_identity:
        raise _upload_error("source identity is stale")
    return source_path


def _validate_probe(path: Path, manifest: ProxyManifestData, *, ffprobe: str) -> None:
    inspected = probe_media(path, ffprobe=ffprobe)
    video = inspected.video
    audio = inspected.audio
    if video is None or video.width != manifest.video_width:
        raise _upload_error("upload candidate width mismatch")
    if video.height != manifest.video_height:
        raise _upload_error("upload candidate height mismatch")
    if video.codec_name != manifest.video_codec:
        raise _upload_error("upload candidate video codec mismatch")
    if video.avg_frame_rate is None or abs(
        Decimal(str(video.avg_frame_rate)) - manifest.video_fps
    ) > Decimal("0.000001"):
        raise _upload_error("upload candidate FPS mismatch")
    if audio is None or audio.codec_name != manifest.audio_codec:
        raise _upload_error("upload candidate audio codec mismatch")
    if audio.channels != manifest.audio_channels:
        raise _upload_error("upload candidate audio channels mismatch")
    if manifest.audio_bitrate_bps != 64_000:
        raise _upload_error("upload candidate audio bitrate mismatch")
    try:
        probed_audio_bitrate = _probe_audio_bitrate(path, ffprobe)
    except VideoEditorError as exc:
        raise _upload_error(
            f"cannot verify upload candidate audio bitrate: {exc}"
        ) from exc
    if probed_audio_bitrate != manifest.audio_probe_bitrate_bps:
        raise _upload_error("upload candidate audio probe bitrate mismatch")
    duration = _decimal_probe(inspected.duration, "duration")
    if abs(duration - manifest.media_duration) > _DURATION_TOLERANCE:
        raise _upload_error("upload candidate duration mismatch")
    if manifest.video_width > 640 or abs(manifest.video_fps - 15) > Decimal("0.000001"):
        raise _upload_error("upload candidate settings exceed cloud proxy limits")


def validate_upload_candidate(
    manifest_id: str,
    store: JobStore,
    paths: PathSettings,
    *,
    ffprobe: str = "ffprobe",
) -> Path:
    """Revalidate registered bytes and provenance immediately before upload."""
    record = store.get_proxy_manifest(manifest_id)
    if record is None:
        raise _upload_error("unregistered upload candidate")
    try:
        manifest = ProxyManifestData.model_validate(record["data"])
    except ValidationError as exc:
        raise _upload_error("registered manifest mapping is invalid") from exc
    chunk = store.get_analysis_chunk(manifest.chunk_id)
    if chunk is None:
        raise _upload_error("registered manifest has no analysis chunk")
    try:
        chunk_data = AnalysisChunkData.model_validate(chunk["data"])
    except ValidationError as exc:
        raise _upload_error("registered chunk mapping is invalid") from exc
    _validate_persisted_identity(record, manifest, chunk, chunk_data)

    job = store.get_job(manifest.job_id)
    source = _stored_source(job, manifest.source_id)
    source_path = _validate_source_identity(source, manifest)
    source_paths = [
        Path(value["path"])
        for value in job.get("sources", [])
        if isinstance(value, dict) and isinstance(value.get("path"), str)
    ]
    path, file_stat = _validate_candidate_path(
        Path(manifest.artifact_path), manifest, paths, [source_path, *source_paths]
    )
    _validate_file_facts(path, file_stat, record, manifest)
    _validate_probe(path, manifest, ffprobe=ffprobe)
    return path
