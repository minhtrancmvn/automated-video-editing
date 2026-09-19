"""External-volume identity and capacity safety checks."""

from __future__ import annotations

import platform
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from video_editor.errors import ErrorCategory, VideoEditorError

_SUPPORTED_FILESYSTEMS = {"apfs", "exfat"}


@dataclass(frozen=True)
class VolumeIdentity:
    """Stable identifying properties for a mounted filesystem volume."""

    device: int
    mount_point: Path
    filesystem: str | None


def _storage_error(message: str) -> VideoEditorError:
    return VideoEditorError(ErrorCategory.STORAGE, message)


def _mount_point(path: Path, device: int) -> Path:
    current = path
    while current != current.parent:
        try:
            if current.parent.stat().st_dev != device:
                return current
        except OSError as exc:
            raise _storage_error(f"cannot inspect volume for {path}: {exc}") from exc
        current = current.parent
    return current


def _filesystem(mount_point: Path) -> str | None:
    if platform.system() != "Darwin":
        return None
    if shutil.which("diskutil") is None:
        raise _storage_error("cannot inspect filesystem: diskutil is unavailable")
    try:
        result = subprocess.run(
            ["diskutil", "info", "-plist", str(mount_point)],
            check=True,
            capture_output=True,
        )
        info = plistlib.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException) as exc:
        raise _storage_error(
            f"cannot inspect filesystem for {mount_point}: {exc}"
        ) from exc
    value = info.get("FilesystemName")
    if not isinstance(value, str):
        raise _storage_error(
            f"cannot inspect filesystem for {mount_point}: missing FilesystemName"
        )
    return value.lower()


def inspect_volume(path: Path) -> VolumeIdentity:
    """Inspect existing path; never create missing external-volume directories."""

    try:
        stat = path.stat()
    except FileNotFoundError as exc:
        raise _storage_error(f"volume path {path} is not mounted") from exc
    except OSError as exc:
        raise _storage_error(f"cannot inspect volume {path}: {exc}") from exc
    mount_point = _mount_point(path, stat.st_dev)
    filesystem = _filesystem(mount_point)
    if filesystem is not None and filesystem not in _SUPPORTED_FILESYSTEMS:
        raise _storage_error(f"unsupported filesystem {filesystem} on {mount_point}")
    return VolumeIdentity(stat.st_dev, mount_point, filesystem)


def assert_expected_volume(path: Path, expected: VolumeIdentity) -> None:
    """Reject a path whose current volume differs from recorded identity."""

    actual = inspect_volume(path)
    if actual != expected:
        raise _storage_error(
            f"volume changed for {path}: expected {expected.mount_point} ({expected.device}), "
            f"found {actual.mount_point} ({actual.device})"
        )


def assert_free_space(path: Path, required_bytes: int, reserve_bytes: int) -> None:
    """Require required bytes plus reserve to fit on path's volume."""

    if required_bytes < 0 or reserve_bytes < 0:
        raise _storage_error("required_bytes and reserve_bytes must be non-negative")
    try:
        usage = shutil.disk_usage(path)
        free = usage.free if hasattr(usage, "free") else usage[2]
    except (OSError, IndexError, TypeError) as exc:
        raise _storage_error(f"cannot inspect free space for {path}: {exc}") from exc
    if free - reserve_bytes < required_bytes:
        raise _storage_error(
            f"insufficient free space on {path}: {free} free, {reserve_bytes} reserved, "
            f"{required_bytes} required"
        )


def is_within(root: Path, candidate: Path) -> bool:
    """Return whether candidate resolves to root or one of its descendants."""

    return candidate.resolve().is_relative_to(root.resolve())
