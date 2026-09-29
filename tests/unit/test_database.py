import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from video_editor.analysis.models import (
    AnalysisBoundaryKind,
    AnalysisChunkData,
    AnalysisResultData,
    ProxyManifestData,
    RequestReservation,
)
from video_editor.media.discovery import SourceCandidate
from video_editor.media.sequencing import sequence_sources
from video_editor.persistence.database import JobStatus, JobStore, StageStatus
from video_editor.persistence.migrations import LATEST_MIGRATION_VERSION


def _proxy_manifest_data() -> ProxyManifestData:
    return ProxyManifestData(
        schema_version=1,
        source_fingerprint="source-fingerprint",
        source_identity="source-identity",
        artifact_path="/generated/proxy.mp4",
        generated_root="/generated",
        mapping_version="v1",
        source_start=Decimal(0),
        source_end=Decimal(1),
        proxy_start=Decimal(0),
        proxy_end=Decimal(1),
        video_width=1920,
        video_height=1080,
        video_fps=Decimal(30),
        audio_codec="aac",
        audio_bitrate_bps=128000,
        implementation_version="v1",
    )


def _analysis_chunk_data() -> AnalysisChunkData:
    return AnalysisChunkData(
        schema_version=1,
        mapping_version="v1",
        proxy_start=Decimal(0),
        proxy_end=Decimal(1),
        boundary_kind=AnalysisBoundaryKind.SCENE,
        implementation_version="v1",
    )


def _analysis_result_data() -> AnalysisResultData:
    return AnalysisResultData(
        schema_version=1,
        provider="provider",
        model="model",
        request_id="request-1",
        prompt_version="v1",
        response_schema_version="v1",
        implementation_version="v1",
        token_count=3,
        request_token_count=2,
        output_token_count=1,
        normalized_record_ids=("record-1",),
    )


def test_failed_stage_retains_prior_completed_stage(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "inspect", "a", "b", "v1")
        store.complete_stage(job, "inspect", "{}")
        store.start_stage(job, "render", "c", "d", "v1")
        store.fail_stage(
            job, "render", "rendering", "encoder failed", interrupted=False
        )
        state = store.get_job(job)
    assert state["status"] == JobStatus.FAILED
    assert state["stages"]["inspect"]["status"] == StageStatus.COMPLETED
    assert state["stages"]["render"]["status"] == StageStatus.FAILED
    assert state["stages"]["render"]["error"] == {
        "phase": "rendering",
        "message": "encoder failed",
    }


def test_parameterized_values_preserve_quotes(tmp_path: Path) -> None:
    volume = '{"path":"/tmp/a\\"b"}'
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job('{"title":"O\'Reilly"}', volume)
        store.start_stage(job, "inspect", "input'fingerprint", 'settings"hash', "v1")
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
        store.save_chronology(
            job, [{"group_id": "g1", "members": ["s1"], "confidence": "high"}]
        )
        state = store.get_job(job)
    assert state["sources"] == [{"source_id": "s1", "path": "clip.mp4", "size": 12}]
    assert state["chronology"] == [
        {"group_id": "g1", "members": ["s1"], "confidence": "high"}
    ]


