from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from video_editor.config import AppConfig, PathSettings
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.discovery import (
    IDENTITY_VERSION,
    SourceCandidate,
    bounded_fingerprint,
)
from video_editor.media.probe import MediaProbe
from video_editor.media.proxies import ProxySettings
from video_editor.media.proxies import settings_hash as proxy_settings_hash
from video_editor.media.storage import VolumeIdentity, inspect_volume
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


def test_inspection_rerun_replaces_old_chronology_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    input_path = tmp_path / "input"
    input_path.mkdir()
    source = input_path / "GOPR0001.MP4"
    source.write_bytes(b"source")
    candidate = SourceCandidate(
        path=source,
        size_bytes=source.stat().st_size,
        discovery_index=0,
        fingerprint="source-id",
        identity_version=IDENTITY_VERSION,
    )
    probe = MediaProbe(path=source, duration=1)
    volume = inspect_volume(input_path)
    with JobStore(config.paths.state_dir / "jobs.sqlite") as store:
        job_id = store.create_job(
            {
                "input_path": str(input_path),
                "destination_volumes": {
                    "workspace": {
                        "device": volume.device,
                        "mount_point": str(volume.mount_point),
                        "filesystem": volume.filesystem,
                    }
                },
            },
            {
                "device": volume.device,
                "mount_point": str(volume.mount_point),
                "filesystem": volume.filesystem,
            },
        )
        service = WorkflowService(config, store)
        monkeypatch.setattr(service, "_discover", lambda _path: [candidate])
        monkeypatch.setattr("video_editor.workflow.probe_media", lambda _path: probe)
        monkeypatch.setattr(
            "video_editor.workflow.detect_capabilities",
            lambda: Mock(model_dump=lambda mode: {}),
        )
        monkeypatch.setattr(
            "video_editor.workflow.inspect_volume", lambda _path: volume
        )
        monkeypatch.setattr(
            "video_editor.workflow.assert_free_space", lambda *args: None
        )
        store.start_stage(
            job_id, "inspect", "first", "settings", IMPLEMENTATION_VERSION
        )
        service._inspect_stage(job_id, input_path)
        assert len(store.get_job(job_id)["chronology"]) == 1

        monkeypatch.setattr(
            "video_editor.workflow.sequence_sources", lambda _sources, _times: []
        )
        store.start_stage(
            job_id, "inspect", "second", "settings", IMPLEMENTATION_VERSION
        )
        service._inspect_stage(job_id, input_path)
        assert store.get_job(job_id)["chronology"] == []


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

    command = compile_render(plan, "ffmpeg", output_dir=tmp_path.parent / "output")

    assert command.final_path.parent == (tmp_path.parent / "output").resolve()
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


def test_validate_reuse_revalidates_every_recorded_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    plan_path = tmp_path / "plan.json"
    output_path = tmp_path / "output.mp4"
    plan_path.write_text("{}")
    output_path.write_bytes(b"output")
    rendered = {"outputs": [{"plan": str(plan_path), "output": str(output_path)}]}
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        service = WorkflowService(config, store)
        store.start_stage(
            job_id,
            "validate",
            _hash(rendered["outputs"]),
            _hash({"tolerance": "0.20"}),
            IMPLEMENTATION_VERSION,
        )
        store.complete_stage(
            job_id, "validate", {"outputs": [{"path": str(output_path)}]}
        )
        validate = Mock(side_effect=VideoEditorError(ErrorCategory.OUTPUT, "corrupt"))
        monkeypatch.setattr("video_editor.workflow.load_plan", lambda path: Mock())
        monkeypatch.setattr("video_editor.workflow.timeline_duration", lambda plan: 1)
        monkeypatch.setattr("video_editor.workflow.validate_output", validate)

        with pytest.raises(VideoEditorError, match="corrupt"):
            service._ensure_validate(job_id, rendered)

    assert validate.call_count == 2


