"""No-dispatch CLI and workflow admission use synthetic exported inputs only."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from test_evaluation_execution import FakeProvider, run
from test_evaluation_execution import (
    context as context,  # noqa: PLC0414 - pytest fixture export
)
from typer.testing import CliRunner

from video_editor.analysis import evaluation as e
from video_editor.cli import app


def test_evaluation_cli_preflight_without_config_or_credentials(context, tmp_path):
    plan = context[2]
    exported = tmp_path / "exported-plan.json"
    exported.write_text(plan.model_dump_json())
    output = CliRunner().invoke(app, ["bounded-evaluation", str(exported)])
    assert output.exit_code == 0, output.output
    assert '"dispatch": false' in output.output
    assert '"maximum_generations": 2' in output.output
    assert '"upload_proxy_interval": ["0", "1"]' in output.output
    assert '"audio_supplied": true' in output.output
    assert '"review_source_interval": ["720.1", "720.9"]' in output.output
    assert not context[1].state_dir.exists()


def test_dispatch_missing_credentials_refused_before_output_and_database(
    context, tmp_path, monkeypatch
):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(context[2].model_dump_json())
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(context[3].model_dump_json())
    output_path = tmp_path / "audit.json"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from video_editor.bounded_evaluation import bounded_evaluation

    with pytest.raises(Exception, match="GEMINI_API_KEY"):
        bounded_evaluation(
            plan_path=plan_path,
            dispatch=True,
            approval_path=approval_path,
            config_path=tmp_path / "missing.toml",
            output_path=output_path,
        )
    assert not output_path.exists()


@pytest.fixture
def dispatch_inputs(context, tmp_path, monkeypatch):
    import video_editor.bounded_evaluation as entry

    store, paths, plan, approval, _ = context
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json())
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(approval.model_dump_json())
    paths.state_dir.mkdir()
    (paths.state_dir / "jobs.sqlite3").write_bytes(b"synthetic placeholder")
    monkeypatch.setattr(entry, "load_gemini_api_key", lambda _: "synthetic-key")
    monkeypatch.setattr(
        entry,
        "resolve_config",
        lambda _: SimpleNamespace(
            cloud_enabled=True, gemini=SimpleNamespace(model=plan.model), paths=paths
        ),
    )
    monkeypatch.setattr(entry, "JobStore", lambda _: nullcontext(store))
    provider = FakeProvider()
    monkeypatch.setattr(entry, "GeminiEvaluationProvider", lambda **_: provider)
    return (
        entry,
        provider,
        {
            "plan_path": plan_path,
            "dispatch": True,
            "approval_path": approval_path,
            "config_path": tmp_path / "synthetic.toml",
            "output_path": tmp_path / "audit.json",
        },
    )


@pytest.mark.parametrize("invalid_output", ["existing", "missing_parent"])
def test_unusable_audit_path_refused_before_dispatch(
    dispatch_inputs, context, invalid_output
):
    entry, provider, kwargs = dispatch_inputs
    output = kwargs["output_path"]
    if invalid_output == "existing":
        output.write_text("earlier audit must survive")
    else:
        kwargs["output_path"] = output.parent / "missing" / "audit.json"
    with pytest.raises(OSError):
        entry.bounded_evaluation(**kwargs)
    assert provider.calls == []
    assert not (context[1].state_dir / "evaluation-attempts").exists()
    assert (
        context[0]
        .connection.execute("SELECT count(*) FROM evaluation_requests")
        .fetchone()[0]
        == 0
    )
    if invalid_output == "existing":
        assert output.read_text() == "earlier audit must survive"


def test_audit_storage_probe_failure_refused_before_dispatch(
    dispatch_inputs, context, monkeypatch
):
    entry, provider, kwargs = dispatch_inputs

    def fail_sync(_):
        raise OSError("synthetic storage sync failure")

    with monkeypatch.context() as patch:
        patch.setattr(entry.os, "fsync", fail_sync)
        with pytest.raises(OSError, match="storage sync failure"):
            entry.bounded_evaluation(**kwargs)
    assert provider.calls == []
    assert not (context[1].state_dir / "evaluation-attempts").exists()


def test_dispatch_persists_complete_bundle_to_exclusive_output(dispatch_inputs):
    entry, provider, kwargs = dispatch_inputs
    result = entry.bounded_evaluation(**kwargs)
    bundle = e.EvaluationBundle.model_validate_json(kwargs["output_path"].read_bytes())
    assert result["audit_digest"] == bundle.audit_digest
    assert bundle.complete is True
    assert provider.calls == ["upload", "ground", "delete", "compare"]
    assert result["render_ready"] is False
    assert result["paid_dispatch_authorized"] is False


def test_predispatch_refusal_leaves_only_pending_marker(dispatch_inputs, context):
    entry, provider, kwargs = dispatch_inputs
    context[0].connection.execute(
        "UPDATE proxy_manifests SET data_json = replace(data_json, 'mapping-v1', 'mapping-changed')"
    )
    context[0].connection.commit()
    with pytest.raises(ValueError, match="registered"):
        entry.bounded_evaluation(**kwargs)
    assert kwargs["output_path"].read_text() == (
        '{"complete":false,"status":"pending"}\n'
    )
    assert provider.calls == []
    assert not (context[1].state_dir / "evaluation-attempts").exists()


def test_late_audit_sync_failure_preserves_receipts_and_blocks_replay(
    dispatch_inputs, context, monkeypatch
):
    entry, provider, kwargs = dispatch_inputs
    original_sync = entry.os.fsync

    def fail_after_comparison(descriptor):
        if "compare" in provider.calls:
            raise OSError("synthetic final audit sync failure")
        original_sync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(entry.os, "fsync", fail_after_comparison)
        with pytest.raises(OSError, match="final audit sync failure"):
            entry.bounded_evaluation(**kwargs)
    assert provider.calls == ["upload", "ground", "delete", "compare"]
    rows = (
        context[0]
        .connection.execute("SELECT status FROM evaluation_requests")
        .fetchall()
    )
    assert [row[0] for row in rows] == ["completed", "completed"]
    assert context[0].budget_state(context[2].job_id).reserved_usd == 0
    with pytest.raises(FileExistsError):
        entry.bounded_evaluation(**kwargs)
    assert provider.calls == ["upload", "ground", "delete", "compare"]


def test_dispatch_missing_approval_refused_before_config(context, tmp_path):
    exported = tmp_path / "exported-plan.json"
    exported.write_text(context[2].model_dump_json())
    output = CliRunner().invoke(
        app, ["bounded-evaluation", str(exported), "--dispatch"]
    )
    assert output.exit_code != 0
    assert "approval" in output.output
    assert not context[1].state_dir.exists()


def test_evaluated_workflow_revalidates_bundle_each_review_access(context):
    from video_editor.evaluated_workflow import EvaluatedWorkflowService

    bundle = run(context, FakeProvider())
    store, paths, _, _, media = context
    service = EvaluatedWorkflowService(
        SimpleNamespace(cloud_enabled=True, paths=paths),
        store,
        evaluation_bundle=bundle,
    )
    assert service.selection_reviews[0].evaluator_version == e.EVALUATION_VERSION
    media.write_bytes(b"changed synthetic evidence")
    with pytest.raises(ValueError, match="evidence"):
        _ = service.selection_reviews


def test_evaluated_workflow_constructs_with_closed_store(context):
    from video_editor.evaluated_workflow import EvaluatedWorkflowService

    store, paths, _, _, _ = context
    bundle = run(context, FakeProvider())
    store.__exit__(None, None, None)
    service = EvaluatedWorkflowService(
        SimpleNamespace(cloud_enabled=True, paths=paths),
        store,
        evaluation_bundle=bundle,
    )
    with pytest.raises(RuntimeError, match="JobStore is not open"):
        _ = service.selection_reviews


def test_evaluated_workflow_cannot_accept_legacy_review_override(context):
    from video_editor.evaluated_workflow import EvaluatedWorkflowService

    bundle = run(context, FakeProvider())
    service = EvaluatedWorkflowService(
        SimpleNamespace(cloud_enabled=True, paths=context[1]),
        context[0],
        evaluation_bundle=bundle,
    )
    with pytest.raises(ValueError, match="legacy"):
        service.selection_reviews = (object(),)
