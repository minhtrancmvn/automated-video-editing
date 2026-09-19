from typer.testing import CliRunner

from video_editor.cli import app


def test_cli_exposes_phase_one_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("inspect", "plan", "render-from-plan", "run", "status", "resume"):
        assert command in result.stdout
