"""SQLite-backed resumable job state."""

from __future__ import annotations

import inspect
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

from video_editor.analysis.models import BudgetState, RequestReservation
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.persistence.migrations import run_migrations


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


_MICRO_USD = Decimal(1000000)
_SQLITE_MAX_INTEGER = 2**63 - 1


def _usd_to_microusd(amount: Decimal, rounding: str) -> int:
    if not amount.is_finite() or amount < 0:
        raise ValueError("USD amount must be finite and non-negative")
    microusd = int((amount * _MICRO_USD).to_integral_value(rounding=rounding))
    if microusd > _SQLITE_MAX_INTEGER:
        raise OverflowError("USD amount exceeds SQLite integer range")
    return microusd


def _microusd_to_usd(amount: int) -> Decimal:
    return Decimal(amount) / _MICRO_USD


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
        self._connection = sqlite3.connect(self.db_path, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
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
        run_migrations(self.connection)

    def migration_version(self) -> int:
        """Return current ordered schema migration version."""
        row = self.connection.execute(
            "SELECT value FROM schema_metadata WHERE key = ?",
            ("migration_version",),
        ).fetchone()
        if row is None:
            raise RuntimeError("database is missing migration version metadata")
        return int(row["value"])

    def create_job(self, config_json: Any, volume_json: Any) -> str:
        job_id = str(uuid.uuid4())
        now = _now()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO jobs(id, config_json, volume_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    _json(config_json),
                    _json(volume_json),
                    JobStatus.PENDING,
                    now,
                    now,
                ),
            )
        return job_id

    def _job_exists(self, connection: sqlite3.Connection, job_id: str) -> None:
        if (
            connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
            is None
        ):
            raise KeyError(f"unknown job: {job_id}")

    def start_stage(
        self,
        job_id: str,
        name: str,
        input_fingerprint: str,
        settings_hash: str,
        implementation_version: str,
        *,
        preserve_artifacts: bool = False,
    ) -> None:
        now = _now()
        with self._transaction() as connection:
            self._job_exists(connection, job_id)
            # Render retries preserve valid per-output artifacts so resume can skip them.
            if not preserve_artifacts:
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
                (
                    job_id,
                    name,
                    input_fingerprint,
                    settings_hash,
                    implementation_version,
                    StageStatus.RUNNING,
                    now,
                ),
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
            interrupted = connection.execute(
                "SELECT 1 FROM job_stages WHERE job_id = ? AND status = ? LIMIT 1",
                (job_id, StageStatus.INTERRUPTED),
            ).fetchone()
            failed = connection.execute(
                "SELECT 1 FROM job_stages WHERE job_id = ? AND status = ? LIMIT 1",
                (job_id, StageStatus.FAILED),
            ).fetchone()
            if interrupted is not None:
                job_status = JobStatus.INTERRUPTED
            elif failed is not None:
                job_status = JobStatus.FAILED
            else:
                job_status = JobStatus.RUNNING
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
                (
                    status,
                    now,
                    _json({"phase": phase, "message": message}),
                    job_id,
                    name,
                ),
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
                self._insert_source(connection, job_id, source)

    def _insert_source(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        source: Mapping[str, Any],
    ) -> None:
        data = dict(source)
        source_id = str(data.get("source_id", data.get("id", "")))
        if not source_id:
            raise ValueError("source requires source_id")
        connection.execute(
            "INSERT INTO sources(job_id, source_id, data_json) VALUES (?, ?, ?) "
            "ON CONFLICT(job_id, source_id) DO UPDATE SET data_json=excluded.data_json",
            (job_id, source_id, _json(data)),
        )
        probe = data.get("probe")
        if probe is not None:
            source_row = connection.execute(
                "SELECT id FROM sources WHERE job_id = ? AND source_id = ?",
                (job_id, source_id),
            ).fetchone()
            assert source_row is not None
            connection.execute(
                "DELETE FROM source_probes WHERE source_id = ?", (source_row["id"],)
            )
            connection.execute(
                "INSERT INTO source_probes(source_id, data_json) VALUES (?, ?)",
                (source_row["id"], _json(probe)),
            )

    def replace_sources(
        self, job_id: str, sources: Iterable[Mapping[str, Any]]
    ) -> None:
        """Atomically replace inspection sources and their persisted probes."""
        with self._transaction() as connection:
            self._job_exists(connection, job_id)
            connection.execute("DELETE FROM sources WHERE job_id = ?", (job_id,))
            for source in sources:
                self._insert_source(connection, job_id, source)

    def save_chronology(self, job_id: str, groups: Iterable[Mapping[str, Any]]) -> None:
        with self._transaction() as connection:
            self._job_exists(connection, job_id)
            for group in groups:
                self._insert_chronology_group(connection, job_id, group)

    def replace_chronology(
        self, job_id: str, groups: Iterable[Mapping[str, Any]]
    ) -> None:
        """Atomically replace chronology groups and their ordered members."""
        with self._transaction() as connection:
            self._job_exists(connection, job_id)
            connection.execute(
                "DELETE FROM chronology_groups WHERE job_id = ?", (job_id,)
            )
            for group in groups:
                self._insert_chronology_group(connection, job_id, group)

    def _insert_chronology_group(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        group: Mapping[str, Any],
    ) -> None:
        data = dict(group)
        group_id = str(data.get("group_id", data.get("id", "")))
        if not group_id:
            raise ValueError("chronology group requires group_id")
        connection.execute(
            "INSERT INTO chronology_groups(job_id, group_id, data_json) VALUES (?, ?, ?) "
            "ON CONFLICT(job_id, group_id) DO UPDATE SET data_json=excluded.data_json",
            (job_id, group_id, _json(data)),
        )
        row = connection.execute(
            "SELECT id FROM chronology_groups WHERE job_id = ? AND group_id = ?",
            (job_id, group_id),
        ).fetchone()
        assert row is not None
        connection.execute(
            "DELETE FROM chronology_members WHERE group_id = ?", (row["id"],)
        )
        members = data.get("members", [])
        if not isinstance(members, list):
            raise TypeError("chronology group members must be a list")
        for position, member in enumerate(members):
            member_data = (
                dict(member)
                if isinstance(member, Mapping)
                else {"source_id": str(member)}
            )
            source_id = str(member_data.get("source_id", member_data.get("id", "")))
            if not source_id:
                raise ValueError("chronology member requires source_id")
            connection.execute(
                "INSERT INTO chronology_members(group_id, source_id, position, data_json) VALUES (?, ?, ?, ?)",
                (row["id"], source_id, position, _json(member_data)),
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
                (
                    job_id,
                    stage_name,
                    artifact_name,
                    str(path),
                    _json(metadata if metadata is not None else {}),
                    now,
                ),
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
            (
                name,
                input_fingerprint,
                settings_hash,
                implementation_version,
                StageStatus.COMPLETED,
            ),
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
            valid = bool(
                artifact_validator(path, metadata)
                if len(parameters) >= 2
                else artifact_validator(path)
            )
            if not valid:
                return None
            checked.append(
                {"name": artifact["name"], "path": path, "metadata": metadata}
            )
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

    def save_proxy_manifest(
        self,
        job_id: str,
        manifest_id: str,
        digest: str,
        data: Any,
    ) -> None:
        """Persist one generated proxy manifest for later upload validation."""
        with self._transaction() as connection:
            self._job_exists(connection, job_id)
            connection.execute(
                """INSERT INTO proxy_manifests(
                    job_id, manifest_id, digest, data_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(manifest_id) DO UPDATE SET
                    digest=excluded.digest,
                    data_json=excluded.data_json,
                    created_at=excluded.created_at""",
                (job_id, manifest_id, digest, _json(data), _now()),
            )

    def save_analysis_chunk(
        self,
        job_id: str,
        chunk_id: str,
        manifest_id: str,
        *,
        source_id: str,
        source_start: Decimal,
        source_end: Decimal,
        data: Any,
    ) -> None:
        """Persist one source-mapped analysis chunk."""
        if source_start < 0 or source_end <= source_start:
            raise ValueError("analysis chunk requires an increasing source range")
        with self._transaction() as connection:
            self._job_exists(connection, job_id)
            connection.execute(
                """INSERT INTO analysis_chunks(
                    job_id, chunk_id, manifest_id, source_id,
                    source_start, source_end, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    source_id=excluded.source_id,
                    source_start=excluded.source_start,
                    source_end=excluded.source_end,
                    data_json=excluded.data_json""",
                (
                    job_id,
                    chunk_id,
                    manifest_id,
                    source_id,
                    format(source_start, "f"),
                    format(source_end, "f"),
                    _json(data),
                ),
            )

    def save_analysis_result(
        self,
        result_id: str,
        job_id: str,
        cache_key: str,
        chunk_id: str,
        mode: Literal["broad", "candidate"],
        data: Any,
        *,
        validated: bool,
    ) -> None:
        """Persist one provider-neutral normalized analysis result."""
        with self._transaction() as connection:
            self._job_exists(connection, job_id)
            connection.execute(
                """INSERT INTO analysis_results(
                    result_id, job_id, cache_key, chunk_id, mode,
                    validated, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, cache_key) DO UPDATE SET
                    result_id=excluded.result_id,
                    chunk_id=excluded.chunk_id,
                    mode=excluded.mode,
                    validated=excluded.validated,
                    data_json=excluded.data_json""",
                (
                    result_id,
                    job_id,
                    cache_key,
                    chunk_id,
                    mode,
                    int(validated),
                    _json(data),
                ),
            )

    def find_analysis_result(
        self, job_id: str, cache_key: str
    ) -> dict[str, Any] | None:
        """Return one cached analysis result regardless of validation state."""
        row = self.connection.execute(
            "SELECT * FROM analysis_results WHERE job_id = ? AND cache_key = ?",
            (job_id, cache_key),
        ).fetchone()
        if row is None:
            return None
        return {
            "result_id": row["result_id"],
            "job_id": row["job_id"],
            "cache_key": row["cache_key"],
            "chunk_id": row["chunk_id"],
            "mode": row["mode"],
            "validated": bool(row["validated"]),
            "data": _row_json(row["data_json"]),
        }

    def initialize_budget(self, job_id: str, limit_usd: Decimal) -> None:
        """Create an exact job budget without mutating an existing account."""
        limit_microusd = _usd_to_microusd(limit_usd, ROUND_FLOOR)
        with self._transaction() as connection:
            self._job_exists(connection, job_id)
            row = connection.execute(
                "SELECT limit_microusd FROM budget_accounts WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is not None:
                if int(row["limit_microusd"]) != limit_microusd:
                    raise ValueError("job budget is already initialized")
                return
            connection.execute(
                "INSERT INTO budget_accounts(job_id, limit_microusd) VALUES (?, ?)",
                (job_id, limit_microusd),
            )

    def _reserve_request(
        self,
        connection: sqlite3.Connection,
        reservation: RequestReservation,
    ) -> str | None:
        cached = connection.execute(
            """SELECT 1 FROM analysis_results
            WHERE job_id = ? AND cache_key = ? AND validated = 1""",
            (reservation.job_id, reservation.cache_key),
        ).fetchone()
        if cached is not None:
            return None
        existing = connection.execute(
            """SELECT request_id, status FROM analysis_requests
            WHERE job_id = ? AND cache_key = ?""",
            (reservation.job_id, reservation.cache_key),
        ).fetchone()
        if existing is not None:
            if existing["request_id"] != reservation.request_id:
                raise ValueError("cache key is already assigned to another request")
            if existing["status"] in {
                "reserved",
                "dispatched",
                "billing_unknown",
                "completed",
            }:
                return str(existing["request_id"])
        account = connection.execute(
            """SELECT limit_microusd, spent_microusd, reserved_microusd
            FROM budget_accounts WHERE job_id = ?""",
            (reservation.job_id,),
        ).fetchone()
        if account is None:
            raise KeyError(f"budget is not initialized: {reservation.job_id}")
        maximum = _usd_to_microusd(reservation.maximum_cost_usd, ROUND_CEILING)
        if (
            account["spent_microusd"] + account["reserved_microusd"] + maximum
            > account["limit_microusd"]
        ):
            raise VideoEditorError(
                ErrorCategory.BUDGET,
                "request exceeds job budget",
                code="budget_exhausted",
            )
        if existing is not None:
            connection.execute(
                """UPDATE analysis_requests SET
                    status = 'reserved', maximum_cost_microusd = ?,
                    actual_cost_microusd = NULL, data_json = ?
                WHERE request_id = ?""",
                (maximum, reservation.model_dump_json(), reservation.request_id),
            )
        else:
            connection.execute(
                """INSERT INTO analysis_requests(
                    request_id, job_id, cache_key, mode, status,
                    maximum_cost_microusd, actual_cost_microusd, data_json
                ) VALUES (?, ?, ?, ?, 'reserved', ?, NULL, ?)""",
                (
                    reservation.request_id,
                    reservation.job_id,
                    reservation.cache_key,
                    reservation.mode,
                    maximum,
                    reservation.model_dump_json(),
                ),
            )
        connection.execute(
            """UPDATE budget_accounts
            SET reserved_microusd = reserved_microusd + ? WHERE job_id = ?""",
            (maximum, reservation.job_id),
        )
        return reservation.request_id

    def reserve_request(self, reservation: RequestReservation) -> str | None:
        """Reserve a request maximum under an immediate SQLite write lock."""
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            request_id = self._reserve_request(connection, reservation)
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
            return request_id

    def reserve_request_batch(
        self,
        job_id: str,
        requests: Sequence[RequestReservation],
    ) -> tuple[str, ...]:
        """Atomically reserve all uncached broad requests for one job."""
        if any(request.job_id != job_id for request in requests):
            raise ValueError("all reservations must belong to the requested job")
        if any(request.mode != "broad" for request in requests):
            raise ValueError("batch preflight accepts only broad requests")
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            request_ids = tuple(
                request_id
                for request in requests
                if (request_id := self._reserve_request(connection, request))
                is not None
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
            return request_ids

    def _request_for_update(
        self, connection: sqlite3.Connection, request_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM analysis_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown analysis request: {request_id}")
        return cast(sqlite3.Row, row)

    def mark_request_dispatched(self, request_id: str) -> None:
        """Record that provider dispatch began while preserving reservation."""
        with self._transaction() as connection:
            row = self._request_for_update(connection, request_id)
            if row["status"] != "reserved":
                raise ValueError("only a reserved request can be dispatched")
            connection.execute(
                "UPDATE analysis_requests SET status = 'dispatched' WHERE request_id = ?",
                (request_id,),
            )

    def mark_request_billing_unknown(self, request_id: str) -> None:
        """Retain reservation when provider billing outcome is unknown."""
        with self._transaction() as connection:
            row = self._request_for_update(connection, request_id)
            if row["status"] not in {"reserved", "dispatched"}:
                raise ValueError("request billing outcome cannot become unknown")
            connection.execute(
                """UPDATE analysis_requests SET status = 'billing_unknown'
                WHERE request_id = ?""",
                (request_id,),
            )

    def settle_request(self, request_id: str, actual_cost_usd: Decimal) -> None:
        """Replace full reservation with provider-confirmed actual cost."""
        actual = _usd_to_microusd(actual_cost_usd, ROUND_CEILING)
        with self._transaction() as connection:
            row = self._request_for_update(connection, request_id)
            if row["status"] not in {"reserved", "dispatched", "billing_unknown"}:
                raise ValueError("request cannot be settled from its current state")
            maximum = int(row["maximum_cost_microusd"])
            if actual > maximum:
                raise VideoEditorError(
                    ErrorCategory.BUDGET,
                    "actual request cost exceeds reservation",
                    code="actual_cost_exceeds_reservation",
                )
            connection.execute(
                """UPDATE budget_accounts
                SET reserved_microusd = reserved_microusd - ?,
                    spent_microusd = spent_microusd + ?
                WHERE job_id = ?""",
                (maximum, actual, row["job_id"]),
            )
            connection.execute(
                """UPDATE analysis_requests
                SET status = 'completed', actual_cost_microusd = ?
                WHERE request_id = ?""",
                (actual, request_id),
            )

    def release_confirmed_nonbillable(self, request_id: str) -> None:
        """Release reservation after confirmed non-billable provider failure."""
        with self._transaction() as connection:
            row = self._request_for_update(connection, request_id)
            if row["status"] not in {"reserved", "dispatched", "billing_unknown"}:
                raise ValueError("request cannot be released from its current state")
            connection.execute(
                """UPDATE budget_accounts
                SET reserved_microusd = reserved_microusd - ? WHERE job_id = ?""",
                (row["maximum_cost_microusd"], row["job_id"]),
            )
            connection.execute(
                """UPDATE analysis_requests
                SET status = 'released', actual_cost_microusd = 0
                WHERE request_id = ?""",
                (request_id,),
            )

    def reconcile_unknown_request(
        self,
        request_id: str,
        *,
        provider_confirmed_actual_cost_usd: Decimal | None = None,
        confirmed_nonbillable: bool = False,
    ) -> None:
        """Resolve unknown billing from exactly one provider-confirmed outcome."""
        if (provider_confirmed_actual_cost_usd is None) == (not confirmed_nonbillable):
            raise ValueError("exactly one confirmed billing outcome is required")
        row = self.connection.execute(
            "SELECT status FROM analysis_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown analysis request: {request_id}")
        if row["status"] != "billing_unknown":
            raise ValueError("only billing_unknown requests can be reconciled")
        if provider_confirmed_actual_cost_usd is not None:
            self.settle_request(request_id, provider_confirmed_actual_cost_usd)
        else:
            self.release_confirmed_nonbillable(request_id)

    def budget_state(self, job_id: str) -> BudgetState:
        """Return exact budget totals for one job."""
        row = self.connection.execute(
            "SELECT * FROM budget_accounts WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"budget is not initialized: {job_id}")
        limit = int(row["limit_microusd"])
        spent = int(row["spent_microusd"])
        reserved = int(row["reserved_microusd"])
        return BudgetState(
            limit_usd=_microusd_to_usd(limit),
            spent_usd=_microusd_to_usd(spent),
            reserved_usd=_microusd_to_usd(reserved),
            remaining_usd=_microusd_to_usd(limit - spent - reserved),
        )

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self.connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job is None:
            raise KeyError(f"unknown job: {job_id}")
        stages = self.connection.execute(
            "SELECT * FROM job_stages WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        sources = self.connection.execute(
            "SELECT data_json FROM sources WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        chronology = self.connection.execute(
            "SELECT data_json FROM chronology_groups WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
        artifacts = self.connection.execute(
            "SELECT * FROM artifacts WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        proxy_manifests = self.connection.execute(
            "SELECT * FROM proxy_manifests WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        analysis_results = self.connection.execute(
            "SELECT * FROM analysis_results WHERE job_id = ? ORDER BY rowid",
            (job_id,),
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
            "proxy_manifests": [
                {
                    "manifest_id": row["manifest_id"],
                    "digest": row["digest"],
                    "data": _row_json(row["data_json"]),
                    "created_at": row["created_at"],
                }
                for row in proxy_manifests
            ],
            "analysis_results": [
                {
                    "result_id": row["result_id"],
                    "cache_key": row["cache_key"],
                    "chunk_id": row["chunk_id"],
                    "mode": row["mode"],
                    "validated": bool(row["validated"]),
                    "data": _row_json(row["data_json"]),
                }
                for row in analysis_results
            ],
        }
