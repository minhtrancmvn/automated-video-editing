"""Report wiring preserves unknown legacy plan diagnostics."""

from types import SimpleNamespace

from video_editor.workflow import WorkflowService


def test_phase2_report_includes_plan_diagnostics_without_retracking() -> None:
    config = SimpleNamespace(
        cloud_enabled=True,
        gemini=SimpleNamespace(model="gemini-3.8-flash", broad_fps="0.5"),
    )
    service = WorkflowService(config, SimpleNamespace())
    service._report_state = lambda *_args: {"status": "completed"}
    diagnostic = {"version": "short-diagnostics-v1", "candidates": []}
    state = service._phase2_report_state(
        "job",
        {},
        {},
        {},
        {"ranked": []},
        {"short_diagnostics": diagnostic},
        {"outputs": []},
    )
    assert state["analysis"]["short_diagnostics"] == diagnostic


def test_phase2_report_marks_legacy_diagnostics_not_recorded() -> None:
    config = SimpleNamespace(
        cloud_enabled=True,
        gemini=SimpleNamespace(model="gemini-3.8-flash", broad_fps="0.5"),
    )
    service = WorkflowService(config, SimpleNamespace())
    service._report_state = lambda *_args: {"status": "completed"}
    state = service._phase2_report_state(
        "job", {}, {}, {}, {"ranked": []}, {}, {"outputs": []}
    )
    assert state["analysis"]["short_diagnostics"] == {"status": "not_recorded"}
