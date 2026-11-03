"""Replay safety at real stage boundary, without running providers or renders."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.workflow import WorkflowService


def test_analyze_stage_never_reuses_old_completed_output():
    service = WorkflowService(SimpleNamespace(cloud_enabled=True), Mock())
    stale = {"candidates": [{"refined": {"candidate_id": "unreviewed"}}]}
    service.store.get_job.return_value = {"stages": {"analyze": {"result": stale}}}
    operation = Mock(
        side_effect=VideoEditorError(
            ErrorCategory.ANALYSIS,
            "selection requires evidence",
            code="selection_evidence_required",
        )
    )
    with (
        patch.object(service, "_stage_reusable", return_value=True),
        patch.object(
            service, "_run_stage", side_effect=lambda *args, **kwargs: args[5]()
        ),
        pytest.raises(VideoEditorError) as error,
    ):
        service._phase2_stage(
            "job", "analyze", {}, {}, ErrorCategory.ANALYSIS, operation
        )
    assert error.value.code == "selection_evidence_required"
    operation.assert_called_once()


def test_provisional_selection_cannot_reuse_cached_highlight_plan():
    service = WorkflowService(
        SimpleNamespace(
            cloud_enabled=True, highlights=SimpleNamespace(), crop=SimpleNamespace()
        ),
        Mock(),
    )
    with (
        patch.object(
            service, "_phase2_stage", return_value={"plans": ["stale-plan.json"]}
        ) as stage,
        pytest.raises(VideoEditorError) as error,
    ):
        service._ensure_highlight_plan(
            "job", {}, {"selection": {"render_ready": False}}, {}
        )
    assert error.value.code == "selection_final_evidence_required"
    stage.assert_not_called()
