"""Tests for the additive evaluator ledger and schema migration v4."""

from __future__ import annotations

import concurrent.futures
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from video_editor.analysis.models import RequestReservation
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.persistence.database import JobStore
from video_editor.persistence.evaluation import EvaluationLedger
from video_editor.persistence.migrations import LATEST_MIGRATION_VERSION, MIGRATIONS


def _create_v3_database(db_path: Path) -> None:
    """Create a database at migration v3 with historical data."""
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
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
            INSERT INTO schema_metadata(key, value) VALUES ('migration_version', '1');
            """
        )
    # Run migrations up to v3 using the first 2 migration entries (v2 and v3)
    with sqlite3.connect(db_path) as conn:
        for migration in MIGRATIONS:
            if migration.version > 3:
                continue
            conn.execute("BEGIN")
            for stmt in migration.statements:
                conn.execute(stmt)
            conn.execute(
                "UPDATE schema_metadata SET value = ? WHERE key = ?",
                (str(migration.version), "migration_version"),
            )
            conn.commit()


def test_no_default_db_access_on_import(tmp_path: Path) -> None:
    """Importing persistence.evaluation must not touch filesystem or default database."""
    import video_editor.persistence.evaluation as eval_mod

    assert hasattr(eval_mod, "EvaluationLedger")


def test_migration_v4_appends_evaluator_tables_and_preserves_history(
    tmp_path: Path,
) -> None:
    """Migration v4 creates evaluator tables without altering existing rows or definitions."""
    db_path = tmp_path / "state.db"
    _create_v3_database(db_path)

    # Insert historical job, analysis request with billing_unknown, and budget account
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO jobs (id, config_json, volume_json, status, created_at, updated_at)
            VALUES ('job-hist', '{}', '{}', 'running', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"""
        )
        conn.execute(
            """INSERT INTO budget_accounts (job_id, limit_microusd, spent_microusd, reserved_microusd)
            VALUES ('job-hist', 1000000, 200000, 300000)"""
        )
        conn.execute(
            """INSERT INTO analysis_requests (
                request_id, job_id, cache_key, mode, status,
                maximum_cost_microusd, actual_cost_microusd, data_json
            ) VALUES (
                'req-hist', 'job-hist', 'cache-hist', 'broad', 'billing_unknown',
                300000, NULL, '{"mode":"broad"}'
            )"""
        )
        conn.commit()

    # Opening JobStore applies pending migrations up to LATEST_MIGRATION_VERSION (v4)
    with JobStore(db_path) as store:
        assert store.migration_version() == 4
        assert LATEST_MIGRATION_VERSION == 4

        # Historical data untouched
        account = store.connection.execute(
            "SELECT * FROM budget_accounts WHERE job_id = 'job-hist'"
        ).fetchone()
        assert account is not None
        assert account["limit_microusd"] == 1000000
        assert account["spent_microusd"] == 200000
        assert account["reserved_microusd"] == 300000

        req = store.connection.execute(
            "SELECT * FROM analysis_requests WHERE request_id = 'req-hist'"
        ).fetchone()
        assert req is not None
        assert req["status"] == "billing_unknown"
        assert req["maximum_cost_microusd"] == 300000
        assert req["actual_cost_microusd"] is None

        # New tables exist
        tables = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "evaluation_requests" in tables
        assert "evaluation_results" in tables

        # No api_key columns in any evaluator table
        for table in ("evaluation_requests", "evaluation_results"):
            cols = [
                row["name"]
                for row in store.connection.execute(f"PRAGMA table_info({table})")
            ]
            assert "api_key" not in cols
            assert "token" not in cols


