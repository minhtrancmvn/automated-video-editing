"""Command-line entry point for local video editing workflows."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer

from video_editor.analysis.gemini import GeminiAdapter
from video_editor.analysis.models import RetryPolicy
from video_editor.analysis.pricing import pricing_for_model
from video_editor.bounded_broad import run_bounded_broad
from video_editor.config import resolve_config
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.storage import inspect_volume
from video_editor.models.edit_plan import load_plan
from video_editor.persistence.database import JobStore
from video_editor.workflow import WorkflowService, error_exit_code

app = typer.Typer(help="Local-first generic video editor.")


def _service(
    config_path: Path, progress: Callable[[str], None] | None = None
) -> tuple[WorkflowService, JobStore]:
    config = resolve_config(config_path)
    if config.paths.state_dir.anchor == "/" and config.paths.state_dir.parts[1:2] == (
        "Volumes",
    ):
        inspect_volume(config.paths.state_dir)
    WorkflowService.validate_configured_roots(config, config.paths.input_dir)
    # Cloud mode builds the provider from the runtime key only; the key is never stored.
    provider = (
        GeminiAdapter.from_environment(
            pricing=pricing_for_model(config.gemini.model),
            retry_policy=RetryPolicy(max_attempts=config.gemini.max_transient_attempts),
        )
        if config.cloud_enabled
        else None
    )
    store = JobStore(config.paths.state_dir / "jobs.sqlite3")
    return WorkflowService(config, store, progress, provider=provider), store


def _invoke(config_path: Path, action: str, *args: object) -> None:
    try:
        if action == "render_from_plan" and args and isinstance(args[0], Path):
            plan_path = args[0]
            if plan_path.is_file():
                config = resolve_config(config_path)
                plan = load_plan(plan_path)
                WorkflowService.validate_configured_roots(
                    config,
                    config.paths.input_dir,
                    [source.path for source in plan.sources],
                )
        progress = lambda message: typer.echo(message, err=True)
        service, store = _service(config_path, progress)
        with store:
            result = getattr(service, action)(*args)
        typer.echo(result)
    except VideoEditorError as exc:
        code = f" [{exc.code}]" if exc.code else ""
        typer.echo(f"{exc.category}{code}: {exc}", err=True)
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


@app.command("bounded-broad")
def bounded_broad(
    source: Path,
    config: ConfigOption,
    start: str = typer.Option(..., "--start"),
    end: str = typer.Option(..., "--end"),
    attempt_id: str = typer.Option(..., "--attempt-id"),
    dispatch: bool = typer.Option(False, "--dispatch"),
) -> None:
    """Preflight one bounded broad request; dispatch only with --dispatch."""
    try:
        try:
            start_value = Decimal(start)
            end_value = Decimal(end)
        except InvalidOperation as exc:
            raise VideoEditorError(
                ErrorCategory.ANALYSIS, "start and end must be decimal seconds"
            ) from exc
        resolved_config = resolve_config(config)
        result = run_bounded_broad(
            source=source,
            start=start_value,
            end=end_value,
            attempt_id=attempt_id,
            config=resolved_config,
            dispatch=dispatch,
        )
        typer.echo(result)
    except (VideoEditorError, OSError, InvalidOperation) as exc:
        if isinstance(exc, VideoEditorError):
            typer.echo(f"{exc.category}: {exc}", err=True)
            raise typer.Exit(code=error_exit_code(exc)) from exc
        typer.echo(f"bounded broad preflight failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("bounded-evaluation")
def bounded_evaluation(
    plan_path: Path,
    dispatch: bool = typer.Option(False, "--dispatch"),
    approval: Annotated[Path | None, typer.Option("--approval")] = None,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Price exported evaluation plan; dispatch requires separate exact approval."""
    import json

    from video_editor.bounded_evaluation import bounded_evaluation as execute

    try:
        result = execute(
            plan_path=plan_path,
            dispatch=dispatch,
            approval_path=approval,
            config_path=config,
            output_path=output,
        )
        typer.echo(json.dumps(result, sort_keys=True))
    except (ValueError, OSError, VideoEditorError):
        typer.echo(
            "bounded evaluation refused: check exact plan, approval and local ledger",
            err=True,
        )
        raise typer.Exit(code=1) from None


@app.command("prepare-reviewed")
def prepare_reviewed(
    bundle_path: Path,
    config: Annotated[Path, typer.Option("--config")],
    output: Annotated[Path, typer.Option("--output")],
) -> None:
    """Prepare reviewable V2 drafts from already persisted evidence, offline."""
    import json
    import sqlite3

    from video_editor.reviewed import prepare_reviewed as execute

    try:
        result = execute(
            bundle_path=bundle_path, config_path=config, output_path=output
        )
        typer.echo(json.dumps(result, sort_keys=True))
    except (ValueError, OSError, KeyError, sqlite3.Error, VideoEditorError):
        typer.echo(
            "reviewed preparation refused: check evidence and fresh output", err=True
        )
        raise typer.Exit(code=1) from None


@app.command("render-reviewed")
def render_reviewed(
    draft_path: Path,
    evaluation_bundle: Annotated[Path, typer.Option("--evaluation-bundle")],
    config: Annotated[Path, typer.Option("--config")],
    output: Annotated[Path, typer.Option("--output")],
    approval: Annotated[Path | None, typer.Option("--approval")] = None,
) -> None:
    """Render an exact approved draft locally into a fresh destination."""
    import json
    import sqlite3

    from video_editor.reviewed import render_reviewed as execute

    if approval is None:
        typer.echo("exact draft approval required", err=True)
        raise typer.Exit(code=1)
    try:
        result = execute(
            draft_path=draft_path,
            bundle_path=evaluation_bundle,
            approval_path=approval,
            config_path=config,
            output_path=output,
        )
        typer.echo(json.dumps(result, sort_keys=True))
    except (ValueError, OSError, KeyError, sqlite3.Error, VideoEditorError):
        typer.echo(
            "reviewed render refused: check exact approval and current evidence",
            err=True,
        )
        raise typer.Exit(code=1) from None


@app.command()
def resume(
    job_id: str,
    config: ConfigOption,
) -> None:
    """Resume interrupted workflow."""
    _invoke(config, "resume", job_id)
