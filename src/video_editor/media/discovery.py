"""Recursive media discovery and stable bounded source identities."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from video_editor.errors import ErrorCategory, VideoEditorError

IDENTITY_VERSION = "bounded-v1"
VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mts",
        ".webm",
    }
)


@dataclass(frozen=True)
class SourceCandidate:
    """A discovered video file and content-derived identity evidence."""

    path: Path
    size_bytes: int
    discovery_index: int
    fingerprint: str
    identity_version: str


def _inspection_error(message: str, cause: OSError | None = None) -> VideoEditorError:
    error = VideoEditorError(ErrorCategory.INSPECTION, message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _normalized_extensions(extensions: frozenset[str]) -> frozenset[str]:
    return frozenset(
        extension.lower() if extension.startswith(".") else f".{extension.lower()}"
        for extension in extensions
    )


def bounded_fingerprint(path: Path, chunk_bytes: int = 1_048_576) -> str:
    """Hash file facts and bounded content without including its location."""

    if chunk_bytes <= 0:
        raise VideoEditorError(
            ErrorCategory.INSPECTION, "chunk_bytes must be greater than zero"
        )
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            first = handle.read(chunk_bytes)
            if size > chunk_bytes:
                handle.seek(max(0, size - chunk_bytes))
                last = handle.read(chunk_bytes)
            else:
                last = first
    except OSError as exc:
        raise _inspection_error(f"cannot read source {path}: {exc}", exc) from exc

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


def discover_sources(
    root: Path, extensions: frozenset[str] = VIDEO_EXTENSIONS
) -> list[SourceCandidate]:
    """Recursively discover supported video files in deterministic path order."""

    try:
        root_stat = root.stat()
    except OSError as exc:
        raise _inspection_error(
            f"cannot inspect discovery root {root}: {exc}", exc
        ) from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise VideoEditorError(
            ErrorCategory.INSPECTION, f"discovery root {root} is not a directory"
        )

    normalized = _normalized_extensions(extensions)
    candidates: list[Path] = []
    pending = [root]
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as handle:
                entries = sorted(handle, key=lambda entry: entry.name)
                for entry in entries:
                    entry_stat = entry.stat(follow_symlinks=False)
                    entry_path = Path(entry.path)
                    if stat.S_ISDIR(entry_stat.st_mode):
                        pending.append(entry_path)
                        continue
                    followed_stat = entry.stat(follow_symlinks=True)
                    if (
                        stat.S_ISREG(followed_stat.st_mode)
                        and entry_path.suffix.lower() in normalized
                    ):
                        candidates.append(entry_path)
        candidates.sort(key=lambda path: path.relative_to(root).as_posix())
    except OSError as exc:
        raise _inspection_error(
            f"cannot inspect discovery root {root}: {exc}", exc
        ) from exc

    sources: list[SourceCandidate] = []
    for discovery_index, path in enumerate(candidates):
        try:
            size_bytes = path.stat().st_size
        except OSError as exc:
            raise _inspection_error(
                f"cannot inspect source {path}: {exc}", exc
            ) from exc
        sources.append(
            SourceCandidate(
                path=path,
                size_bytes=size_bytes,
                discovery_index=discovery_index,
                fingerprint=bounded_fingerprint(path),
                identity_version=IDENTITY_VERSION,
            )
        )
    return sources
