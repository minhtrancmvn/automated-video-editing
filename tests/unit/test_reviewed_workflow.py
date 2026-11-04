"""Synthetic contracts for reviewed drafts; never evidence of real-edit quality."""

import json
import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_edit_plan_v2 import plan
from test_evaluation_execution import FakeProvider, run
from test_tracking import _raw_track

from video_editor import evaluated_workflow as module

pytest_plugins = ["test_evaluation_execution"]


def draft():
    return module.ReviewedDraft(
        job_id="synthetic",
        evaluation_digest="a" * 64,
        provenance_digest="b" * 64,
        plans=(plan(kind="long", filename="long.mp4", plan_id="long"),),
        tracks=(),
    )


def test_preparation_requires_completed_local_stages(context):
    bundle = run(context, FakeProvider())
    service = module.ReviewedWorkflowService(
        SimpleNamespace(cloud_enabled=True, paths=context[1]),
        context[0],
        evaluation_bundle=bundle,
    )
    with pytest.raises(ValueError, match="completed.*proxy"):
        service.prepare_reviewed_plans()


def test_unknown_bundle_cannot_prepare_drafts(context):
    bundle = run(context, FakeProvider(unknown=True))
    service = module.ReviewedWorkflowService(
        SimpleNamespace(cloud_enabled=True, paths=context[1]),
        context[0],
        evaluation_bundle=bundle,
    )
    with pytest.raises(ValueError, match="complete"):
        service.prepare_reviewed_plans()


def test_render_refuses_missing_approval_before_local_work(tmp_path):
    service = object.__new__(module.ReviewedWorkflowService)
    service.prepare_reviewed_plans = Mock(side_effect=AssertionError("too early"))
    with pytest.raises(ValueError, match="approval"):
        service.render_reviewed_plans(None, None, tmp_path / "output")
    service.prepare_reviewed_plans.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_render_refuses_wrong_digest_before_local_work(tmp_path):
    service = object.__new__(module.ReviewedWorkflowService)
    service.prepare_reviewed_plans = Mock(side_effect=AssertionError("too early"))
    approval = module.ReviewedRenderApproval(
        approval_id="synthetic", draft_digest="c" * 64
    )
    with pytest.raises(ValueError, match="approval"):
        service.render_reviewed_plans(draft(), approval, tmp_path / "output")
    service.prepare_reviewed_plans.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "extra",
    [{"paid_dispatch_authorized": True}, {"final_edit_accepted": True}, {"cost": 1}],
)
def test_render_approval_cannot_expand_scope(extra):
    with pytest.raises(ValueError):
        module.ReviewedRenderApproval(
            approval_id="synthetic", draft_digest="a" * 64, **extra
        )


@pytest.mark.parametrize("field", ["plans", "tracks", "metadata", "provenance_digest"])
def test_digest_pins_entire_draft(field):
    original = draft()
    if field == "plans":
        changed = original.model_copy(
            update={"plans": (*original.plans, plan(dedup_group="other"))}
        )
    elif field == "tracks":
        changed = original.model_copy(update={"tracks": (_raw_track(),)})
    elif field == "metadata":
        changed = original.model_copy(update={"metadata": {"setting": "changed"}})
    else:
        changed = original.model_copy(update={field: "c" * 64})
    assert module.reviewed_draft_digest(changed) != module.reviewed_draft_digest(
        original
    )


def test_draft_roundtrip_preserves_digest():
    original = draft()
    reloaded = module.ReviewedDraft.model_validate_json(original.model_dump_json())
    assert reloaded == original
    assert module.reviewed_draft_digest(reloaded) == module.reviewed_draft_digest(
        original
    )
    assert reloaded.paid_dispatch_authorized is False
    assert reloaded.final_edit_accepted is False


def test_empty_plan_set_refused():
    with pytest.raises(ValueError):
        module.ReviewedDraft(
            job_id="synthetic",
            evaluation_digest="a" * 64,
            provenance_digest="b" * 64,
            plans=(),
            tracks=(),
        )


