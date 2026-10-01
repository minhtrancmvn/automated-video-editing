from pathlib import Path

import pytest

from video_editor.config import AppConfig, PathSettings
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.persistence.database import JobStore
from video_editor.workflow import WorkflowService, error_exit_code


def test_workflow_exposes_required_methods(tmp_path: Path) -> None:
    config = AppConfig(
        PathSettings(
            *(
                tmp_path / name
                for name in ("input", "workspace", "cache", "output", "state")
            )
        ),
        0,
    )
    with JobStore(config.paths.state_dir / "jobs.sqlite") as store:
        service = WorkflowService(config, store)
        assert all(
            callable(getattr(service, name))
            for name in (
                "inspect",
                "plan",
                "render_from_plan",
                "run",
                "status",
                "resume",
            )
        )


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (ErrorCategory.STORAGE, 11),
        (ErrorCategory.ANALYSIS, 17),
        (ErrorCategory.PROVIDER, 18),
        (ErrorCategory.BUDGET, 19),
    ],
)
def test_error_categories_have_stable_exit_codes(
    category: ErrorCategory, expected: int
) -> None:
    assert error_exit_code(VideoEditorError(category, "failure")) == expected


def test_error_safe_details_are_copied() -> None:
    details = {"request_id": "request-123"}
    error = VideoEditorError(
        ErrorCategory.PROVIDER, "provider failure", safe_details=details
    )
    details["request_id"] = "changed"
    assert error.safe_details == {"request_id": "request-123"}


def test_phase1_mode_keeps_six_stages_and_phase2_declares_nine(
    tmp_path: Path,
) -> None:
    from video_editor.workflow import (
        PHASE1_STAGES,
        PHASE2_STAGES,
        STAGE_IMPLEMENTATION_VERSIONS,
    )

    config = AppConfig(
        PathSettings(
            *(
                tmp_path / name
                for name in ("input", "workspace", "cache", "output", "state")
            )
        ),
        0,
    )
    with JobStore(config.paths.state_dir / "jobs.sqlite") as store:
        assert WorkflowService(config, store).stages == PHASE1_STAGES
    assert PHASE1_STAGES == ("inspect", "proxy", "plan", "render", "validate", "report")
    assert PHASE2_STAGES == (
        "inspect",
        "proxy",
        "segment",
        "analyze",
        "rank",
        "plan",
        "render",
        "validate",
        "report",
    )
    assert set(STAGE_IMPLEMENTATION_VERSIONS) == set(PHASE2_STAGES)