def test_render_from_plan_requires_exact_identity_version_and_digest(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "roots")
    source = tmp_path / "sources" / "source.mp4"
    source.parent.mkdir()
    source.write_bytes(b"source")
    identity = f"{IDENTITY_VERSION}:{bounded_fingerprint(source)}"
    plan = EditPlan(
        schema_version=1,
        planner_version="test",
        sources=[
            PlanSource(
                id="source",
                path=source,
                identity=identity + "-stale",
                duration=Decimal(1),
            )
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
            audio="none",
        ),
        provenance=Provenance(planner="test"),
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json())
    with JobStore(tmp_path / "state.db") as store:
        service = WorkflowService(config, store)
        with pytest.raises(VideoEditorError, match="identity changed"):
            service.render_from_plan(plan_path)
        assert store.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_render_from_plan_rejects_changed_second_source_before_job_creation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    first = tmp_path / "sources-a" / "first.mp4"
    second = tmp_path / "sources-b" / "second.mp4"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    plan = EditPlan(
        schema_version=1,
        planner_version="test",
        sources=[
            PlanSource(
                id="first",
                path=first,
                identity=f"{IDENTITY_VERSION}:{bounded_fingerprint(first)}",
                duration=Decimal(1),
            ),
            PlanSource(
                id="second",
                path=second,
                identity=f"{IDENTITY_VERSION}:{bounded_fingerprint(second)}",
                duration=Decimal(1),
            ),
        ],
        clips=[
            TimelineClip(
                source_id="first",
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
            audio="none",
        ),
        provenance=Provenance(planner="test"),
    )
    second.write_bytes(b"changed")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json())
    with JobStore(tmp_path / "state.db") as store:
        service = WorkflowService(config, store)
        with pytest.raises(VideoEditorError, match="identity changed"):
            service.render_from_plan(plan_path)
        assert store.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_new_job_rejects_roots_overlapping_input_root(tmp_path: Path) -> None:
    input_path = tmp_path / "input"
    input_path.mkdir()
    config = AppConfig(
        PathSettings(
            input_path,
            input_path / "workspace",
            tmp_path / "cache",
            tmp_path / "output",
            tmp_path / "state",
        ),
        1,
    )
    with JobStore(config.paths.state_dir / "jobs.sqlite") as store:
        service = WorkflowService(config, store)
        with pytest.raises(VideoEditorError, match="overlaps input/source root"):
            service._new_job(input_path)


def test_plan_source_parents_each_protect_configured_roots(tmp_path: Path) -> None:
    source_a = tmp_path / "source-a" / "a.mp4"
    source_b = tmp_path / "source-b" / "b.mp4"
    source_a.parent.mkdir()
    source_b.parent.mkdir()
    source_a.write_bytes(b"a")
    source_b.write_bytes(b"b")
    config = AppConfig(
        PathSettings(
            tmp_path / "unused-input",
            tmp_path / "source-b" / "workspace",
            tmp_path / "cache",
            tmp_path / "output",
            tmp_path / "state",
        ),
        1,
    )
    with JobStore(tmp_path / "state.db") as store:
        service = WorkflowService(config, store)
        with pytest.raises(VideoEditorError, match="workspace root overlaps"):
            service._validate_roots(source_a.parent, [source_a, source_b])


def test_destination_volume_rejects_mount_point_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    with JobStore(config.paths.state_dir / "jobs.sqlite") as store:
        service = WorkflowService(config, store)
        expected = VolumeIdentity(1, Path("/mounted-a"), "apfs")
        actual = VolumeIdentity(1, Path("/mounted-b"), "apfs")
        monkeypatch.setattr("video_editor.workflow.inspect_volume", lambda path: actual)
        monkeypatch.setattr(
            "video_editor.workflow.assert_free_space", lambda *args: None
        )
        with pytest.raises(VideoEditorError, match="volume changed"):
            service._destination_volume(tmp_path / "output", 1, expected)


