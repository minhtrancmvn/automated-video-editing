from pathlib import Path

import pytest

from video_editor.errors import VideoEditorError
from video_editor.media.storage import (
    VolumeIdentity,
    assert_expected_volume,
    assert_free_space,
    inspect_volume,
    is_within,
)


def test_volume_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    actual = inspect_volume(tmp_path)
    wrong = VolumeIdentity(actual.device + 1, actual.mount_point, actual.filesystem)
    with pytest.raises(VideoEditorError, match="volume changed"):
        assert_expected_volume(tmp_path, wrong)


def test_missing_volume_is_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "workspace"
    with pytest.raises(VideoEditorError, match="not mounted"):
        inspect_volume(missing)
    assert not missing.exists()


def test_free_space_reserve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("video_editor.media.storage.shutil.disk_usage", lambda _: (100, 50, 10))
    with pytest.raises(VideoEditorError, match="insufficient free space"):
        assert_free_space(tmp_path, required_bytes=1, reserve_bytes=10)
    assert_free_space(tmp_path, required_bytes=0, reserve_bytes=10)


def test_cleanup_containment(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    assert is_within(root, root / "cache" / "partial.mp4")
    assert not is_within(root, tmp_path / "outside.mp4")


def test_filesystem_policy_accepts_apfs_and_exfat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("video_editor.media.storage._filesystem", lambda _: "apfs")
    assert inspect_volume(tmp_path).filesystem == "apfs"
    monkeypatch.setattr("video_editor.media.storage._filesystem", lambda _: "exfat")
    assert inspect_volume(tmp_path).filesystem == "exfat"
