"""Command-line entry point for local video editing workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from video_editor.config import resolve_config
from video_editor.errors import VideoEditorError
from video_editor.media.storage import inspect_volume
from video_editor.persistence.database import JobStore
from video_editor.workflow import WorkflowService, error_exit_code

app = typer.Typer(help="Local-first generic video editor.")


def _service(config_path: Path) -> tuple[WorkflowService, JobStore]:
    config = resolve_config(config_path)
    if config.paths.state_dir.anchor == "/" and config.paths.state_dir.parts[1:2] == ("Volumes",):
        inspect_volume(config.paths.state_dir)
    store = JobStore(config.paths.state_dir / "jobs.sqlite3")
    return WorkflowService(config, store), store


def _invoke(config_path: Path, action: str, *args: object) -> None:
    try:
        service, store = _service(config_path)
        with store:
            result = getattr(service, action)(*args)
        typer.echo(result)
    except VideoEditorError as exc:
        typer.echo(f"{exc.category}: {exc}", err=True)
        raise typer.Exit(code=error_exit_code(exc)) from exc


ConfigOption = Annotated[Path, typer.Option("--config", exists=True, readable=True)]


@app.command()
def inspect(
    input_path: Path,
    config: ConfigOption,
) -> None:
    """Inspect media sources and report their capabilities."""
    _invoke(config, "inspect", input_path)


@app.command()
def plan(
    input_path: Path,
    output_path: Path,
    config: ConfigOption,
) -> None:
    """Build edit plans from source media and configuration."""
    _invoke(config, "plan", input_path, output_path)


@app.command("render-from-plan")
def render_from_plan(
    plan_path: Path,
    config: ConfigOption,
) -> None:
    """Render media from an existing edit plan."""
    _invoke(config, "render_from_plan", plan_path)


@app.command()
def run(
    input_path: Path,
    config: ConfigOption,
) -> None:
    """Run inspection, planning, proxy, rendering, validation, and reports."""
    _invoke(config, "run", input_path)


@app.command()
def status(
    job_id: str,
    config: ConfigOption,
) -> None:
    """Show workflow state and available outputs."""
    _invoke(config, "status", job_id)


@app.command()
def resume(
    job_id: str,
    config: ConfigOption,
) -> None:
    """Resume interrupted workflow."""
    _invoke(config, "resume", job_id)
