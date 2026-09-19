from pathlib import Path

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