def test_replace_sources_removes_stale_sources_and_probes(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.replace_sources(
            job,
            [
                {"source_id": "old", "path": "old.mp4", "probe": {"duration": 1}},
                {"source_id": "keep", "path": "keep.mp4", "probe": {"duration": 2}},
            ],
        )
        store.replace_sources(
            job,
            [{"source_id": "keep", "path": "keep.mp4", "probe": {"duration": 3}}],
        )
        state = store.get_job(job)
        probes = store.connection.execute(
            "SELECT data_json FROM source_probes ORDER BY id"
        ).fetchall()
    assert [source["source_id"] for source in state["sources"]] == ["keep"]
    assert len(probes) == 1
    assert probes[0]["data_json"] == '{"duration":3}'


def test_replace_chronology_removes_stale_groups_and_members(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.replace_chronology(
            job,
            [{"group_id": "old", "members": ["old-source"], "warnings": ["old"]}],
        )
        store.replace_chronology(
            job,
            [{"group_id": "new", "members": ["new-source"], "warnings": []}],
        )
        state = store.get_job(job)
        members = store.connection.execute(
            "SELECT source_id FROM chronology_members ORDER BY id"
        ).fetchall()
    assert state["chronology"] == [
        {"group_id": "new", "members": ["new-source"], "warnings": []}
    ]
    assert [member["source_id"] for member in members] == ["new-source"]


def test_reuse_requires_all_cache_keys_and_valid_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "render.mp4"
    artifact.write_text("output")
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "render", "source-a", "settings-a", "impl-a")
        store.complete_stage(job, "render", '{"artifact":"render"}')
        store.save_artifact(job, "render", artifact, {"valid": True})

        validator = lambda path, metadata: path.exists() and metadata["valid"]
        assert (
            store.find_reusable_stage(
                "render", "source-a", "settings-a", "impl-a", validator
            )
            is not None
        )
        assert (
            store.find_reusable_stage(
                "render",
                "source-a",
                "settings-b",
                "impl-a",
                lambda path, metadata: True,
            )
            is None
        )
        artifact.unlink()
        assert (
            store.find_reusable_stage(
                "render",
                "source-a",
                "settings-a",
                "impl-a",
                lambda path, metadata: path.exists(),
            )
            is None
        )


def test_reuse_rejects_missing_validator_and_non_file_artifact(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "render.mp4"
    artifact_dir.mkdir()
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "render", "source", "settings", "impl")
        store.complete_stage(job, "render")
        store.save_artifact(job, "render", artifact_dir)

        assert store.find_reusable_stage("render", "source", "settings", "impl") is None
        assert (
            store.find_reusable_stage(
                "render", "source", "settings", "impl", lambda path: True
            )
            is None
        )


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
        assert (
            store.find_reusable_stage(
                "render", "source", "settings", "impl", lambda path: True
            )
            is None
        )


def test_persisted_chronology_keeps_groups_order_and_warnings(tmp_path: Path) -> None:
    sources = [
        SourceCandidate(Path(name), 1, index, f"id-{index}", "bounded-v1")
        for index, name in enumerate(
            [
                "GOPR0200.MP4",
                "GP020200.MP4",
                "GH020200.MP4",
                "GOPR0100.MP4",
                "GP020100.MP4",
                "phone.mp4",
            ]
        )
    ]
    chronology = sequence_sources(
        sources,
        {
            "GOPR0200.MP4": datetime(2026, 1, 2, tzinfo=UTC),
            "GP020200.MP4": datetime(2026, 1, 2, 0, 0, 1, tzinfo=UTC),
            "GH020200.MP4": datetime(2026, 1, 2, 0, 0, 2, tzinfo=UTC),
            "GOPR0100.MP4": datetime(2026, 1, 1, tzinfo=UTC),
            "GP020100.MP4": datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        },
    )
    groups = [
        {
            "group_id": group.group_id,
            "members": [
                {
                    "source_id": member.source.fingerprint,
                    "position": position,
                }
                for position, member in enumerate(group.members)
            ],
            "warnings": list(group.warnings),
        }
        for group in chronology
    ]
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.save_chronology(job, groups)
        saved = store.get_job(job)["chronology"]
    assert [group["group_id"] for group in saved] == ["100", "200", "discovery-5"]
    assert saved[0]["members"][0]["position"] == 0
    assert any("duplicate chapter" in warning for warning in saved[1]["warnings"])
    assert any("uncertain" in warning for warning in saved[-1]["warnings"])


def test_render_retry_can_preserve_per_output_artifacts(tmp_path: Path) -> None:
    first = tmp_path / "long.mp4"
    second = tmp_path / "short.mp4"
    first.write_text("valid")
    second.write_text("valid")
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "render", "source", "settings", "impl")
        store.save_artifact(job, "render", first, {"plan": "long.json"})
        store.save_artifact(job, "render", second, {"plan": "short.json"})
        store.complete_stage(job, "render")
        store.start_stage(
            job, "render", "source", "settings", "impl", preserve_artifacts=True
        )
        artifacts = store.get_job(job)["artifacts"]
    assert [item["path"] for item in artifacts] == [str(first), str(second)]


