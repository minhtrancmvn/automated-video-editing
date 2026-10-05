from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from video_editor.cli import _service, app
from video_editor.config import AppConfig, GeminiSettings, PathSettings


def test_cli_exposes_phase_one_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("inspect", "plan", "render-from-plan", "run", "status", "resume"):
        assert command in result.stdout


@pytest.mark.parametrize("cloud_enabled", [True, False])
def test_service_wires_production_pricing_only_for_cloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cloud_enabled: bool
) -> None:
    paths = PathSettings(
        input_dir=tmp_path / "input",
        workspace_dir=tmp_path / "workspace",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
    )
    config = AppConfig(
        paths=paths,
        storage_reserve_bytes=10 * 1024**3,
        cloud_enabled=cloud_enabled,
        gemini=GeminiSettings(enabled=cloud_enabled),
    )
    adapter = Mock()
    factory = Mock(return_value=adapter)
    monkeypatch.setattr("video_editor.cli.resolve_config", lambda _path: config)
    monkeypatch.setattr(
        "video_editor.cli.WorkflowService.validate_configured_roots",
        lambda *_args: None,
    )
    monkeypatch.setattr("video_editor.cli.GeminiAdapter.from_environment", factory)

    service, store = _service(tmp_path / "config.toml")
    try:
        if cloud_enabled:
            assert factory.call_count == 1
            (pricing,) = tuple(factory.call_args.kwargs.values())
            assert pricing.model == config.gemini.model
            assert pricing.audio_input_usd_per_million_tokens == Decimal("0.75")
            assert service.provider is adapter
        else:
            factory.assert_not_called()
            assert service.provider is None
    finally:
        store.__exit__(None, None, None)


def test_cloud_service_rejects_expired_pricing_before_adapter_or_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = PathSettings(
        input_dir=tmp_path / "input",
        workspace_dir=tmp_path / "workspace",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
    )
    config = AppConfig(
        paths=paths,
        storage_reserve_bytes=10 * 1024**3,
        cloud_enabled=True,
        gemini=GeminiSettings(enabled=True),
    )
    factory = Mock()
    monkeypatch.setattr("video_editor.cli.resolve_config", lambda _path: config)
    monkeypatch.setattr(
        "video_editor.cli.WorkflowService.validate_configured_roots",
        lambda *_args: None,
    )
    monkeypatch.setattr("video_editor.cli.GeminiAdapter.from_environment", factory)
    monkeypatch.setattr(
        "video_editor.analysis.pricing.datetime",
        Mock(now=lambda _tz: Mock(date=lambda: date(2027, 1, 1))),
    )

    from video_editor.errors import VideoEditorError

    with pytest.raises(VideoEditorError) as raised:
        _service(tmp_path / "config.toml")

    assert raised.value.code == "pricing_unknown"
    factory.assert_not_called()
    assert not (paths.state_dir / "jobs.sqlite3").exists()
