"""SQLite-backed resumable job state."""

from __future__ import annotations

import inspect
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _json(value: Any) -> str:
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"))


def _row_json(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


class JobStore:
    """Context manager and repository for persistent job state."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._connection: sqlite3.Connection | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("JobStore is not open")
        return self._connection

    def __enter__(self) -> JobStore:  # noqa: PYI034
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.db_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:  # noqa: PYI036
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @contextmanager
    def _transaction(self) -> Any:
        connection = self.connection
        connection.execute("BEGIN")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _create_schema(self) -> None:
        ddl = (
            """CREATE TABLE IF NOT EXISTS schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                config_json TEXT NOT NULL,
                volume_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS job_stages (
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
            )""",
            """CREATE INDEX IF NOT EXISTS job_stages_cache_idx
                ON job_stages(name, input_fingerprint, settings_hash, implementation_version, status)""",
            """CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                source_id TEXT NOT NULL,
                data_json TEXT NOT NULL,
                UNIQUE(job_id, source_id)
            )""",
            """CREATE TABLE IF NOT EXISTS source_probes (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                data_json TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS chronology_groups (
                id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                group_id TEXT NOT NULL,
                data_json TEXT NOT NULL,
                UNIQUE(job_id, group_id)
            )""",
            """CREATE TABLE IF NOT EXISTS chronology_members (
                id INTEGER PRIMARY KEY,
                group_id INTEGER NOT NULL REFERENCES chronology_groups(id) ON DELETE CASCADE,
                source_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                data_json TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS proxy_mappings (
                id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                data_json TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                stage_name TEXT NOT NULL,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(job_id, stage_name, name)
            )""",
            """CREATE TABLE IF NOT EXISTS cache_entries (
                id INTEGER PRIMARY KEY,
                stage_id INTEGER NOT NULL REFERENCES job_stages(id) ON DELETE CASCADE,
                artifact_id INTEGER REFERENCES artifacts(id) ON DELETE SET NULL
            )""",
            """CREATE TABLE IF NOT EXISTS render_attempts (
                id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                stage_name TEXT NOT NULL,
                data_json TEXT NOT NULL
            )""",
        )
        with self._transaction() as connection:
            for statement in ddl:
                connection.execute(statement)
            connection.execute(
                "INSERT OR IGNORE INTO schema_metadata(key, value) VALUES (?, ?)",
                ("migration_version", "1"),
            )

    def create_job(self, config_json: Any, volume_json: Any) -> str:
        job_id = str(uuid.uuid4())
        now = _now()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO jobs(id, config_json, volume_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, _json(config_json), _json(volume_json), JobStatus.PENDING, now, now),
            )
        return job_id

    def _job_exists(self, connection: sqlite3.Connection, job_id: str) -> None:
        if connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
            raise KeyError(f"unknown job: {job_id}")

    def start_stage(
        self,
        job_id: str,
        name: str,
        input_fingerprint: str,
        settings_hash: str,
        implementation_version: str,
    ) -> None:
        now = _now()
        with self._transaction() as connection:
            self._job_exists(connection, job_id)
            # A restarted stage must not expose artifacts produced by its prior attempt.
            connection.execute(
                "DELETE FROM artifacts WHERE job_id = ? AND stage_name = ?",
                (job_id, name),
            )
            connection.execute(
                """INSERT INTO job_stages(
                    job_id, name, input_fingerprint, settings_hash,
                    implementation_version, status, started_at, completed_at,
                    error_json, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                ON CONFLICT(job_id, name) DO UPDATE SET
                    input_fingerprint=excluded.input_fingerprint,
                    settings_hash=excluded.settings_hash,
                    implementation_version=excluded.implementation_version,
                    status=excluded.status,
                    started_at=excluded.started_at,
                    completed_at=NULL,
                    error_json=NULL,
                    result_json=NULL""",
                (job_id, name, input_fingerprint, settings_hash, implementation_version, StageStatus.RUNNING, now),
            )
            connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (JobStatus.RUNNING, now, job_id),
            )

    def complete_stage(self, job_id: str, name: str, result_json: Any = "{}") -> None:
        now = _now()
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE job_stages SET status = ?, completed_at = ?, result_json = ?, error_json = NULL "
                "WHERE job_id = ? AND name = ?",
                (StageStatus.COMPLETED, now, _json(result_json), job_id, name),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown stage: {job_id}/{name}")
            remaining = connection.execute(
                "SELECT COUNT(*) FROM job_stages WHERE job_id = ? AND status != ?",
                (job_id, StageStatus.COMPLETED),
            ).fetchone()[0]
            job_status = JobStatus.COMPLETED if remaining == 0 else JobStatus.RUNNING
            connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (job_status, now, job_id),
            )

    def complete_job(self, job_id: str) -> None:
        """Mark job completed in one transaction after successful execution."""
        now = _now()
        with self._transaction() as connection:
            self._job_exists(connection, job_id)
            incomplete = connection.execute(
                "SELECT 1 FROM job_stages WHERE job_id = ? AND status != ? LIMIT 1",
                (job_id, StageStatus.COMPLETED),
            ).fetchone()
            if incomplete is not None:
                raise ValueError(f"job has incomplete stages: {job_id}")
            connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (JobStatus.COMPLETED, now, job_id),
            )

    def fail_stage(
        self,
        job_id: str,
        name: str,
        phase: str,
        message: str,
        interrupted: bool = False,
    ) -> None:
        now = _now()
        status = StageStatus.INTERRUPTED if interrupted else StageStatus.FAILED
        job_status = JobStatus.INTERRUPTED if interrupted else JobStatus.FAILED
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE job_stages SET status = ?, completed_at = ?, error_json = ? "
                "WHERE job_id = ? AND name = ?",
                (status, now, _json({"phase": phase, "message": message}), job_id, name),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown stage: {job_id}/{name}")
            connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (job_status, now, job_id),
            )

    def save_sources(self, job_id: str, sources: Iterable[Mapping[str, Any]]) -> None:
        with self._transaction() as connection:
            self._job_exists(connection, job_id)
            for source in sources:
                data = dict(source)
                source_id = str(data.get("source_id", data.get("id", "")))
                if not source_id:
                    raise ValueError("source requires source_id")
                connection.execute(
                    "INSERT INTO sources(job_id, source_id, data_json) VALUES (?, ?, ?) "
                    "ON CONFLICT(job_id, source_id) DO UPDATE SET data_json=excluded.data_json",
                    (job_id, source_id, _json(data)),
                )

    def save_chronology(self, job_id: str, groups: Iterable[Mapping[str, Any]]) -> None:
        with self._transaction() as connection:
            self._job_exists(connection, job_id)
            for group in groups:
                data = dict(group)
                group_id = str(data.get("group_id", data.get("id", "")))
                if not group_id:
                    raise ValueError("chronology group requires group_id")
                connection.execute(
                    "INSERT INTO chronology_groups(job_id, group_id, data_json) VALUES (?, ?, ?) "
                    "ON CONFLICT(job_id, group_id) DO UPDATE SET data_json=excluded.data_json",
                    (job_id, group_id, _json(data)),
                )

    def save_artifact(
        self,
        job_id: str,
        stage_name: str,
        path: Path,
        metadata: Any = None,
        name: str | None = None,
    ) -> int:
        artifact_name = name or path.name
        now = _now()
        with self._transaction() as connection:
            self._job_exists(connection, job_id)
            connection.execute(
                """INSERT INTO artifacts(job_id, stage_name, name, path, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, stage_name, name) DO UPDATE SET
                    path=excluded.path, metadata_json=excluded.metadata_json, created_at=excluded.created_at""",
                (job_id, stage_name, artifact_name, str(path), _json(metadata if metadata is not None else {}), now),
            )
            row = connection.execute(
                "SELECT id FROM artifacts WHERE job_id = ? AND stage_name = ? AND name = ?",
                (job_id, stage_name, artifact_name),
            ).fetchone()
            assert row is not None
            return int(row["id"])

    def find_reusable_stage(
        self,
        name: str,
        input_fingerprint: str,
        settings_hash: str,
        implementation_version: str,
        artifact_validator: Callable[..., bool] | None = None,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT s.*, j.id AS owning_job_id FROM job_stages AS s
            JOIN jobs AS j ON j.id = s.job_id
            WHERE s.name = ? AND s.input_fingerprint = ? AND s.settings_hash = ?
              AND s.implementation_version = ? AND s.status = ?
            ORDER BY s.completed_at DESC LIMIT 1""",
            (name, input_fingerprint, settings_hash, implementation_version, StageStatus.COMPLETED),
        ).fetchone()
        if row is None:
            return None
        artifacts = self.connection.execute(
            "SELECT * FROM artifacts WHERE job_id = ? AND stage_name = ? ORDER BY id",
            (row["job_id"], name),
        ).fetchall()
        if not artifacts:
            return None
        checked: list[dict[str, Any]] = []
        for artifact in artifacts:
            path = Path(artifact["path"])
            metadata = _row_json(artifact["metadata_json"])
            if not path.is_file() or artifact_validator is None:
                return None
            parameters = inspect.signature(artifact_validator).parameters
            valid = bool(artifact_validator(path, metadata) if len(parameters) >= 2 else artifact_validator(path))
            if not valid:
                return None
            checked.append({"name": artifact["name"], "path": path, "metadata": metadata})
        return {
            "job_id": row["job_id"],
            "stage_id": row["id"],
            "name": row["name"],
            "status": row["status"],
            "input_fingerprint": row["input_fingerprint"],
            "settings_hash": row["settings_hash"],
            "implementation_version": row["implementation_version"],
            "result": _row_json(row["result_json"]),
            "artifacts": checked,
        }

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self.connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise KeyError(f"unknown job: {job_id}")
        stages = self.connection.execute(
            "SELECT * FROM job_stages WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        sources = self.connection.execute(
            "SELECT data_json FROM sources WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        chronology = self.connection.execute(
            "SELECT data_json FROM chronology_groups WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        artifacts = self.connection.execute(
            "SELECT * FROM artifacts WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        return {
            "job_id": job["id"],
            "status": job["status"],
            "config": _row_json(job["config_json"]),
            "volume": _row_json(job["volume_json"]),
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "stages": {
                row["name"]: {
                    "status": row["status"],
                    "input_fingerprint": row["input_fingerprint"],
                    "settings_hash": row["settings_hash"],
                    "implementation_version": row["implementation_version"],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                    "error": _row_json(row["error_json"]),
                    "result": _row_json(row["result_json"]),
                }
                for row in stages
            },
            "sources": [_row_json(row["data_json"]) for row in sources],
            "chronology": [_row_json(row["data_json"]) for row in chronology],
            "artifacts": [
                {
                    "stage": row["stage_name"],
                    "name": row["name"],
                    "path": row["path"],
                    "metadata": _row_json(row["metadata_json"]),
                }
                for row in artifacts
            ],
        }
