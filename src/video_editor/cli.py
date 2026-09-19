"""Command-line entry point for local video editing workflows."""

import typer

app = typer.Typer(help="Local-first generic video editor.")


def _not_implemented() -> None:
    typer.echo("not implemented", err=True)
    raise typer.Exit(code=2)


@app.command()
def inspect() -> None:
    """Inspect media sources and report their capabilities."""

    _not_implemented()


@app.command()
def plan() -> None:
    """Build an edit plan from source media and configuration."""

    _not_implemented()


@app.command("render-from-plan")
def render_from_plan() -> None:
    """Render media from an existing edit plan."""

    _not_implemented()


@app.command()
def run() -> None:
    """Run complete inspection, planning, and rendering workflow."""

    _not_implemented()


@app.command()
def status() -> None:
    """Show workflow state and available outputs."""

    _not_implemented()


@app.command()
def resume() -> None:
    """Resume an interrupted workflow."""

    _not_implemented()
