"""Synthetic registered-input bounds and authorization mechanics."""

from decimal import Decimal

import pytest
from test_analysis_orchestrator import _save_chunk
from test_selection_gate import proposal

from video_editor.analysis import evaluation
from video_editor.persistence.database import JobStore

D = Decimal


def test_input_rejects_manifest_from_other_job(tmp_path):
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        _save_chunk(store, job_id, "chunk-1", D(0))
        record = store.get_proxy_manifest_for_job(job_id, "manifest-chunk-1")
        with pytest.raises(ValueError, match="lineage"):
            evaluation.EvaluationInput(
                proposal=proposal(),
                manifest_id="manifest-chunk-1",
                manifest=record["data"],
            )


def test_missing_evaluation_approval_cannot_call_provider_factory(tmp_path):
    called = []
    with pytest.raises(ValueError, match="approval"):
        evaluation.require_evaluation_approval(None, None)
    assert called == []


def test_non_finite_approval_caps_refused():
    with pytest.raises(ValueError):
        evaluation.EvaluationApproval(
            approval_id="synthetic",
            plan_digest="a" * 64,
            maximum_generations=1,
            maximum_cost_usd=D("NaN"),
        )
