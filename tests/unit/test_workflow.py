from pathlib import Path

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


def test_error_categories_have_stable_exit_codes() -> None:
    assert error_exit_code(VideoEditorError(ErrorCategory.STORAGE, "no volume")) == 11
