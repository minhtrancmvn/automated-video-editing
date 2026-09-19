from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from video_editor.config import AppConfig, PathSettings
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.storage import inspect_volume
from video_editor.models.edit_plan import (
    EditPlan,
    Framing,
    OutputSpec,
    PlanSource,
    Provenance,
    TimelineClip,
)
from video_editor.persistence.database import JobStore
from video_editor.rendering.compiler import compile_render
from video_editor.workflow import IMPLEMENTATION_VERSION, WorkflowService, _hash


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        PathSettings(
            tmp_path / "input",
            tmp_path / "workspace",
            tmp_path / "cache",
            tmp_path / "output",
            tmp_path / "state",
        ),
        1,
    )


def test_compile_render_never_places_output_beside_source(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    plan = EditPlan(
        schema_version=1,
        planner_version="test",
        sources=[
            PlanSource(id="source", path=source, identity="id", duration=Decimal(1))
        ],
        clips=[
            TimelineClip(
                source_id="source",
                source_start=Decimal(0),
                source_end=Decimal(1),
                timeline_start=Decimal(0),
                speed=Decimal(1),
                framing=Framing(mode="center_crop"),
                selection_reason="test",
            )
        ],
        output=OutputSpec(
            kind="short",
            width=320,
            height=240,
            frame_rate=Decimal(10),
            codec="libx264",
        ),
        provenance=Provenance(planner="test"),
    )

    command = compile_render(plan, "ffmpeg", output_dir=tmp_path / "output")

    assert command.final_path.parent == (tmp_path / "output").resolve()
    assert command.final_path != source
    assert source.read_bytes() == b"source"


def test_resume_keeps_same_job_id_without_calling_public_job_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    input_path = tmp_path / "input"
    input_path.mkdir()
    (input_path / "clip.mp4").write_bytes(b"input")
    with JobStore(config.paths.state_dir / "jobs.sqlite") as store:
        service = WorkflowService(config, store)
        job_id = store.create_job(
            {"input_path": str(input_path)},
            {
                "device": inspect_volume(input_path).device,
                "mount_point": str(inspect_volume(input_path).mount_point),
                "filesystem": inspect_volume(input_path).filesystem,
            },
        )
        store.start_stage(job_id, "inspect", "input", "settings", "phase1-workflow-v1")
        artifact = config.paths.workspace_dir / f"{job_id}-inventory.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("{}")
        store.save_artifact(job_id, "inspect", artifact)
        store.complete_stage(job_id, "inspect", {"artifact": str(artifact)})
        monkeypatch.setattr(
            service, "inspect", Mock(side_effect=AssertionError("new job"))
        )
        monkeypatch.setattr(service, "run", Mock(side_effect=AssertionError("new job")))
        execute = Mock(return_value={"job_id": job_id})
        monkeypatch.setattr(service, "_execute", execute)

        result = service.resume(job_id)

    assert result["job_id"] == job_id
    execute.assert_called_once_with(job_id)


def test_report_write_failure_is_normalized_and_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    with JobStore(config.paths.state_dir / "jobs.sqlite") as store:
        service = WorkflowService(config, store)
        volume = inspect_volume(tmp_path)
        job_id = store.create_job(
            {
                "input_path": str(tmp_path / "input.mp4"),
                "destination_volumes": {
                    "workspace": {
                        "device": volume.device,
                        "mount_point": str(volume.mount_point),
                        "filesystem": volume.filesystem,
                    },
                    "cache": {
                        "device": volume.device,
                        "mount_point": str(volume.mount_point),
                        "filesystem": volume.filesystem,
                    },
                    "output": {
                        "device": volume.device,
                        "mount_point": str(volume.mount_point),
                        "filesystem": volume.filesystem,
                    },
                },
            },
            {
                "device": volume.device,
                "mount_point": str(volume.mount_point),
                "filesystem": volume.filesystem,
            },
        )
        for root in (
            config.paths.output_dir,
            config.paths.workspace_dir,
            config.paths.cache_dir,
        ):
            root.mkdir(parents=True)
        store.start_stage(job_id, "report", "input", "settings", "phase1-workflow-v2")
        monkeypatch.setattr(
            "video_editor.workflow.write_json_report",
            Mock(side_effect=OSError("read-only")),
        )
        with pytest.raises(VideoEditorError) as caught:
            service._write_report_stage(job_id, {})
        assert caught.value.category == ErrorCategory.OUTPUT
        assert store.get_job(job_id)["stages"]["report"]["status"] == "failed"


def test_stage_reuse_requires_matching_identity_and_valid_artifact(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}")
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.start_stage(
            job_id,
            "inspect",
            _hash("input-a"),
            _hash("settings-a"),
            IMPLEMENTATION_VERSION,
        )
        store.save_artifact(job_id, "inspect", artifact)
        store.complete_stage(job_id, "inspect", {"artifact": str(artifact)})
        service = WorkflowService(_config(tmp_path), store)
        job = store.get_job(job_id)
        assert service._stage_reusable(job, "inspect", "input-a", "settings-a")
        assert not service._stage_reusable(job, "inspect", "input-b", "settings-a")
        artifact.unlink()
        assert not service._stage_reusable(
            store.get_job(job_id), "inspect", "input-a", "settings-a"
        )
