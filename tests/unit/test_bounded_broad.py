"""Offline nominal budget and durable no-replay guards."""

from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from video_editor import bounded_broad
from video_editor.config import AppConfig, PathSettings
from video_editor.errors import VideoEditorError


def config_at(root: Path) -> AppConfig:
    return AppConfig(
        paths=PathSettings(
            *(
                root / name
                for name in ("input", "workspace", "cache", "output", "state")
            )
        ),
        storage_reserve_bytes=0,
    )


@pytest.mark.parametrize(
    "start,end",
    [
        ("NaN", "720"),
        ("0", "Infinity"),
        ("-1", "1"),
        ("1", "1"),
        ("2", "1"),
        ("0", "900"),
    ],
)
def test_invalid_range_or_cost_never_creates_provider_or_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start: str,
    end: str,
) -> None:
    config = config_at(tmp_path)
    factory = Mock(side_effect=AssertionError("provider forbidden"))
    monkeypatch.setattr(bounded_broad.GeminiAdapter, "from_environment", factory)
    with pytest.raises(VideoEditorError):
        bounded_broad.run_bounded_broad(
            source=tmp_path / "missing.MP4",
            start=Decimal(start),
            end=Decimal(end),
            config=config,
            dispatch=True,
        )
    factory.assert_not_called()
    assert not config.paths.state_dir.exists()


@pytest.mark.parametrize("attempt_id", ["..", ".", "../escape", "/escape", "", "a/b"])
def test_claim_rejects_path_traversal(tmp_path: Path, attempt_id: str) -> None:
    with pytest.raises(VideoEditorError, match="invalid bounded attempt ID"):
        bounded_broad._claim_attempt(tmp_path, attempt_id)
    assert not (tmp_path / "bounded-attempts").exists()


def test_claim_rejects_symlink_root_without_changing_target(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "unrelated"
    target.mkdir(mode=0o755)
    before = target.stat().st_mode
    (state / "bounded-attempts").symlink_to(target, target_is_directory=True)
    with pytest.raises(VideoEditorError, match="root is unsafe"):
        bounded_broad._claim_attempt(state, "once")
    assert target.stat().st_mode == before
    assert not list(target.iterdir())


def test_partial_claim_blocks_replay_after_metadata_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = bounded_broad.os.open

    def fail_metadata(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if Path(path).name == "claimed.json":
            raise OSError("fixture write failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(bounded_broad.os, "open", fail_metadata)
    with pytest.raises(VideoEditorError, match="cannot safely claim"):
        bounded_broad._claim_attempt(tmp_path, "partial")
    with pytest.raises(VideoEditorError, match="refusing replay"):
        bounded_broad._claim_attempt(tmp_path, "partial")
