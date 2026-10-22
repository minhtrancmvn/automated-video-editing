"""Evidence storage errors must not strand known provider billing."""

from decimal import Decimal
from pathlib import Path

import pytest
from test_analysis_orchestrator import _usage

from video_editor.analysis.models import RequestReservation
from video_editor.analysis.orchestrator import _record_provider_failure
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.persistence.database import JobStore


@pytest.mark.parametrize("failure_site", ["write", "artifact"])
@pytest.mark.parametrize("billing", ["known", "unknown", "absent", "nonbillable"])
def test_billing_recorded_before_failure_evidence_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
    billing: str,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, Decimal(1))
        request_id = "failure-evidence-request"
        store.reserve_request(
            RequestReservation(
                request_id=request_id,
                job_id=job_id,
                cache_key="failure-evidence-key",
                mode="candidate",
                maximum_cost_usd=Decimal("0.20"),
            )
        )
        store.mark_request_dispatched(request_id)
        details = {}
        if billing in {"known", "unknown"}:
            usage = _usage(request_id, billing == "unknown")
            if billing == "known":
                usage = usage.model_copy(update={"actual_cost_usd": Decimal("0.05")})
            details["usage"] = usage.model_dump_json()
        error = VideoEditorError(
            ErrorCategory.PROVIDER,
            "offline provider failure",
            code="provider_invalid_request"
            if billing == "nonbillable"
            else "provider_response_invalid",
            safe_details=details,
        )

        def fail_evidence(*args: object, **kwargs: object) -> None:
            raise OSError("failure evidence unavailable")

        if failure_site == "write":
            monkeypatch.setattr(Path, "write_text", fail_evidence)
        else:
            monkeypatch.setattr(store, "save_artifact", fail_evidence)

        with pytest.raises(OSError, match="failure evidence unavailable"):
            _record_provider_failure(store, request_id, error)

        row = store.connection.execute(
            "SELECT status FROM analysis_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        budget = store.budget_state(job_id)
        if billing == "known":
            assert row["status"] == "completed"
            assert budget.spent_usd == Decimal("0.05")
            assert budget.reserved_usd == 0
        elif billing == "nonbillable":
            assert row["status"] == "released"
            assert budget.spent_usd == 0
            assert budget.reserved_usd == 0
        else:
            assert row["status"] == "billing_unknown"
            assert budget.spent_usd == 0
            assert budget.reserved_usd == Decimal("0.20")