def test_evaluation_ledger_reserve_success(tmp_path: Path) -> None:
    """Reserve creates a reserved request and updates budget atomically."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("5.00"))
        ledger = EvaluationLedger(store)

        payload = {"proposal_digest": "abc", "phase": "video_grounding"}
        ledger.reserve(
            request_id="req-1",
            job_id=job_id,
            cache_key="cache-1",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.50"),
            payload=payload,
        )

        # Budget state has reserved 500_000 microUSD
        state = store.budget_state(job_id)
        assert state.reserved_usd == Decimal("0.5")
        assert state.spent_usd == Decimal(0)
        assert state.remaining_usd == Decimal("4.5")

        req = ledger.get_request("req-1")
        assert req == {
            "request_id": "req-1",
            "job_id": job_id,
            "cache_key": "cache-1",
            "phase": "video_grounding",
            "status": "reserved",
            "maximum_cost_usd": Decimal("0.5"),
            "actual_cost_usd": None,
            "payload": payload,
        }


def test_evaluation_ledger_reserve_editorial_comparison(tmp_path: Path) -> None:
    """Reserve supports editorial_comparison phase."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("2.00"))
        ledger = EvaluationLedger(store)

        ledger.reserve(
            request_id="req-ed",
            job_id=job_id,
            cache_key="cache-ed",
            phase="editorial_comparison",
            maximum_cost_usd=Decimal("0.25"),
            payload={"comparison": "synthetic"},
        )
        req = ledger.get_request("req-ed")
        assert req is not None
        assert req["phase"] == "editorial_comparison"
        assert req["status"] == "reserved"


def test_evaluation_ledger_reserve_rejects_invalid_phase(tmp_path: Path) -> None:
    """Reserve rejects phases other than video_grounding and editorial_comparison."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("1.00"))
        ledger = EvaluationLedger(store)

        with pytest.raises(ValueError, match="phase"):
            ledger.reserve(
                request_id="req-bad",
                job_id=job_id,
                cache_key="cache-bad",
                phase="broad",  # broad is for analysis, not evaluation
                maximum_cost_usd=Decimal("0.10"),
                payload={},
            )


def test_evaluation_ledger_reserve_budget_exhausted(tmp_path: Path) -> None:
    """Reserve raises VideoEditorError(BUDGET) when requested maximum exceeds budget."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("0.30"))
        ledger = EvaluationLedger(store)

        with pytest.raises(VideoEditorError) as exc_info:
            ledger.reserve(
                request_id="req-over",
                job_id=job_id,
                cache_key="cache-over",
                phase="video_grounding",
                maximum_cost_usd=Decimal("0.50"),
                payload={},
            )
        assert exc_info.value.category == ErrorCategory.BUDGET
        assert exc_info.value.code == "budget_exhausted"

        # Budget unchanged
        state = store.budget_state(job_id)
        assert state.reserved_usd == Decimal(0)
        assert state.remaining_usd == Decimal("0.30")


def test_evaluation_ledger_reserve_stops_replay(tmp_path: Path) -> None:
    """Existing request ID or job/cache key never dispatch again, even after release; no retry."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("2.00"))
        ledger = EvaluationLedger(store)

        ledger.reserve(
            request_id="req-rep",
            job_id=job_id,
            cache_key="cache-rep",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.20"),
            payload={"test": 1},
        )

        # Duplicate request_id with different cache key
        with pytest.raises(ValueError, match="already exists"):
            ledger.reserve(
                request_id="req-rep",
                job_id=job_id,
                cache_key="cache-diff",
                phase="video_grounding",
                maximum_cost_usd=Decimal("0.20"),
                payload={"test": 1},
            )

        # Duplicate (job_id, cache_key) with different request_id
        with pytest.raises(ValueError, match="already exists"):
            ledger.reserve(
                request_id="req-diff",
                job_id=job_id,
                cache_key="cache-rep",
                phase="video_grounding",
                maximum_cost_usd=Decimal("0.20"),
                payload={"test": 1},
            )

        # After releasing predispatch, re-reserving still rejected (no retry API)
        ledger.release_predispatch("req-rep")
        with pytest.raises(ValueError, match="already exists"):
            ledger.reserve(
                request_id="req-rep",
                job_id=job_id,
                cache_key="cache-rep",
                phase="video_grounding",
                maximum_cost_usd=Decimal("0.20"),
                payload={"test": 1},
            )


def test_evaluation_ledger_reserve_rejects_known_overrun_reconciliation_lock(
    tmp_path: Path,
) -> None:
    """Reserve rejects any new reservation on a job that has an un-reconciled evaluator overrun."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("3.00"))
        ledger = EvaluationLedger(store)

        # First request runs and overruns reservation ($0.35 actual vs $0.30 max)
        ledger.reserve(
            request_id="req-over-1",
            job_id=job_id,
            cache_key="cache-over-1",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.30"),
            payload={},
        )
        ledger.mark_dispatched("req-over-1")
        with pytest.raises(VideoEditorError):
            ledger.finish(
                "req-over-1",
                result={},
                usage={},
                actual_cost_usd=Decimal("0.35"),
            )

        # Attempting any subsequent reservation on this job must fail due to reconciliation lock
        with pytest.raises(VideoEditorError) as exc_info:
            ledger.reserve(
                request_id="req-blocked-by-overrun",
                job_id=job_id,
                cache_key="cache-blocked",
                phase="video_grounding",
                maximum_cost_usd=Decimal("0.10"),
                payload={},
            )
        assert exc_info.value.category == ErrorCategory.BUDGET
        assert exc_info.value.code == "evaluator_overrun_lock"

        # Separate job without overrun can still reserve normally
        other_job = store.create_job("{}", "{}")
        store.initialize_budget(other_job, Decimal("1.00"))
        ledger.reserve(
            request_id="req-other-job",
            job_id=other_job,
            cache_key="cache-other",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.10"),
            payload={},
        )
        assert ledger.get_request("req-other-job") is not None