def test_falsy_artifact_metadata_is_preserved(tmp_path: Path) -> None:
    artifact = tmp_path / "render.mp4"
    artifact.write_text("output")
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "render", "source", "settings", "impl")
        store.complete_stage(job, "render")
        store.save_artifact(job, "render", artifact, metadata=[])
        assert store.get_job(job)["artifacts"][0]["metadata"] == []


def test_completing_stage_leaves_job_running_until_explicit_completion(
    tmp_path: Path,
) -> None:
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


def _create_version_one_fixture(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                config_json TEXT NOT NULL,
                volume_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE job_stages (
                id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL,
                settings_hash TEXT NOT NULL,
                implementation_version TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                error_json TEXT,
                result_json TEXT,
                UNIQUE(job_id, name)
            );
            CREATE TABLE sources (
                id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                source_id TEXT NOT NULL,
                data_json TEXT NOT NULL,
                UNIQUE(job_id, source_id)
            );
            CREATE TABLE chronology_groups (
                id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                group_id TEXT NOT NULL,
                data_json TEXT NOT NULL,
                UNIQUE(job_id, group_id)
            );
            CREATE TABLE artifacts (
                id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                stage_name TEXT NOT NULL,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(job_id, stage_name, name)
            );
            INSERT INTO schema_metadata(key, value)
            VALUES ('migration_version', '1');
            INSERT INTO jobs(
                id, config_json, volume_json, status, created_at, updated_at
            ) VALUES (
                'existing', '{"phase":1}', '{}', 'completed',
                '2026-09-28T00:00:00+00:00', '2026-09-28T00:00:00+00:00'
            );
            """
        )


def test_version_one_database_migrates_without_losing_jobs(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_version_one_fixture(db_path)

    with JobStore(db_path) as store:
        assert store.migration_version() == LATEST_MIGRATION_VERSION
        assert store.get_job("existing")["status"] == "completed"
        assert store.get_job("existing")["config"] == {"phase": 1}


def test_new_database_has_latest_schema_without_rewriting_phase_one_tables(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        assert store.migration_version() == LATEST_MIGRATION_VERSION
        columns = store.connection.execute("PRAGMA table_info(jobs)").fetchall()

    assert [column["name"] for column in columns] == [
        "id",
        "config_json",
        "volume_json",
        "status",
        "created_at",
        "updated_at",
    ]


def test_analysis_manifest_chunk_and_result_round_trip(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.save_proxy_manifest(
            job_id,
            "manifest-1",
            "sha256:abc",
            _proxy_manifest_data(),
        )
        store.save_analysis_chunk(
            job_id,
            "chunk-1",
            "manifest-1",
            source_id="source-1",
            source_start=Decimal("1.25"),
            source_end=Decimal("10.5"),
            data=_analysis_chunk_data(),
        )
        store.save_analysis_result(
            "result-1",
            job_id,
            "cache-1",
            "chunk-1",
            "broad",
            _analysis_result_data(),
            validated=True,
        )

        assert store.find_analysis_result(job_id, "cache-1") == {
            "result_id": "result-1",
            "job_id": job_id,
            "cache_key": "cache-1",
            "chunk_id": "chunk-1",
            "mode": "broad",
            "validated": True,
            "data": _analysis_result_data().model_dump(mode="json"),
        }
        state = store.get_job(job_id)

    assert state["proxy_manifests"][0]["manifest_id"] == "manifest-1"
    assert state["analysis_results"][0]["result_id"] == "result-1"


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "secret"},
        {"client_secret": "secret"},
        {"private_key": "secret"},
        {"x-api-key": "secret"},
        {"unknown": {"nested": "payload"}},
    ],
)
def test_analysis_save_methods_reject_untyped_payloads(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        with pytest.raises(TypeError, match="ProxyManifestData"):
            store.save_proxy_manifest(job_id, "manifest-1", "digest", payload)

        store.save_proxy_manifest(
            job_id, "manifest-1", "digest", _proxy_manifest_data()
        )
        with pytest.raises(TypeError, match="AnalysisChunkData"):
            store.save_analysis_chunk(
                job_id,
                "chunk-1",
                "manifest-1",
                source_id="source-1",
                source_start=Decimal(0),
                source_end=Decimal(1),
                data=payload,
            )

        store.save_analysis_chunk(
            job_id,
            "chunk-1",
            "manifest-1",
            source_id="source-1",
            source_start=Decimal(0),
            source_end=Decimal(1),
            data=_analysis_chunk_data(),
        )
        with pytest.raises(TypeError, match="AnalysisResultData"):
            store.save_analysis_result(
                "result-1",
                job_id,
                "cache-1",
                "chunk-1",
                "broad",
                payload,
                validated=True,
            )


def test_analysis_payload_persistence_contains_only_declared_model_fields(
    tmp_path: Path,
) -> None:
    manifest_data = _proxy_manifest_data()
    chunk_data = _analysis_chunk_data()
    result_data = _analysis_result_data()
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.save_proxy_manifest(job_id, "manifest-1", "digest", manifest_data)
        store.save_analysis_chunk(
            job_id,
            "chunk-1",
            "manifest-1",
            source_id="source-1",
            source_start=Decimal(0),
            source_end=Decimal(1),
            data=chunk_data,
        )
        store.save_analysis_result(
            "result-1",
            job_id,
            "cache-1",
            "chunk-1",
            "broad",
            result_data,
            validated=True,
        )
        stored_payloads = [
            json.loads(row["data_json"])
            for table in ("proxy_manifests", "analysis_chunks", "analysis_results")
            for row in store.connection.execute(f"SELECT data_json FROM {table}")
        ]

    assert stored_payloads == [
        manifest_data.model_dump(mode="json"),
        chunk_data.model_dump(mode="json"),
        result_data.model_dump(mode="json"),
    ]


def test_analysis_records_reject_cross_job_identity_conflicts(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_a = store.create_job("{}", "{}")
        job_b = store.create_job("{}", "{}")
        store.save_proxy_manifest(
            job_a, "manifest-a", "digest-a", _proxy_manifest_data()
        )
        store.save_analysis_chunk(
            job_a,
            "chunk-a",
            "manifest-a",
            source_id="source-a",
            source_start=Decimal(0),
            source_end=Decimal(10),
            data=_analysis_chunk_data(),
        )
        store.save_analysis_result(
            "result-a",
            job_a,
            "cache-a",
            "chunk-a",
            "broad",
            _analysis_result_data(),
            validated=True,
        )

        with pytest.raises(ValueError, match="manifest ID belongs"):
            store.save_proxy_manifest(
                job_b, "manifest-a", "digest-b", _proxy_manifest_data()
            )
        with pytest.raises(ValueError, match="proxy manifest belongs"):
            store.save_analysis_chunk(
                job_b,
                "chunk-b",
                "manifest-a",
                source_id="source-b",
                source_start=Decimal(0),
                source_end=Decimal(10),
                data=_analysis_chunk_data(),
            )
        store.save_proxy_manifest(
            job_b, "manifest-b", "digest-b", _proxy_manifest_data()
        )
        with pytest.raises(ValueError, match="chunk ID is already"):
            store.save_analysis_chunk(
                job_b,
                "chunk-a",
                "manifest-b",
                source_id="source-a",
                source_start=Decimal(0),
                source_end=Decimal(10),
                data=_analysis_chunk_data(),
            )
        with pytest.raises(ValueError, match="analysis chunk belongs"):
            store.save_analysis_result(
                "result-b",
                job_b,
                "cache-b",
                "chunk-a",
                "broad",
                _analysis_result_data(),
                validated=True,
            )
        store.save_analysis_chunk(
            job_b,
            "chunk-b",
            "manifest-b",
            source_id="source-b",
            source_start=Decimal(0),
            source_end=Decimal(10),
            data=_analysis_chunk_data(),
        )
        with pytest.raises(ValueError, match="result ID or cache key"):
            store.save_analysis_result(
                "result-a",
                job_b,
                "cache-b",
                "chunk-b",
                "broad",
                _analysis_result_data(),
                validated=True,
            )
        with pytest.raises(ValueError, match="result ID or cache key"):
            store.save_analysis_result(
                "result-b",
                job_a,
                "cache-a",
                "chunk-a",
                "broad",
                _analysis_result_data(),
                validated=True,
            )

        manifest = store.connection.execute(
            "SELECT job_id, digest, data_json FROM proxy_manifests WHERE manifest_id = 'manifest-a'"
        ).fetchone()
        chunk = store.connection.execute(
            "SELECT job_id, manifest_id, source_id, source_start, source_end, data_json "
            "FROM analysis_chunks WHERE chunk_id = 'chunk-a'"
        ).fetchone()
        result = store.connection.execute(
            "SELECT result_id, job_id, cache_key, chunk_id, data_json "
            "FROM analysis_results WHERE result_id = 'result-a'"
        ).fetchone()

    assert tuple(manifest) == (
        job_a,
        "digest-a",
        json.dumps(
            _proxy_manifest_data().model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    assert tuple(chunk) == (
        job_a,
        "manifest-a",
        "source-a",
        "0",
        "10",
        json.dumps(
            _analysis_chunk_data().model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    assert tuple(result) == (
        "result-a",
        job_a,
        "cache-a",
        "chunk-a",
        json.dumps(
            _analysis_result_data().model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


@pytest.mark.parametrize(
    ("source_start", "source_end"),
    [
        (Decimal("NaN"), Decimal(1)),
        (Decimal("Infinity"), Decimal(1)),
        (Decimal("-Infinity"), Decimal(1)),
        (Decimal(1), Decimal(1)),
        (Decimal(2), Decimal(1)),
    ],
)
def test_analysis_chunk_rejects_non_finite_or_reversed_ranges(
    tmp_path: Path, source_start: Decimal, source_end: Decimal
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.save_proxy_manifest(
            job_id, "manifest-1", "digest", _proxy_manifest_data()
        )
        with pytest.raises(ValidationError):
            store.save_analysis_chunk(
                job_id,
                "chunk-1",
                "manifest-1",
                source_id="source-1",
                source_start=source_start,
                source_end=source_end,
                data=_analysis_chunk_data(),
            )

        assert (
            store.connection.execute("SELECT * FROM analysis_chunks").fetchall() == []
        )


def test_failed_migration_does_not_advance_version(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_version_one_fixture(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE proxy_manifests (id INTEGER PRIMARY KEY, marker TEXT)"
        )

    try:
        with JobStore(db_path):
            pass
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("conflicting migration unexpectedly succeeded")

    with sqlite3.connect(db_path) as connection:
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'migration_version'"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == ("1",)
    assert "analysis_chunks" not in tables


def test_analysis_records_never_add_api_key_columns(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        tables = [
            "proxy_manifests",
            "analysis_chunks",
            "analysis_requests",
            "analysis_results",
            "budget_accounts",
        ]
        columns = {
            table: [
                row["name"]
                for row in store.connection.execute(f"PRAGMA table_info({table})")
            ]
            for table in tables
        }

    assert "api_key" not in json.dumps(columns).lower()


def test_budget_uses_integer_microusd_storage(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("1.0000009"))
        request_id = store.reserve_request(
            RequestReservation(
                request_id="request-1",
                job_id=job_id,
                cache_key="cache-1",
                mode="broad",
                maximum_cost_usd=Decimal("0.1000001"),
            )
        )
        assert request_id == "request-1"
        account = store.connection.execute(
            "SELECT limit_microusd, reserved_microusd FROM budget_accounts WHERE job_id = ?",
            (job_id,),
        ).fetchone()

        assert account is not None
        assert tuple(account) == (1_000_000, 100_001)
        assert store.budget_state(job_id).reserved_usd == Decimal("0.100001")
