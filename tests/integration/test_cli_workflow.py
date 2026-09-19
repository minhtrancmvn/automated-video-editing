from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from video_editor.cli import app
from video_editor.models.edit_plan import (
    EditPlan,
    Framing,
    OutputSpec,
    PlanSource,
    Provenance,
    TimelineClip,
    write_plan,
)


def test_service_rejects_missing_external_state_volume_before_store_open(
    tmp_path: Path,
) -> None:
    missing = Path("/Volumes") / f"video-editor-state-{tmp_path.name}"
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[paths]\ninput_dir = \"{missing}/input\"\nworkspace_dir = \"{missing}/workspace\"\ncache_dir = \"{missing}/cache\"\noutput_dir = \"{missing}/output\"\nstate_dir = \"{missing}/state\"\n[settings]\nstorage_reserve_bytes = 0\n"""
    )

    result = CliRunner().invoke(app, ["status", "missing", "--config", str(config)])

    assert result.exit_code == 11
    assert "not mounted" in result.output
    assert not missing.exists()


def test_state_overlap_is_rejected_before_database_parent_creation(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    state_dir = input_dir / "state"
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[paths]\ninput_dir = \"{input_dir}\"\nworkspace_dir = \"{tmp_path}/workspace\"\ncache_dir = \"{tmp_path}/cache\"\noutput_dir = \"{tmp_path}/output\"\nstate_dir = \"{state_dir}\"\n[settings]\nstorage_reserve_bytes = 0\n"""
    )

    result = CliRunner().invoke(app, ["status", "missing", "--config", str(config)])

    assert result.exit_code == 11
    assert "state root overlaps input/source root" in result.output
    assert not state_dir.exists()


def test_render_from_plan_source_overlap_rejected_before_database_parent_creation(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "clip.mp4"
    source.write_bytes(b"source")
    plan = tmp_path / "plan.json"
    write_plan(
        EditPlan(
            schema_version=1,
            planner_version="test",
            sources=[
                PlanSource(
                    id="source",
                    path=source,
                    identity="identity",
                    duration=1,
                    has_audio=False,
                )
            ],
            clips=[
                TimelineClip(
                    source_id="source",
                    source_start=0,
                    source_end=1,
                    timeline_start=0,
                    speed=1,
                    framing=Framing(mode="center_crop"),
                    selection_reason="test",
                )
            ],
            output=OutputSpec(
                kind="short",
                width=16,
                height=16,
                frame_rate=16,
                codec="libx264",
                audio="silence",
            ),
            provenance=Provenance(planner="test"),
        ),
        plan,
    )
    config = tmp_path / "config.toml"
    state_dir = source_dir / ".state"
    config.write_text(
        f"""[paths]\ninput_dir = \"{tmp_path / "input"}\"\nworkspace_dir = \"{tmp_path / "workspace"}\"\ncache_dir = \"{tmp_path / "cache"}\"\noutput_dir = \"{tmp_path / "output"}\"\nstate_dir = \"{state_dir}\"\n[settings]\nstorage_reserve_bytes = 0\n"""
    )

    result = CliRunner().invoke(
        app, ["render-from-plan", str(plan), "--config", str(config)]
    )

    assert result.exit_code == 11
    assert "state root overlaps input/source root" in result.output
    assert not state_dir.exists()


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
