"""Ordered SQLite schema migrations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    """One immutable, ordered database migration."""

    version: int
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=2,
        statements=(
            """CREATE TABLE proxy_manifests (
                id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                manifest_id TEXT NOT NULL UNIQUE,
                digest TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE analysis_chunks (
                id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                chunk_id TEXT NOT NULL UNIQUE,
                manifest_id TEXT NOT NULL REFERENCES proxy_manifests(manifest_id),
                source_id TEXT NOT NULL,
                source_start TEXT NOT NULL,
                source_end TEXT NOT NULL,
                data_json TEXT NOT NULL
            )""",
            """CREATE TABLE analysis_requests (
                request_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                cache_key TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('broad', 'candidate')),
                status TEXT NOT NULL CHECK(status IN (
                    'reserved', 'dispatched', 'completed', 'released',
                    'billing_unknown'
                )),
                maximum_cost_microusd INTEGER NOT NULL
                    CHECK(maximum_cost_microusd >= 0),
                actual_cost_microusd INTEGER CHECK(actual_cost_microusd >= 0),
                data_json TEXT NOT NULL,
                UNIQUE(job_id, cache_key)
            )""",
            """CREATE TABLE analysis_results (
                result_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                cache_key TEXT NOT NULL,
                chunk_id TEXT NOT NULL REFERENCES analysis_chunks(chunk_id),
                mode TEXT NOT NULL CHECK(mode IN ('broad', 'candidate')),
                validated INTEGER NOT NULL CHECK(validated IN (0, 1)),
                data_json TEXT NOT NULL,
                UNIQUE(job_id, cache_key)
            )""",
            """CREATE TABLE budget_accounts (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                limit_microusd INTEGER NOT NULL CHECK(limit_microusd >= 0),
                spent_microusd INTEGER NOT NULL DEFAULT 0
                    CHECK(spent_microusd >= 0),
                reserved_microusd INTEGER NOT NULL DEFAULT 0
                    CHECK(reserved_microusd >= 0),
                CHECK(spent_microusd + reserved_microusd <= limit_microusd)
            )""",
            """CREATE TABLE candidates (
                candidate_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                source_id TEXT NOT NULL,
                data_json TEXT NOT NULL
            )""",
            """CREATE TABLE crop_tracks (
                track_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                candidate_id TEXT NOT NULL
                    REFERENCES candidates(candidate_id) ON DELETE CASCADE,
                data_json TEXT NOT NULL
            )""",
            """CREATE TABLE plan_outputs (
                plan_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                data_json TEXT NOT NULL,
                UNIQUE(job_id, filename)
            )""",
        ),
    ),
)
LATEST_MIGRATION_VERSION = MIGRATIONS[-1].version


def run_migrations(connection: sqlite3.Connection) -> None:
    """Apply every pending migration atomically and in version order."""
    row = connection.execute(
        "SELECT value FROM schema_metadata WHERE key = ?", ("migration_version",)
    ).fetchone()
    if row is None:
        raise RuntimeError("database is missing migration version metadata")
    current_version = int(row[0])
    if current_version > LATEST_MIGRATION_VERSION:
        raise RuntimeError(
            f"database migration version {current_version} is newer than supported "
            f"version {LATEST_MIGRATION_VERSION}"
        )

    for migration in MIGRATIONS:
        if migration.version <= current_version:
            continue
        if migration.version != current_version + 1:
            raise RuntimeError(
                f"missing migration between versions {current_version} and "
                f"{migration.version}"
            )
        connection.execute("BEGIN")
        try:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "UPDATE schema_metadata SET value = ? WHERE key = ?",
                (str(migration.version), "migration_version"),
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
            current_version = migration.version
