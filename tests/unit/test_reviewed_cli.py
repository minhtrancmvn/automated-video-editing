"""Local reviewed commands must not construct a credentialed workflow."""

import sqlite3

import pytest
from typer.testing import CliRunner

from video_editor import cli

runner = CliRunner()


def test_prepare_reviewed_is_explicit_local_command():
    result = runner.invoke(cli.app, ["prepare-reviewed", "--help"])
    assert result.exit_code == 0, result.output
    assert "--output" in result.output
    assert "--config" in result.output


def test_render_reviewed_missing_approval_stops_before_config(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("config/provider access before approval")

    monkeypatch.setattr(cli, "resolve_config", forbidden)
    monkeypatch.setattr(cli, "_service", forbidden)
    result = runner.invoke(
        cli.app,
        [
            "render-reviewed",
            str(tmp_path / "draft.json"),
            "--evaluation-bundle",
            str(tmp_path / "bundle.json"),
            "--config",
            str(tmp_path / "config.toml"),
            "--output",
            str(tmp_path / "render"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "exact draft approval required" in result.output
    assert not (tmp_path / "render").exists()


def test_prepare_reviewed_existing_output_stops_before_config(tmp_path, monkeypatch):
    output = tmp_path / "draft.json"
    output.write_bytes(b"preserve existing evidence")

    def forbidden(*args, **kwargs):
        raise AssertionError("config/provider access before collision check")

    monkeypatch.setattr(cli, "resolve_config", forbidden)
    monkeypatch.setattr(cli, "_service", forbidden)
    result = runner.invoke(
        cli.app,
        [
            "prepare-reviewed",
            str(tmp_path / "bundle.json"),
            "--config",
            str(tmp_path / "config.toml"),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "reviewed preparation refused" in result.output
    assert output.read_bytes() == b"preserve existing evidence"


def test_prepare_reviewed_provider_failure_details_are_not_printed(
    tmp_path, monkeypatch
):
    from video_editor import reviewed

    def refuse(**kwargs):
        raise ValueError("private arbitrary ledger content")

    monkeypatch.setattr(reviewed, "prepare_reviewed", refuse)
    result = runner.invoke(
        cli.app,
        [
            "prepare-reviewed",
            str(tmp_path / "bundle.json"),
            "--config",
            str(tmp_path / "config.toml"),
            "--output",
            str(tmp_path / "draft.json"),
        ],
    )
    assert result.exit_code == 1
    assert "private arbitrary ledger content" not in result.output
    assert "reviewed preparation refused" in result.output


@pytest.mark.parametrize("root_name", ["input", "state", "cache", "workspace"])
def test_draft_file_cannot_write_into_protected_tree(tmp_path, root_name):
    from video_editor.reviewed import _fresh_file

    protected = tmp_path / root_name
    protected.mkdir()
    output = protected / "draft.json"
    with pytest.raises(ValueError, match="protected"):
        _fresh_file(output, protected_roots=(protected,))
    assert not output.exists()


def test_draft_file_rejects_symlink_parent(tmp_path):
    from video_editor.reviewed import _fresh_file

    target = tmp_path / "evidence"
    target.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        _fresh_file(link / "draft.json")
    assert list(target.iterdir()) == []


def test_read_only_store_cannot_create_missing_ledger(tmp_path):
    from video_editor.reviewed import _ReadOnlyStore

    path = tmp_path / "missing-state" / "jobs.sqlite3"
    with pytest.raises(FileNotFoundError), _ReadOnlyStore(path):
        pytest.fail("missing store opened")
    assert not path.parent.exists()


def test_read_only_store_never_migrates_or_mutates_ledger(tmp_path, monkeypatch):
    from video_editor.persistence.database import JobStore
    from video_editor.reviewed import _ReadOnlyStore

    path = tmp_path / "synthetic.db"
    with JobStore(path) as store:
        job_id = store.create_job({}, {})
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("schema migration forbidden")

    monkeypatch.setattr(JobStore, "_create_schema", forbidden)
    with _ReadOnlyStore(path) as store:
        assert store.get_job(job_id)["job_id"] == job_id
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.connection.execute("DELETE FROM jobs")
    assert path.read_bytes() == before


def test_draft_destination_rechecks_authoritative_source_roots(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from video_editor import evaluated_workflow, reviewed
    from video_editor.analysis.evaluation import EvaluationBundle
    from video_editor.config import AppConfig, PathSettings

    paths = PathSettings(
        *(
            tmp_path / name
            for name in ("input", "workspace", "cache", "output", "state")
        )
    )
    source_root = tmp_path / "other-source"
    source_root.mkdir()
    output = source_root / "draft.json"
    bundle = tmp_path / "bundle.json"
    bundle.write_text("{}")
    monkeypatch.setattr(
        EvaluationBundle,
        "model_validate_json",
        lambda _: SimpleNamespace(
            audit=SimpleNamespace(plan=SimpleNamespace(inputs=()))
        ),
    )
    monkeypatch.setattr(reviewed, "resolve_config", lambda _: AppConfig(paths, 0))
    monkeypatch.setattr(reviewed, "_ReadOnlyStore", lambda _: nullcontext(object()))
    from test_edit_plan_v2 import plan

    current_plan = plan(kind="long", filename="long.mp4", plan_id="long")
    current_plan = current_plan.model_copy(
        update={
            "sources": [
                current_plan.sources[0].model_copy(
                    update={"path": source_root / "original.mp4"}
                )
            ]
        }
    )
    current_draft = evaluated_workflow.ReviewedDraft(
        job_id="synthetic",
        evaluation_digest="a" * 64,
        provenance_digest="b" * 64,
        plans=(current_plan,),
        tracks=(),
    )
    monkeypatch.setattr(
        evaluated_workflow.ReviewedWorkflowService,
        "__init__",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        evaluated_workflow.ReviewedWorkflowService,
        "prepare_reviewed_plans",
        lambda _: current_draft,
    )
    with pytest.raises(ValueError, match="protected"):
        reviewed.prepare_reviewed(
            bundle_path=bundle, config_path=tmp_path / "config.toml", output_path=output
        )
    assert not output.exists()