def test_evaluation_ledger_reserve_rejects_secrets(tmp_path: Path) -> None:
    """Reserve rejects payload containing secret keys."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("1.00"))
        ledger = EvaluationLedger(store)

        for secret_key in ("api_key", "access_token", "Client_Secret", "authorization"):
            with pytest.raises(ValueError, match="secret"):
                ledger.reserve(
                    request_id=f"req-sec-{secret_key}",
                    job_id=job_id,
                    cache_key=f"cache-sec-{secret_key}",
                    phase="video_grounding",
                    maximum_cost_usd=Decimal("0.10"),
                    payload={secret_key: "leaked"},
                )


def test_evaluation_ledger_reserve_rejects_secrets_in_tuples(tmp_path: Path) -> None:
    """Reserve rejects secret keys nested inside tuple containers."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("1.00"))
        ledger = EvaluationLedger(store)

        # Direct tuple containing dict with secret
        with pytest.raises(ValueError, match="secret"):
            ledger.reserve(
                request_id="req-sec-tup-1",
                job_id=job_id,
                cache_key="cache-sec-tup-1",
                phase="video_grounding",
                maximum_cost_usd=Decimal("0.10"),
                payload={"items": ({"api_key": "leaked"},)},
            )

        # Nested tuple inside list
        with pytest.raises(ValueError, match="secret"):
            ledger.reserve(
                request_id="req-sec-tup-2",
                job_id=job_id,
                cache_key="cache-sec-tup-2",
                phase="video_grounding",
                maximum_cost_usd=Decimal("0.10"),
                payload={"items": [("nested", {"client_secret": "leaked"})]},
            )

        # Deeply nested tuple
        with pytest.raises(ValueError, match="secret"):
            ledger.reserve(
                request_id="req-sec-tup-3",
                job_id=job_id,
                cache_key="cache-sec-tup-3",
                phase="video_grounding",
                maximum_cost_usd=Decimal("0.10"),
                payload={"deep": ("a", ("b", {"access_token": "secret"}))},
            )

        # Safe tuple succeeds
        ledger.reserve(
            request_id="req-safe-tup",
            job_id=job_id,
            cache_key="cache-safe-tup",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.10"),
            payload={"items": ("safe", {"normal_key": "value"})},
        )
        req = ledger.get_request("req-safe-tup")
        assert req is not None
        assert req["payload"] == {"items": ["safe", {"normal_key": "value"}]}