def test_output_directory_requires_new_safe_child(tmp_path):
    protected = tmp_path / "input"
    protected.mkdir()
    with pytest.raises(ValueError, match="protected"):
        module._fresh_directory(protected / "output", (protected,))
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="fresh"):
        module._fresh_directory(existing, ())
    link = tmp_path / "alias"
    link.symlink_to(protected, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        module._fresh_directory(link / "output", ())
    with pytest.raises(ValueError, match="parent"):
        module._fresh_directory(tmp_path / "absent" / "output", ())
    assert not (protected / "output").exists()


def test_valid_draft_still_requires_current_evidence(tmp_path):
    original = draft()
    service = object.__new__(module.ReviewedWorkflowService)
    service.prepare_reviewed_plans = Mock(
        return_value=original.model_copy(update={"provenance_digest": "c" * 64})
    )
    approval = module.ReviewedRenderApproval(
        approval_id="synthetic", draft_digest=module.reviewed_draft_digest(original)
    )
    with pytest.raises(ValueError, match="current"):
        service.render_reviewed_plans(original, approval, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_refinement_requires_completed_billing(context):
    with pytest.raises(ValueError, match="completed.*billing"):
        module._completed_analysis_request(
            context[0], "synthetic-job", "missing", "a" * 64
        )


@pytest.mark.parametrize(
    "status", ["reserved", "dispatched", "billing_unknown", "released", "completed"]
)
def test_analysis_billing_admission_preserves_request_and_budget(tmp_path, status):
    from video_editor.analysis.models import RequestReservation
    from video_editor.persistence.database import JobStore
    from video_editor.reviewed import _ReadOnlyStore

    path = tmp_path / "synthetic.db"
    with JobStore(path) as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, Decimal(1))
        store.reserve_request(
            RequestReservation(
                request_id="synthetic-request",
                job_id=job_id,
                cache_key="synthetic-cache",
                mode="candidate",
                maximum_cost_usd=Decimal("0.25"),
            )
        )
        if status == "dispatched":
            store.mark_request_dispatched("synthetic-request")
        elif status == "billing_unknown":
            store.mark_request_billing_unknown("synthetic-request")
        elif status == "released":
            store.release_confirmed_nonbillable("synthetic-request")
        elif status == "completed":
            store.settle_request("synthetic-request", Decimal("0.10"))
        before_budget = store.budget_state(job_id)
    before_bytes = path.read_bytes()
    with _ReadOnlyStore(path) as store:
        if status == "completed":
            module._completed_analysis_request(
                store, job_id, "synthetic-request", "synthetic-cache"
            )
        else:
            with pytest.raises(ValueError, match="completed.*billing"):
                module._completed_analysis_request(
                    store, job_id, "synthetic-request", "synthetic-cache"
                )
        assert store.budget_state(job_id) == before_budget
    assert path.read_bytes() == before_bytes


@pytest.mark.parametrize("mismatch", ["job", "request", "cache"])
def test_completed_billing_must_match_exact_analysis_identity(tmp_path, mismatch):
    from video_editor.analysis.models import RequestReservation
    from video_editor.persistence.database import JobStore

    with JobStore(tmp_path / "synthetic.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, Decimal(1))
        store.reserve_request(
            RequestReservation(
                request_id="synthetic-request",
                job_id=job_id,
                cache_key="synthetic-cache",
                mode="candidate",
                maximum_cost_usd=Decimal("0.25"),
            )
        )
        store.settle_request("synthetic-request", Decimal("0.10"))
        values = [job_id, "synthetic-request", "synthetic-cache"]
        values[["job", "request", "cache"].index(mismatch)] = "wrong"
        with pytest.raises(ValueError, match="completed.*billing"):
            module._completed_analysis_request(store, *values)


@pytest.mark.parametrize("failure_point", ["render", "validation", "publication"])
def test_local_render_failure_retains_evidence_without_completion(
    tmp_path, monkeypatch, failure_point
):
    output = tmp_path / "render"
    source = tmp_path / "sources"
    source.mkdir()
    source_file = source / "original.mp4"
    source_file.write_bytes(b"synthetic source fixture")
    original = draft()
    original_plan = original.plans[0].model_copy(
        update={
            "sources": [
                original.plans[0].sources[0].model_copy(update={"path": source_file})
            ]
        }
    )
    original = original.model_copy(update={"plans": (original_plan,)})
    service = object.__new__(module.ReviewedWorkflowService)
    service.config = SimpleNamespace(
        paths=SimpleNamespace(
            **{
                name: tmp_path / name
                for name in ("input_dir", "state_dir", "cache_dir", "workspace_dir")
            }
        )
    )
    service._evaluation_bundle = SimpleNamespace(
        audit=SimpleNamespace(plan=SimpleNamespace(inputs=()))
    )
    service.prepare_reviewed_plans = Mock(return_value=original)
    service._reviewed_evidence = Mock(return_value=({}, {}, original.provenance_digest))
    service._destination_volume = Mock(return_value=object())
    approval = module.ReviewedRenderApproval(
        approval_id="synthetic-test-only",
        draft_digest=module.reviewed_draft_digest(original),
    )
    command = SimpleNamespace(final_path=output / ".staging" / "long.mp4")
    monkeypatch.setattr(module, "compile_render", lambda *args, **kwargs: command)

    def render(*args, **kwargs):
        if failure_point == "render":
            (output / ".staging" / "long.partial.mp4").write_bytes(b"partial")
            raise ValueError("private arbitrary renderer text")
        command.final_path.write_bytes(b"synthetic staged media")

    def validate(*args, **kwargs):
        if failure_point == "validation":
            raise ValueError("private arbitrary validator text")
        return object()

    monkeypatch.setattr(module, "run_render", render)
    monkeypatch.setattr(module, "validate_phase2_output", validate)
    if failure_point == "publication":

        def collide(*args, **kwargs):
            (output / "long.mp4").write_bytes(b"preserve collision")
            raise FileExistsError("private arbitrary publication text")

        monkeypatch.setattr(module, "_publish_reviewed_media", collide)
    with pytest.raises((ValueError, FileExistsError)):
        service.render_reviewed_plans(original, approval, output)
    assert not (output / "audit.json").exists()
    assert (output / "draft.json").exists()
    failure = json.loads((output / "failure.json").read_text())
    assert failure["status"] == "failed"
    assert failure["draft_digest"] == approval.draft_digest
    assert failure["published_outputs"] == []
    assert failure["paid_dispatch_authorized"] is False
    assert failure["final_edit_accepted"] is False
    assert "private arbitrary" not in (output / "failure.json").read_text()
    if failure_point == "render":
        assert (output / ".staging" / "long.partial.mp4").read_bytes() == b"partial"
    else:
        assert command.final_path.read_bytes() == b"synthetic staged media"
    if failure_point == "publication":
        assert (output / "long.mp4").read_bytes() == b"preserve collision"
    else:
        assert not (output / "long.mp4").exists()
    assert source_file.read_bytes() == b"synthetic source fixture"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS exclusive rename contract")
def test_media_publication_without_hardlinks_preserves_collisions(
    tmp_path, monkeypatch
):
    staged = tmp_path / "staged.mp4"
    final = tmp_path / "final.mp4"
    staged.write_bytes(b"validated staged media")

    def forbidden(*args, **kwargs):
        pytest.fail("exFAT cannot hardlink")

    monkeypatch.setattr(module.os, "link", forbidden)
    module._publish_reviewed_media(staged, final)
    assert final.read_bytes() == b"validated staged media"
    assert not staged.exists()
    staged.write_bytes(b"next attempt")
    with pytest.raises(FileExistsError):
        module._publish_reviewed_media(staged, final)
    assert staged.read_bytes() == b"next attempt"
    assert final.read_bytes() == b"validated staged media"
