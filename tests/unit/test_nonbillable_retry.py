"""Explicit successor reservations preserve released attempt history."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Lock

import pytest
from test_analysis_orchestrator import (
    FakeProvider,
    _authorize,
    _candidate_request,
    _paths,
    _save_chunk,
    _segmentation,
)

from video_editor.analysis.models import RequestReservation, SourceRange, UploadedFile
from video_editor.analysis.orchestrator import (
    AnalysisOutcome,
    CandidateAnalysisRequest,
    analysis_cache_key,
    coverage_report,
    run_candidate_refinement,
)
from video_editor.errors import VideoEditorError
from video_editor.persistence.database import JobStore
from video_editor.persistence.migrations import MIGRATIONS


def reservation(job: str, request: str, maximum: str = "0.1") -> RequestReservation:
    return RequestReservation(
        request_id=request,
        job_id=job,
        cache_key="unchanged-key",
        mode="candidate",
        maximum_cost_usd=Decimal(maximum),
    )


def test_cached_result_does_not_bypass_explicit_retry_guard(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job({}, {})
        store.initialize_budget(job, Decimal(1))
        _save_chunk(store, job, "chunk-1", Decimal(0))
        store.connection.execute(
            "insert into analysis_results values (?, ?, ?, ?, 'candidate', 1, '{}')",
            ("cached", job, "unchanged-key", "chunk-1"),
        )
        store.connection.commit()
        assert store.reserve_request(reservation(job, "ordinary")) is None
        with pytest.raises(ValueError):
            store.reserve_retry_request(
                reservation(job, "retry"), predecessor_request_id="missing"
            )
        assert store.budget_state(job).reserved_usd == 0


def test_explicit_retry_dispatches_through_existing_candidate_orchestrator(
    tmp_path: Path,
) -> None:
    class RetryProvider(FakeProvider):
        def upload(self, authorization):
            old = self.store.connection.execute(
                "select * from analysis_requests where request_id='old'"
            ).fetchone()
            assert old["status"] == "released"
            assert self.store.budget_state(self.job_id).reserved_usd == Decimal("0.20")
            self.events.append("upload")
            current = self.store.connection.execute(
                "select status from analysis_requests where request_id='new'"
            ).fetchone()
            assert current["status"] == "dispatched"
            authorization.close()
            return UploadedFile(
                name=f"files/{authorization.manifest_id}",
                uri="gs://offline/upload",
                mime_type="video/mp4",
                state="ACTIVE",
                manifest_id=authorization.manifest_id,
            )

    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job({}, {})
        store.initialize_budget(job, Decimal(1))
        _save_chunk(store, job, "chunk-1", Decimal(0))
        template = _candidate_request(1, 0.1)
        key = analysis_cache_key(template.cache_identity)
        old_reservation = RequestReservation(
            request_id="old",
            job_id=job,
            cache_key=key,
            mode="candidate",
            maximum_cost_usd=Decimal("0.20"),
        )
        store.reserve_request(old_reservation)
        store.mark_request_dispatched("old")
        store.release_confirmed_nonbillable("old")
        before = dict(
            store.connection.execute(
                "select * from analysis_requests where request_id='old'"
            ).fetchone()
        )
        new = old_reservation.model_copy(update={"request_id": "new"})
        store.reserve_retry_request(new, predecessor_request_id="old")
        request = CandidateAnalysisRequest(
            template.manifest_id,
            template.candidate,
            template.cache_identity,
            template.maximum_cost_usd,
            request_id="new",
        )
        provider = RetryProvider(store, job)
        ranges = [SourceRange(source_id="source-1", start=Decimal(0), end=Decimal(1))]
        outcome = run_candidate_refinement(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job,
            broad_outcome=AnalysisOutcome(
                state="complete", coverage=coverage_report(ranges, ranges)
            ),
            requests=[request],
            segmentations=[_segmentation(1, 0.1)],
            upload_validator=_authorize,
        )
        assert outcome.state == "complete"
        assert provider.candidate_calls == 1 and provider.broad_calls == 0
        assert provider.deleted == ["files/manifest-chunk-1"]
        assert store.find_analysis_result(job, key)["validated"]
        assert (
            dict(
                store.connection.execute(
                    "select * from analysis_requests where request_id='old'"
                ).fetchone()
            )
            == before
        )
        assert store.budget_state(job).reserved_usd == 0


def test_explicit_retry_preserves_released_row_and_reuses_reservation(
    tmp_path: Path,
) -> None:
    db = tmp_path / "state.db"
    with JobStore(db) as store:
        job = store.create_job({}, {})
        store.initialize_budget(job, Decimal(1))
        old = reservation(job, "old")
        store.reserve_request(old)
        store.mark_request_dispatched("old")
        store.release_confirmed_nonbillable("old")
        before = dict(
            store.connection.execute("select * from analysis_requests").fetchone()
        )
        new = reservation(job, "new", "0.2")
        with pytest.raises(ValueError):
            store.reserve_request(new)
        assert store.reserve_retry_request(new, predecessor_request_id="old") == "new"
        assert store.reserve_retry_request(new, predecessor_request_id="old") == "new"
        assert store.reserve_request(new) == "new"
        store.verify_request_dispatchable(new)
        assert (
            dict(
                store.connection.execute(
                    "select * from analysis_requests where request_id='old'"
                ).fetchone()
            )
            == before
        )
        assert store.budget_state(job).reserved_usd == Decimal("0.2")
        link = dict(
            store.connection.execute(
                "select * from analysis_request_retries"
            ).fetchone()
        )
        assert (
            link["predecessor_request_id"] == "old"
            and link["successor_request_id"] == "new"
        )
        with pytest.raises(ValueError):
            store.reserve_retry_request(
                reservation(job, "other"), predecessor_request_id="old"
            )
        store.mark_request_dispatched("new")
        store.settle_request("new", Decimal("0.01"))
        assert store.budget_state(job).spent_usd == Decimal("0.01")
        assert store.budget_state(job).reserved_usd == 0
    with JobStore(db) as store:
        assert (
            store.connection.execute(
                "select count(*) from analysis_requests"
            ).fetchone()[0]
            == 2
        )
        with pytest.raises(ValueError):
            store.reserve_retry_request(
                reservation(job, "other"), predecessor_request_id="new"
            )


@pytest.mark.parametrize(
    "state", ["reserved", "dispatched", "billing_unknown", "completed"]
)
def test_retry_rejects_unreleased_predecessor_without_budget_mutation(
    tmp_path: Path, state: str
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job({}, {})
        store.initialize_budget(job, Decimal(1))
        store.reserve_request(reservation(job, "old"))
        if state != "reserved":
            store.mark_request_dispatched("old")
        if state == "billing_unknown":
            store.mark_request_billing_unknown("old")
        if state == "completed":
            store.settle_request("old", Decimal(0))
        before = store.budget_state(job)
        with pytest.raises(ValueError):
            store.reserve_retry_request(
                reservation(job, "new"), predecessor_request_id="old"
            )
        assert store.budget_state(job) == before
        assert (
            store.connection.execute(
                "select count(*) from analysis_requests"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "other"),
        ("cache_key", "other"),
        ("mode", "broad"),
        ("request_id", "old"),
    ],
)
def test_retry_rejects_changed_owner_identity_or_old_request_id(
    tmp_path: Path, field: str, value: str
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job({}, {})
        store.initialize_budget(job, Decimal(1))
        store.reserve_request(reservation(job, "old"))
        store.release_confirmed_nonbillable("old")
        new = reservation(job, "new").model_copy(update={field: value})
        with pytest.raises((KeyError, ValueError)):
            store.reserve_retry_request(new, predecessor_request_id="old")
        assert store.budget_state(job).reserved_usd == 0
        assert (
            store.connection.execute(
                "select count(*) from analysis_request_retries"
            ).fetchone()[0]
            == 0
        )


def test_retry_budget_failure_rolls_back_successor_and_link(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job({}, {})
        store.initialize_budget(job, Decimal("0.1"))
        store.reserve_request(reservation(job, "old"))
        store.release_confirmed_nonbillable("old")
        with pytest.raises(VideoEditorError) as caught:
            store.reserve_retry_request(
                reservation(job, "new", "0.2"), predecessor_request_id="old"
            )
        assert caught.value.code == "budget_exhausted"
        assert store.budget_state(job).reserved_usd == 0
        assert (
            store.connection.execute(
                "select count(*) from analysis_requests"
            ).fetchone()[0]
            == 1
        )
        assert (
            store.connection.execute(
                "select count(*) from analysis_request_retries"
            ).fetchone()[0]
            == 0
        )


def test_only_one_concurrent_successor_can_reserve(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with JobStore(db) as store:
        job = store.create_job({}, {})
        store.initialize_budget(job, Decimal(1))
        store.reserve_request(reservation(job, "old"))
        store.release_confirmed_nonbillable("old")
    barrier = Barrier(2)
    startup = Lock()

    def reserve(name: str) -> bool:
        store = JobStore(db)
        with startup:
            store.__enter__()
        try:
            barrier.wait(timeout=10)
            try:
                store.reserve_retry_request(
                    reservation(job, name), predecessor_request_id="old"
                )
            except ValueError:
                return False
            return True
        finally:
            store.__exit__(None, None, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, ("a", "b"))) == [False, True]
    with JobStore(db) as store:
        assert store.budget_state(job).reserved_usd == Decimal("0.1")
        assert (
            store.connection.execute(
                "select count(*) from analysis_request_retries"
            ).fetchone()[0]
            == 1
        )


def test_migration_preserves_v2_requests_budgets_and_foreign_keys(
    tmp_path: Path,
) -> None:
    db = tmp_path / "v2.db"
    with JobStore(db) as store:
        job = store.create_job({}, {})
        store.initialize_budget(job, Decimal(1))
        store.reserve_request(reservation(job, "released"))
        store.release_confirmed_nonbillable("released")
        store.reserve_request(
            reservation(job, "unknown").model_copy(update={"cache_key": "other-key"})
        )
        store.mark_request_billing_unknown("unknown")
        before = [
            tuple(r)
            for r in store.connection.execute(
                "select * from analysis_requests order by request_id"
            )
        ]
        budget = store.budget_state(job)
    connection = sqlite3.connect(db)
    connection.execute("drop table analysis_request_retries")
    connection.execute("drop index analysis_requests_active_key")
    connection.execute("drop index analysis_requests_history_key")
    connection.execute("alter table analysis_requests rename to requests_new")
    connection.execute(
        next(
            sql
            for sql in MIGRATIONS[0].statements
            if sql.startswith("CREATE TABLE analysis_requests (")
        )
    )
    connection.execute("insert into analysis_requests select * from requests_new")
    connection.execute("drop table requests_new")
    connection.execute(
        "update schema_metadata set value='2' where key='migration_version'"
    )
    connection.commit()
    connection.close()
    with JobStore(db) as store:
        assert store.migration_version() == 3
        assert [
            tuple(r)
            for r in store.connection.execute(
                "select * from analysis_requests order by request_id"
            )
        ] == before
        assert store.budget_state(job) == budget
        assert store.connection.execute("pragma foreign_key_check").fetchall() == []
        store.reserve_retry_request(
            reservation(job, "retry"), predecessor_request_id="released"
        )
        with pytest.raises(ValueError):
            store.reserve_retry_request(
                reservation(job, "invalid").model_copy(
                    update={"cache_key": "other-key"}
                ),
                predecessor_request_id="unknown",
            )


def test_retry_rejects_released_predecessor_with_nonzero_or_missing_cost(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job({}, {})
        store.initialize_budget(job, Decimal(1))
        store.reserve_request(reservation(job, "old"))
        store.release_confirmed_nonbillable("old")
        for cost in (None, 1):
            store.connection.execute(
                "update analysis_requests set actual_cost_microusd=? where request_id='old'",
                (cost,),
            )
            store.connection.commit()
            with pytest.raises(ValueError):
                store.reserve_retry_request(
                    reservation(job, "new"), predecessor_request_id="old"
                )
        assert store.budget_state(job).reserved_usd == 0


def test_retry_of_retry_requires_explicit_leaf_and_preserves_history(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job({}, {})
        store.initialize_budget(job, Decimal(1))
        store.reserve_request(reservation(job, "old"))
        store.release_confirmed_nonbillable("old")
        store.reserve_retry_request(
            reservation(job, "second"), predecessor_request_id="old"
        )
        store.release_confirmed_nonbillable("second")
        with pytest.raises(ValueError):
            store.reserve_request(reservation(job, "third"))
        with pytest.raises(ValueError):
            store.reserve_retry_request(
                reservation(job, "third"), predecessor_request_id="old"
            )
        store.reserve_retry_request(
            reservation(job, "third"), predecessor_request_id="second"
        )
        assert store.reserve_request(reservation(job, "third")) == "third"
        assert (
            store.connection.execute(
                "select count(*) from analysis_requests"
            ).fetchone()[0]
            == 3
        )
        assert (
            store.connection.execute(
                "select count(*) from analysis_request_retries"
            ).fetchone()[0]
            == 2
        )
        assert store.budget_state(job).reserved_usd == Decimal("0.1")
