from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from video_editor.cli import app


def test_cli_commands_require_config_and_expose_workflow(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[paths]\ninput_dir = \"{tmp_path}/input\"\nworkspace_dir = \"{tmp_path}/workspace\"\ncache_dir = \"{tmp_path}/cache\"\noutput_dir = \"{tmp_path}/output\"\nstate_dir = \"{tmp_path}/state\"\n[settings]\nstorage_reserve_bytes = 0\n"""
    )
    result = CliRunner().invoke(app, ["status", "missing", "--config", str(config)])
    assert result.exit_code != 0
    assert "state" in result.output


@pytest.mark.parametrize(
    ("args", "method", "expected"),
    [
        (["inspect", "/input"], "inspect", (Path("/input"),)),
        (["plan", "/input", "/output"], "plan", (Path("/input"), Path("/output"))),
        (["render-from-plan", "/plan.json"], "render_from_plan", (Path("/plan.json"),)),
        (["run", "/input"], "run", (Path("/input"),)),
        (["status", "job-1"], "status", ("job-1",)),
        (["resume", "job-1"], "resume", ("job-1",)),
    ],
)
def test_each_cli_command_invokes_matching_service_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    method: str,
    expected: tuple[object, ...],
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[paths]\ninput_dir='.'\nworkspace_dir='.'\ncache_dir='.'\noutput_dir='.'\nstate_dir='.'\n"
    )
    service = Mock()
    getattr(service, method).return_value = {"ok": True}
    store = Mock()
    store.__enter__ = Mock(return_value=store)
    store.__exit__ = Mock(return_value=None)
    monkeypatch.setattr("video_editor.cli._service", lambda _path: (service, store))

    result = CliRunner().invoke(app, [*args, "--config", str(config)])

    assert result.exit_code == 0, result.output
    getattr(service, method).assert_called_once_with(*expected)