def test_evaluation_ledger_mark_dispatched(tmp_path: Path) -> None:
    """Only reserved request can be marked dispatched."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("1.00"))
        ledger = EvaluationLedger(store)

        ledger.reserve(
            request_id="req-disp",
            job_id=job_id,
            cache_key="cache-disp",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.10"),
            payload={},
        )
        ledger.mark_dispatched("req-disp")

        req = ledger.get_request("req-disp")
        assert req is not None
        assert req["status"] == "dispatched"

        # Calling again on already dispatched request fails
        with pytest.raises(ValueError, match="reserved"):
            ledger.mark_dispatched("req-disp")

        # Unknown request fails
        with pytest.raises(KeyError):
            ledger.mark_dispatched("req-unknown")


def test_evaluation_ledger_finish_known_actual_settlement(tmp_path: Path) -> None:
    """Dispatched request settles reservation with exact known cost."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("2.00"))
        ledger = EvaluationLedger(store)

        ledger.reserve(
            request_id="req-fin",
            job_id=job_id,
            cache_key="cache-fin",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.40"),
            payload={"grounding": "test"},
        )
        ledger.mark_dispatched("req-fin")

        result_payload = {"factual_support": "supported", "confidence": 0.95}
        usage_payload = {"prompt_tokens": 100, "completion_tokens": 20}
        ledger.finish(
            "req-fin",
            result=result_payload,
            usage=usage_payload,
            actual_cost_usd=Decimal("0.25"),
        )

        # Budget settled: reserved reduced by 0.40, spent increased by 0.25
        state = store.budget_state(job_id)
        assert state.reserved_usd == Decimal(0)
        assert state.spent_usd == Decimal("0.25")
        assert state.remaining_usd == Decimal("1.75")

        req = ledger.get_request("req-fin")
        assert req is not None
        assert req["status"] == "completed"
        assert req["actual_cost_usd"] == Decimal("0.25")

        found = ledger.find_result(job_id, "cache-fin")
        assert found == {
            "request_id": "req-fin",
            "job_id": job_id,
            "cache_key": "cache-fin",
            "phase": "video_grounding",
            "status": "completed",
            "maximum_cost_usd": Decimal("0.4"),
            "actual_cost_usd": Decimal("0.25"),
            "result": result_payload,
            "usage": usage_payload,
        }


def test_evaluation_ledger_finish_exceeding_maximum_fails(tmp_path: Path) -> None:
    """Finish persists result, usage, overrun charge, commits, then raises VideoEditorError."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("2.00"))
        ledger = EvaluationLedger(store)

        ledger.reserve(
            request_id="req-overcost",
            job_id=job_id,
            cache_key="cache-overcost",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.30"),
            payload={},
        )
        ledger.mark_dispatched("req-overcost")

        with pytest.raises(VideoEditorError) as exc_info:
            ledger.finish(
                "req-overcost",
                result={"factual_support": "supported"},
                usage={"prompt_tokens": 50},
                actual_cost_usd=Decimal("0.35"),
            )
        assert exc_info.value.category == ErrorCategory.BUDGET
        assert exc_info.value.code == "actual_cost_exceeds_reservation"

        # Overrun must persist: request completed with actual cost, result and usage saved
        req = ledger.get_request("req-overcost")
        assert req is not None
        assert req["status"] == "completed"
        assert req["actual_cost_usd"] == Decimal("0.35")

        found = ledger.find_result(job_id, "cache-overcost")
        assert found is not None
        assert found["status"] == "completed"
        assert found["maximum_cost_usd"] == Decimal("0.3")
        assert found["actual_cost_usd"] == Decimal("0.35")
        # Overrun detection: actual exceeds maximum
        assert found["actual_cost_usd"] > found["maximum_cost_usd"]
        assert found["result"] == {"factual_support": "supported"}
        assert found["usage"] == {"prompt_tokens": 50}

        # Budget state: reservation released, actual cost charged to spent, remaining updated
        state = store.budget_state(job_id)
        assert state.reserved_usd == Decimal(0)
        assert state.spent_usd == Decimal("0.35")
        assert state.remaining_usd == Decimal("1.65")


def test_evaluation_ledger_finish_overrun_exceeding_job_budget_limit(
    tmp_path: Path,
) -> None:
    """Finish with overrun exceeding total job budget charges available, retains full request cost, raises after commit."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("1.00"))
        ledger = EvaluationLedger(store)

        ledger.reserve(
            request_id="req-overlimit",
            job_id=job_id,
            cache_key="cache-overlimit",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.50"),
            payload={},
        )
        ledger.mark_dispatched("req-overlimit")

        # Actual cost is $1.20 on a $1.00 budget (overrun exceeding entire limit)
        with pytest.raises(VideoEditorError) as exc_info:
            ledger.finish(
                "req-overlimit",
                result={"grounding": "overrun"},
                usage={"tokens": 100},
                actual_cost_usd=Decimal("1.20"),
            )
        assert exc_info.value.category == ErrorCategory.BUDGET
        assert exc_info.value.code == "actual_cost_exceeds_reservation"

        # Request stores exact actual cost $1.20 (no silent charge loss)
        req = ledger.get_request("req-overlimit")
        assert req is not None
        assert req["status"] == "completed"
        assert req["actual_cost_usd"] == Decimal("1.20")

        # Result is persisted
        found = ledger.find_result(job_id, "cache-overlimit")
        assert found is not None
        assert found["result"] == {"grounding": "overrun"}

        # Account cannot have negative remaining (CHECK constraint and BudgetState model ge=0);
        # spent is capped at limit, remaining is 0
        state = store.budget_state(job_id)
        assert state.reserved_usd == Decimal(0)
        assert state.spent_usd == Decimal("1.00")
        assert state.remaining_usd == Decimal(0)


