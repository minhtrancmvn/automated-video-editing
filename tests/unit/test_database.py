from pathlib import Path

from video_editor.persistence.database import JobStatus, JobStore, StageStatus


def test_failed_stage_retains_prior_completed_stage(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "inspect", "a", "b", "v1")
        store.complete_stage(job, "inspect", "{}")
        store.start_stage(job, "render", "c", "d", "v1")
        store.fail_stage(job, "render", "rendering", "encoder failed", interrupted=False)
        state = store.get_job(job)
    assert state["status"] == JobStatus.FAILED
    assert state["stages"]["inspect"]["status"] == StageStatus.COMPLETED
    assert state["stages"]["render"]["status"] == StageStatus.FAILED
    assert state["stages"]["render"]["error"] == {"phase": "rendering", "message": "encoder failed"}


def test_parameterized_values_preserve_quotes(tmp_path: Path) -> None:
    volume = '{"path":"/tmp/a\\"b"}'
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job('{"title":"O\'Reilly"}', volume)
        store.start_stage(job, "inspect", "input'fingerprint", "settings\"hash", "v1")
        store.complete_stage(job, "inspect", '{"note":"it\'s \\"done\\""}')
        state = store.get_job(job)
    assert state["config"] == {"title": "O'Reilly"}
    assert state["volume"] == {"path": '/tmp/a"b'}
    assert state["stages"]["inspect"]["input_fingerprint"] == "input'fingerprint"
    assert state["stages"]["inspect"]["settings_hash"] == 'settings"hash'
    assert state["stages"]["inspect"]["result"] == {"note": 'it\'s "done"'}


def test_interrupted_stage_has_interrupted_status(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "render", "source", "settings", "v1")
        store.fail_stage(job, "render", "encoding", "stopped", interrupted=True)
        state = store.get_job(job)
    assert state["status"] == JobStatus.INTERRUPTED
    assert state["stages"]["render"]["status"] == StageStatus.INTERRUPTED


def test_completing_stage_preserves_failed_job_status(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "failed", "source", "settings", "v1")
        store.start_stage(job, "other", "source", "settings", "v1")
        store.fail_stage(job, "failed", "encoding", "encoder failed")
        store.complete_stage(job, "other")
        assert store.get_job(job)["status"] == JobStatus.FAILED


def test_completing_stage_preserves_interrupted_job_status(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "interrupted", "source", "settings", "v1")
        store.start_stage(job, "other", "source", "settings", "v1")
        store.fail_stage(job, "interrupted", "encoding", "stopped", interrupted=True)
        store.complete_stage(job, "other")
        assert store.get_job(job)["status"] == JobStatus.INTERRUPTED


def test_sources_and_chronology_are_persisted(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.save_sources(job, [{"source_id": "s1", "path": "clip.mp4", "size": 12}])
        store.save_chronology(job, [{"group_id": "g1", "members": ["s1"], "confidence": "high"}])
        state = store.get_job(job)
    assert state["sources"] == [{"source_id": "s1", "path": "clip.mp4", "size": 12}]
    assert state["chronology"] == [{"group_id": "g1", "members": ["s1"], "confidence": "high"}]


def test_reuse_requires_all_cache_keys_and_valid_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "render.mp4"
    artifact.write_text("output")
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "render", "source-a", "settings-a", "impl-a")
        store.complete_stage(job, "render", '{"artifact":"render"}')
        store.save_artifact(job, "render", artifact, {"valid": True})

        validator = lambda path, metadata: path.exists() and metadata["valid"]
        assert store.find_reusable_stage("render", "source-a", "settings-a", "impl-a", validator) is not None
        assert store.find_reusable_stage("render", "source-a", "settings-b", "impl-a", lambda path, metadata: True) is None
        artifact.unlink()
        assert store.find_reusable_stage("render", "source-a", "settings-a", "impl-a", lambda path, metadata: path.exists()) is None


def test_reuse_rejects_missing_validator_and_non_file_artifact(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "render.mp4"
    artifact_dir.mkdir()
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "render", "source", "settings", "impl")
        store.complete_stage(job, "render")
        store.save_artifact(job, "render", artifact_dir)

        assert store.find_reusable_stage("render", "source", "settings", "impl") is None
        assert store.find_reusable_stage("render", "source", "settings", "impl", lambda path: True) is None


def test_stage_restart_invalidates_prior_artifacts(tmp_path: Path) -> None:
    artifact = tmp_path / "render.mp4"
    artifact.write_text("old output")
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "render", "source", "settings", "impl")
        store.complete_stage(job, "render")
        store.save_artifact(job, "render", artifact, {"attempt": 1})

        store.start_stage(job, "render", "source", "settings", "impl")
        assert store.get_job(job)["artifacts"] == []
        assert store.find_reusable_stage("render", "source", "settings", "impl", lambda path: True) is None


def test_falsy_artifact_metadata_is_preserved(tmp_path: Path) -> None:
    artifact = tmp_path / "render.mp4"
    artifact.write_text("output")
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "render", "source", "settings", "impl")
        store.complete_stage(job, "render")
        store.save_artifact(job, "render", artifact, metadata=[])
        assert store.get_job(job)["artifacts"][0]["metadata"] == []


def test_completing_stage_leaves_job_running_until_explicit_completion(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "inspect", "source", "settings", "impl")
        store.complete_stage(job, "inspect")
        assert store.get_job(job)["status"] == JobStatus.RUNNING

        store.start_stage(job, "render", "source", "settings", "impl")
        assert store.get_job(job)["status"] == JobStatus.RUNNING


def test_complete_job_requires_all_stages_completed(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "inspect", "source", "settings", "impl")
        try:
            store.complete_job(job)
        except ValueError:
            pass
        else:
            raise AssertionError("incomplete job was marked completed")
        assert store.get_job(job)["status"] == JobStatus.RUNNING

        store.complete_stage(job, "inspect")
        store.complete_job(job)
        assert store.get_job(job)["status"] == JobStatus.COMPLETED