def test_report_aggregates_probe_warnings_without_render_fallbacks(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with JobStore(config.paths.state_dir / "jobs.sqlite") as store:
        job_id = store.create_job({}, {})
        store.save_sources(job_id, [{"source_id": "s1", "size_bytes": 1}])
        service = WorkflowService(config, store)
        state = service._report_state(
            job_id,
            {"warnings": [{"code": "hevc", "message": "source uses HEVC"}]},
            {"plans": []},
            {"outputs": [{"warnings": ["software encoder fallback"]}]},
            {"outputs": []},
        )
    assert state["warnings"] == [{"code": "hevc", "message": "source uses HEVC"}]
    assert state["fallbacks"] == ["software encoder fallback"]
    assert state["estimated_peak_space_scope"] == (
        "render-output byte growth only; workspace and cache excluded"
    )
    assert state["estimated_peak_space_bytes"] == 1


def test_report_aggregates_chronology_warnings_with_source_context(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    chronology = [
        {
            "group_id": "12-session-1",
            "warnings": ["session ambiguity: timestamps tied"],
            "members": [
                {
                    "source_id": "source-2",
                    "warnings": ["duplicate chapter: file 12 chapter 2"],
                }
            ],
        }
    ]
    with JobStore(config.paths.state_dir / "jobs.sqlite") as store:
        job_id = store.create_job({}, {})
        service = WorkflowService(config, store)
        state = service._report_state(
            job_id,
            {"warnings": [], "chronology": chronology},
            {"plans": []},
            {"outputs": []},
            {"outputs": []},
        )
    assert state["chronology_warnings"] == [
        {
            "group_id": "12-session-1",
            "source_id": None,
            "warning": "session ambiguity: timestamps tied",
        },
        {
            "group_id": "12-session-1",
            "source_id": "source-2",
            "warning": "duplicate chapter: file 12 chapter 2",
        },
    ]
    assert state["warnings"] == state["chronology_warnings"]


def test_interrupted_render_preserves_valid_output_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    with JobStore(tmp_path / "state.db") as store:
        service = WorkflowService(config, store)
        job_id = store.create_job({}, {})
        first_plan = tmp_path / "first.json"
        second_plan = tmp_path / "second.json"
        first_plan.write_text("first")
        second_plan.write_text("second")
        valid_output = tmp_path / "long.mp4"
        valid_output.write_bytes(b"valid")
        monkeypatch.setattr(
            service,
            "_artifact_valid",
            lambda name, path, metadata=None: path == valid_output,
        )
        monkeypatch.setattr(service, "_destination_volume", lambda *args: None)
        monkeypatch.setattr(
            service, "_expected_destination_volume", lambda *args: Mock()
        )
        monkeypatch.setattr(
            "video_editor.workflow.load_plan",
            lambda path: Mock(output=Mock(width=16, height=16)),
        )
        monkeypatch.setattr("video_editor.workflow._duration_seconds", lambda plan: 1)
        store.start_stage(
            job_id, "render", "source", "settings", IMPLEMENTATION_VERSION
        )
        store.save_artifact(
            job_id, "render", valid_output, {"plan": str(first_plan), "warnings": []}
        )
        store.fail_stage(job_id, "render", "rendering", "stopped", interrupted=True)
        render_plan = Mock(
            return_value={
                "plan": str(second_plan),
                "output": str(tmp_path / "short.mp4"),
            }
        )
        monkeypatch.setattr(service, "_render_plan", render_plan)
        result = service._ensure_render(
            job_id, {"plans": [str(first_plan), str(second_plan)]}
        )
    assert [item["plan"] for item in result["outputs"]] == [
        str(first_plan),
        str(second_plan),
    ]
    render_plan.assert_called_once_with(job_id, second_plan)


def test_resume_persists_and_reuses_final_output_without_old_artifact_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    plan_path = tmp_path / "long-plan.json"
    plan_path.write_text("plan")
    output_path = config.paths.output_dir / "job" / "long.mp4"
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"valid")
    plan = Mock(output=Mock(width=1920, height=1080))
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        service = WorkflowService(config, store)
        monkeypatch.setattr("video_editor.workflow.load_plan", lambda _path: plan)
        monkeypatch.setattr(
            "video_editor.workflow.timeline_duration", lambda _plan: Decimal(1)
        )
        monkeypatch.setattr(
            service,
            "_configured_destination",
            lambda _root, _job_id: output_path.parent,
        )
        monkeypatch.setattr(service, "_destination_volume", lambda *args: Mock())
        monkeypatch.setattr(
            service, "_expected_destination_volume", lambda *args: Mock()
        )
        monkeypatch.setattr(
            service,
            "_artifact_valid",
            lambda _name, path, _metadata=None: path == output_path,
        )
        render = Mock(side_effect=AssertionError("existing final must be reused"))
        monkeypatch.setattr(service, "_render_plan", render)

        result = service._ensure_render(job_id, {"plans": [str(plan_path)]})
        artifacts = store.get_job(job_id)["artifacts"]

    assert result["outputs"][0]["output"] == str(output_path)
    assert artifacts == [
        {
            "stage": "render",
            "name": "long.mp4",
            "path": str(output_path),
            "metadata": {"plan": str(plan_path), "warnings": []},
        }
    ]
    render.assert_not_called()


def test_run_stage_persists_keyboard_interrupt_as_interrupted(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        service = WorkflowService(config, store)
        with pytest.raises(VideoEditorError) as caught:
            service._run_stage(
                job_id,
                "inspect",
                "input",
                "settings",
                ErrorCategory.INSPECTION,
                lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
            )
        state = store.get_job(job_id)

    assert caught.value.interrupted
    assert state["status"] == "interrupted"
    assert state["stages"]["inspect"]["status"] == "interrupted"


@pytest.mark.parametrize(
    "legacy_identity", ["legacy:anything", f"{IDENTITY_VERSION}:other-digest"]
)
def test_proxy_artifact_requires_exact_current_source_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_identity: str
) -> None:
    config = _config(tmp_path)
    artifact = tmp_path / "proxy.mp4"
    artifact.write_bytes(b"proxy")
    metadata = {
        "source_id": "source",
        "source_identity": legacy_identity,
        "settings": {"max_width": 960, "fps": 15, "video_codec": "libx264"},
        "settings_hash": proxy_settings_hash(ProxySettings()),
        "tool_version": "ffmpeg",
        "kind": "proxy",
        "mapping": {
            "source_id": "source",
            "source_identity": legacy_identity,
            "settings_hash": proxy_settings_hash(ProxySettings()),
            "tool_version": "ffmpeg",
        },
    }
    monkeypatch.setattr(
        "video_editor.workflow.valid_cached_media", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        "video_editor.workflow.bounded_fingerprint", lambda _path: "digest"
    )
    with JobStore(tmp_path / "state.db") as store:
        service = WorkflowService(config, store)
        assert not service._proxy_artifact_valid(artifact, metadata)


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