def test_evaluation_ledger_finish_unknown_billing_retains_full_hold(
    tmp_path: Path,
) -> None:
    """None actual_cost_usd retains full own hold and sets status billing_unknown."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("1.00"))
        ledger = EvaluationLedger(store)

        ledger.reserve(
            request_id="req-unk",
            job_id=job_id,
            cache_key="cache-unk",
            phase="editorial_comparison",
            maximum_cost_usd=Decimal("0.40"),
            payload={"comparison": "test"},
        )
        ledger.mark_dispatched("req-unk")

        result_payload = {"score": 8}
        usage_payload = {"tokens": 50}
        ledger.finish(
            "req-unk",
            result=result_payload,
            usage=usage_payload,
            actual_cost_usd=None,
        )

        # Budget retains full 0.40 hold
        state = store.budget_state(job_id)
        assert state.reserved_usd == Decimal("0.40")
        assert state.spent_usd == Decimal(0)
        assert state.remaining_usd == Decimal("0.60")

        req = ledger.get_request("req-unk")
        assert req is not None
        assert req["status"] == "billing_unknown"
        assert req["actual_cost_usd"] is None

        # find_result returns billing_unknown status without silently promoting
        found = ledger.find_result(job_id, "cache-unk")
        assert found == {
            "request_id": "req-unk",
            "job_id": job_id,
            "cache_key": "cache-unk",
            "phase": "editorial_comparison",
            "status": "billing_unknown",
            "maximum_cost_usd": Decimal("0.4"),
            "actual_cost_usd": None,
            "result": result_payload,
            "usage": usage_payload,
        }


def test_evaluation_ledger_finish_none_result_with_known_usage(
    tmp_path: Path,
) -> None:
    """Invalid result may be None but known usage still charges."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("1.00"))
        ledger = EvaluationLedger(store)

        ledger.reserve(
            request_id="req-invalid",
            job_id=job_id,
            cache_key="cache-invalid",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.30"),
            payload={},
        )
        ledger.mark_dispatched("req-invalid")

        ledger.finish(
            "req-invalid",
            result=None,
            usage={"error_tokens": 10},
            actual_cost_usd=Decimal("0.10"),
        )

        state = store.budget_state(job_id)
        assert state.reserved_usd == Decimal(0)
        assert state.spent_usd == Decimal("0.10")

        req = ledger.get_request("req-invalid")
        assert req is not None
        assert req["status"] == "completed"

        found = ledger.find_result(job_id, "cache-invalid")
        assert found is not None
        assert found["maximum_cost_usd"] == Decimal("0.3")
        assert found["actual_cost_usd"] == Decimal("0.1")
        assert found["result"] is None
        assert found["usage"] == {"error_tokens": 10}


