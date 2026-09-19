import os
from pathlib import Path

import pytest

from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.discovery import (
    VIDEO_EXTENSIONS,
    bounded_fingerprint,
    discover_sources,
)


def test_discovery_is_recursive_case_insensitive_and_deterministic(
    tmp_path: Path,
) -> None:
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "GH020001.MP4").write_bytes(b"second")
    (tmp_path / "GOPR0001.mp4").write_bytes(b"first")
    (tmp_path / "notes.txt").write_text("ignore")
    sources = discover_sources(tmp_path)
    assert [s.path.name for s in sources] == ["GOPR0001.mp4", "GH020001.MP4"]
    assert [s.discovery_index for s in sources] == [0, 1]
    assert all(s.identity_version == "bounded-v1" for s in sources)


def test_discovery_returns_empty_list_for_empty_readable_folder(tmp_path: Path) -> None:
    assert discover_sources(tmp_path) == []


def test_discovery_includes_symlinked_video_without_recursing_symlink_dirs(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.mp4"
    target.write_bytes(b"video")
    linked_file = tmp_path / "linked.mp4"
    linked_dir = tmp_path / "linked-dir"
    try:
        linked_file.symlink_to(target)
        linked_dir.symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    sources = discover_sources(tmp_path)

    assert [source.path.name for source in sources] == ["linked.mp4", "target.mp4"]


def test_discovery_rejects_nonexistent_root_with_inspection_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(VideoEditorError) as raised:
        discover_sources(tmp_path / "missing")
    assert raised.value.category is ErrorCategory.INSPECTION


def test_discovery_rejects_unreadable_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_scandir = os.scandir

    def failing_scandir(path: str | bytes | os.PathLike[str] | os.PathLike[bytes]):
        if Path(path) == tmp_path:
            raise PermissionError("permission denied")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", failing_scandir)
    with pytest.raises(VideoEditorError) as raised:
        discover_sources(tmp_path)
    assert raised.value.category is ErrorCategory.INSPECTION


def test_discovery_rejects_unreadable_nested_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "clip.mp4").write_bytes(b"video")
    original_scandir = os.scandir

    def failing_scandir(path: str | bytes | os.PathLike[str] | os.PathLike[bytes]):
        if Path(path) == nested:
            raise PermissionError("permission denied")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", failing_scandir)
    with pytest.raises(VideoEditorError) as raised:
        discover_sources(tmp_path)
    assert raised.value.category is ErrorCategory.INSPECTION


def test_fingerprint_changes_when_first_bounded_chunk_changes(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"first" + b"middle" + b"last")
    original = bounded_fingerprint(path, chunk_bytes=5)
    path.write_bytes(b"FIRST" + b"middle" + b"last")
    assert bounded_fingerprint(path, chunk_bytes=5) != original


def test_fingerprint_changes_when_last_bounded_chunk_changes(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"first" + b"middle" + b"last")
    original = bounded_fingerprint(path, chunk_bytes=4)
    path.write_bytes(b"first" + b"middle" + b"LAST")
    assert bounded_fingerprint(path, chunk_bytes=4) != original


def test_fingerprint_does_not_embed_absolute_path(tmp_path: Path) -> None:
    first = tmp_path / "one" / "clip.mp4"
    second = tmp_path / "two" / "clip.mp4"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"same media content")
    second.write_bytes(b"same media content")
    assert bounded_fingerprint(first) == bounded_fingerprint(second)
    assert str(tmp_path).encode() not in bounded_fingerprint(first).encode()


def test_fingerprint_rejects_non_positive_chunk_size(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"content")
    with pytest.raises(VideoEditorError, match="chunk_bytes"):
        bounded_fingerprint(path, chunk_bytes=0)


def test_custom_extensions_are_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "clip.MOV").write_bytes(b"video")
    (tmp_path / "clip.mp4").write_bytes(b"ignored")
    sources = discover_sources(tmp_path, frozenset({".mov"}))
    assert [s.path.name for s in sources] == ["clip.MOV"]
    assert VIDEO_EXTENSIONS
