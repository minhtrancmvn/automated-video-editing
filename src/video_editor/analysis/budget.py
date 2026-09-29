"""Analysis budget preflight facade."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from video_editor.analysis.models import RequestReservation
from video_editor.persistence.database import JobStore

_SECONDS_PER_HOUR = Decimal(3600)


def job_budget(
    source_seconds: Decimal,
    cap_per_hour: Decimal = Decimal("1.00"),
) -> Decimal:
    """Calculate exact job limit from source duration and hourly cap."""
    if (
        not source_seconds.is_finite()
        or not cap_per_hour.is_finite()
        or source_seconds < 0
        or cap_per_hour < 0
    ):
        raise ValueError("duration and cap must be finite and non-negative")
    return source_seconds / _SECONDS_PER_HOUR * cap_per_hour


def reserve_all_uncached_broad_requests(
    store: JobStore,
    job_id: str,
    requests: Sequence[RequestReservation],
) -> tuple[str, ...]:
    """Atomically reserve every uncached broad request for one analysis job."""
    return store.reserve_request_batch(job_id, requests)