def test_evaluation_ledger_finish_rejects_non_dispatched(tmp_path: Path) -> None:
    """Only dispatched request can be finished."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("1.00"))
        ledger = EvaluationLedger(store)

        ledger.reserve(
            request_id="req-notdisp",
            job_id=job_id,
            cache_key="cache-notdisp",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.20"),
            payload={},
        )

        with pytest.raises(ValueError, match="dispatched"):
            ledger.finish(
                "req-notdisp", result={}, usage={}, actual_cost_usd=Decimal("0.10")
            )


def test_evaluation_ledger_release_predispatch(tmp_path: Path) -> None:
    """Release predispatch releases hold and transitions to released."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("1.00"))
        ledger = EvaluationLedger(store)

        ledger.reserve(
            request_id="req-rel",
            job_id=job_id,
            cache_key="cache-rel",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.40"),
            payload={},
        )
        assert store.budget_state(job_id).reserved_usd == Decimal("0.40")

        ledger.release_predispatch("req-rel")

        # Reservation released
        assert store.budget_state(job_id).reserved_usd == Decimal(0)
        assert store.budget_state(job_id).remaining_usd == Decimal("1.00")

        req = ledger.get_request("req-rel")
        assert req is not None
        assert req["status"] == "released"
        assert req["actual_cost_usd"] == Decimal(0)

        # Cannot release again
        with pytest.raises(ValueError, match="reserved"):
            ledger.release_predispatch("req-rel")


def test_evaluation_ledger_release_predispatch_rejects_dispatched(
    tmp_path: Path,
) -> None:
    """Cannot release predispatch on dispatched request."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("1.00"))
        ledger = EvaluationLedger(store)

        ledger.reserve(
            request_id="req-disp-rel",
            job_id=job_id,
            cache_key="cache-disp-rel",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.20"),
            payload={},
        )
        ledger.mark_dispatched("req-disp-rel")

        with pytest.raises(ValueError, match="reserved"):
            ledger.release_predispatch("req-disp-rel")


def test_find_result_returns_none_when_no_result(tmp_path: Path) -> None:
    """find_result returns None for unstarted, reserved, dispatched, or released requests."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        store.initialize_budget(job_id, Decimal("1.00"))
        ledger = EvaluationLedger(store)

        assert ledger.find_result(job_id, "nonexistent") is None

        ledger.reserve(
            request_id="req-nr",
            job_id=job_id,
            cache_key="cache-nr",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.10"),
            payload={},
        )
        assert ledger.find_result(job_id, "cache-nr") is None

        ledger.mark_dispatched("req-nr")
        assert ledger.find_result(job_id, "cache-nr") is None


def test_concurrent_old_and_new_budget_contention(tmp_path: Path) -> None:
    """Contention between old analysis requests and new evaluator requests shares budget."""
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job("{}", "{}")
        # Total budget: $1.00
        store.initialize_budget(job_id, Decimal("1.00"))
        ledger = EvaluationLedger(store)

        # Old analysis reserves $0.60
        old_req_id = store.reserve_request(
            RequestReservation(
                request_id="old-req-1",
                job_id=job_id,
                cache_key="old-cache-1",
                mode="broad",
                maximum_cost_usd=Decimal("0.60"),
            )
        )
        assert old_req_id == "old-req-1"

        # New evaluator attempts to reserve $0.50 -> must fail ($0.60 + $0.50 > $1.00)
        with pytest.raises(VideoEditorError) as exc_info:
            ledger.reserve(
                request_id="new-req-1",
                job_id=job_id,
                cache_key="new-cache-1",
                phase="video_grounding",
                maximum_cost_usd=Decimal("0.50"),
                payload={},
            )
        assert exc_info.value.category == ErrorCategory.BUDGET

        # New evaluator can reserve $0.40 exactly ($0.60 + $0.40 == $1.00)
        ledger.reserve(
            request_id="new-req-2",
            job_id=job_id,
            cache_key="new-cache-2",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.40"),
            payload={},
        )

        state = store.budget_state(job_id)
        assert state.reserved_usd == Decimal("1.00")
        assert state.remaining_usd == Decimal(0)

        # Now any additional reserve (old or new) must fail
        with pytest.raises(VideoEditorError):
            store.reserve_request(
                RequestReservation(
                    request_id="old-req-2",
                    job_id=job_id,
                    cache_key="old-cache-2",
                    mode="broad",
                    maximum_cost_usd=Decimal("0.01"),
                )
            )


def test_concurrent_threads_competing_for_budget(tmp_path: Path) -> None:
    """Concurrent threads attempting reservations respect the total budget limit."""
    db_path = tmp_path / "state.db"
    with JobStore(db_path) as store:
        job_id = store.create_job("{}", "{}")
        # Total limit: $1.00. 10 requests of $0.20 each -> exactly 5 can succeed.
        store.initialize_budget(job_id, Decimal("1.00"))

    startup = threading.Lock()
    barrier = threading.Barrier(10)

    def attempt_reservation(index: int) -> bool:
        store = JobStore(db_path)
        with startup:
            store.__enter__()
        try:
            barrier.wait(timeout=10)
            thread_ledger = EvaluationLedger(store)
            try:
                thread_ledger.reserve(
                    request_id=f"concurrent-{index}",
                    job_id=job_id,
                    cache_key=f"cache-{index}",
                    phase="video_grounding",
                    maximum_cost_usd=Decimal("0.20"),
                    payload={},
                )
                return True
            except VideoEditorError:
                return False
        finally:
            store.__exit__(None, None, None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(attempt_reservation, range(10)))

    successes = sum(1 for r in results if r)
    failures = sum(1 for r in results if not r)

    assert successes == 5
    assert failures == 5

    with JobStore(db_path) as verify_store:
        state = verify_store.budget_state(job_id)
        assert state.reserved_usd == Decimal("1.00")
        assert state.remaining_usd == Decimal(0)


def test_historical_unknown_hold_preservation(tmp_path: Path) -> None:
    """Historical unknown reservations in budget_accounts remain held and block evaluator spending."""
    db_path = tmp_path / "state.db"
    _create_v3_database(db_path)

    # Historical state: limit $1.00, spent $0.20, reserved $0.60 (held by billing_unknown)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO jobs (id, config_json, volume_json, status, created_at, updated_at)
            VALUES ('job-hold', '{}', '{}', 'running', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"""
        )
        conn.execute(
            """INSERT INTO budget_accounts (job_id, limit_microusd, spent_microusd, reserved_microusd)
            VALUES ('job-hold', 1000000, 200000, 600000)"""
        )
        conn.execute(
            """INSERT INTO analysis_requests (
                request_id, job_id, cache_key, mode, status,
                maximum_cost_microusd, actual_cost_microusd, data_json
            ) VALUES (
                'req-hold', 'job-hold', 'cache-hold', 'broad', 'billing_unknown',
                600000, NULL, '{}'
            )"""
        )
        conn.commit()

    with JobStore(db_path) as store:
        # Remaining budget is $1.00 - $0.20 - $0.60 = $0.20
        state = store.budget_state("job-hold")
        assert state.remaining_usd == Decimal("0.20")
        assert state.reserved_usd == Decimal("0.60")

        ledger = EvaluationLedger(store)

        # Attempt to reserve $0.25 -> fails due to historical hold
        with pytest.raises(VideoEditorError) as exc_info:
            ledger.reserve(
                request_id="eval-fail",
                job_id="job-hold",
                cache_key="cache-eval-fail",
                phase="video_grounding",
                maximum_cost_usd=Decimal("0.25"),
                payload={},
            )
        assert exc_info.value.category == ErrorCategory.BUDGET

        # Attempt to reserve $0.15 -> succeeds
        ledger.reserve(
            request_id="eval-ok",
            job_id="job-hold",
            cache_key="cache-eval-ok",
            phase="video_grounding",
            maximum_cost_usd=Decimal("0.15"),
            payload={},
        )

        state = store.budget_state("job-hold")
        assert state.reserved_usd == Decimal("0.75")
        assert state.remaining_usd == Decimal("0.05")
