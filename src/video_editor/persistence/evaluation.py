"""Additive SQLite ledger for video-aware evaluation requests and results."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING, Any

from video_editor.errors import ErrorCategory, VideoEditorError

if TYPE_CHECKING:
    from video_editor.persistence.database import JobStore

VALID_EVALUATION_PHASES = frozenset({"video_grounding", "editorial_comparison"})

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "client_secret",
        "private_key",
        "x_api_key",
        "key",
        "token",
        "authorization",
        "credential",
        "credentials",
        "secret",
        "password",
    }
)

_MICRO_USD = Decimal(1_000_000)
_SQLITE_MAX_INTEGER = 2**63 - 1


def _now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


def _normalize_key(key: object) -> str:
    return str(key).lower().replace("-", "_").replace(" ", "_")


def _json_without_secrets(value: Any) -> str:
    """Serialize value to JSON after ensuring no secret-like keys exist."""

    def reject_secrets(item: Any) -> None:
        if isinstance(item, Mapping):
            for k, v in item.items():
                if _normalize_key(k) in _SECRET_KEYS:
                    raise ValueError(
                        f"evaluation payload contains secret-like key: {k}"
                    )
                reject_secrets(v)
        elif isinstance(item, (list, tuple)):
            for v in item:
                reject_secrets(v)

    reject_secrets(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row_json(value: str | None) -> Any:
    """Deserialize JSON string from database row."""
    if value is None:
        return None
    return json.loads(value)


def _usd_to_microusd(amount: Decimal, rounding: str = ROUND_CEILING) -> int:
    """Convert USD Decimal to integer microUSD using specified rounding."""
    if not isinstance(amount, Decimal):
        raise TypeError(f"USD amount must be Decimal, got {type(amount).__name__}")
    if not amount.is_finite() or amount < 0:
        raise ValueError("USD amount must be finite and non-negative")
    microusd = int((amount * _MICRO_USD).to_integral_value(rounding=rounding))
    if microusd > _SQLITE_MAX_INTEGER:
        raise OverflowError("USD amount exceeds SQLite integer range")
    return microusd


def _exact_usd_to_microusd(amount: Decimal) -> int:
    """Convert USD Decimal to exact integer microUSD, rejecting non-exact fractions."""
    if not isinstance(amount, Decimal):
        raise TypeError(f"USD amount must be Decimal, got {type(amount).__name__}")
    if not amount.is_finite() or amount < 0:
        raise ValueError("USD amount must be finite and non-negative")
    scaled = amount * _MICRO_USD
    if scaled != scaled.to_integral_value():
        raise ValueError("actual USD cost must be representable in whole microUSD")
    microusd = int(scaled)
    if microusd > _SQLITE_MAX_INTEGER:
        raise OverflowError("USD amount exceeds SQLite integer range")
    return microusd


def _microusd_to_usd(amount: int) -> Decimal:
    """Convert integer microUSD back to USD Decimal."""
    return Decimal(amount) / _MICRO_USD


class EvaluationLedger:
    """Additive persistence ledger for video-aware evaluation requests and results."""

    def __init__(self, store: JobStore) -> None:
        self.store = store

    def reserve(
        self,
        *,
        request_id: str,
        job_id: str,
        cache_key: str,
        phase: str,
        maximum_cost_usd: Decimal,
        payload: dict[str, Any],
    ) -> None:
        """Reserve evaluation budget and persist a reserved evaluation request.

        Phase must be video_grounding or editorial_comparison.
        Shared budget account is updated atomically.
        Re-dispatch of existing request ID or (job_id, cache_key) is rejected.
        """
        if phase not in VALID_EVALUATION_PHASES:
            raise ValueError(
                f"unsupported evaluation phase: {phase}; "
                f"must be one of {sorted(VALID_EVALUATION_PHASES)}"
            )
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id must be a non-empty string")
        if not isinstance(cache_key, str) or not cache_key:
            raise ValueError("cache_key must be a non-empty string")
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")

        maximum_microusd = _usd_to_microusd(maximum_cost_usd, ROUND_CEILING)
        payload_json = _json_without_secrets(payload)
        now = _now()

        connection = self.store.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            job_row = connection.execute(
                "SELECT id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                raise KeyError(f"unknown job: {job_id}")

            overrun_row = connection.execute(
                """SELECT request_id FROM evaluation_requests
                WHERE job_id = ? AND actual_cost_microusd > maximum_cost_microusd
                LIMIT 1""",
                (job_id,),
            ).fetchone()
            if overrun_row is not None:
                raise VideoEditorError(
                    ErrorCategory.BUDGET,
                    f"job has un-reconciled evaluator overrun: {overrun_row['request_id']}",
                    code="evaluator_overrun_lock",
                )

            existing = connection.execute(
                """SELECT request_id, job_id, cache_key FROM evaluation_requests
                WHERE request_id = ? OR (job_id = ? AND cache_key = ?)""",
                (request_id, job_id, cache_key),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"evaluation request ID or cache key already exists: "
                    f"request_id={request_id}, job_id={job_id}, cache_key={cache_key}"
                )

            account = connection.execute(
                """SELECT limit_microusd, spent_microusd, reserved_microusd
                FROM budget_accounts WHERE job_id = ?""",
                (job_id,),
            ).fetchone()
            if account is None:
                raise KeyError(f"budget is not initialized: {job_id}")

            spent = int(account["spent_microusd"])
            reserved = int(account["reserved_microusd"])
            limit = int(account["limit_microusd"])

            if spent + reserved + maximum_microusd > limit:
                raise VideoEditorError(
                    ErrorCategory.BUDGET,
                    "request exceeds job budget",
                    code="budget_exhausted",
                )

            connection.execute(
                """INSERT INTO evaluation_requests (
                    request_id, job_id, cache_key, phase, status,
                    maximum_cost_microusd, actual_cost_microusd, data_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'reserved', ?, NULL, ?, ?, ?)""",
                (
                    request_id,
                    job_id,
                    cache_key,
                    phase,
                    maximum_microusd,
                    payload_json,
                    now,
                    now,
                ),
            )

            connection.execute(
                """UPDATE budget_accounts
                SET reserved_microusd = reserved_microusd + ?
                WHERE job_id = ?""",
                (maximum_microusd, job_id),
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def mark_dispatched(self, request_id: str) -> None:
        """Record that provider dispatch began for a provably reserved request."""
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        now = _now()
        connection = self.store.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT status FROM evaluation_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown evaluation request: {request_id}")
            if row["status"] != "reserved":
                raise ValueError(
                    f"only a reserved request can be dispatched; current status: {row['status']}"
                )
            connection.execute(
                "UPDATE evaluation_requests SET status = 'dispatched', updated_at = ? WHERE request_id = ?",
                (now, request_id),
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def finish(
        self,
        request_id: str,
        *,
        result: dict[str, Any] | None,
        usage: dict[str, Any],
        actual_cost_usd: Decimal | None,
    ) -> None:
        """Complete or mark billing_unknown for a dispatched evaluation request.

        Known actual cost settles own reservation.
        None actual cost retains full reservation hold under billing_unknown status.
        Result is persisted separately and binds original request/cache/job.
        """
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        if not isinstance(usage, dict):
            raise TypeError("usage must be a dict")
        if result is not None and not isinstance(result, dict):
            raise TypeError("result must be None or a dict")

        usage_json = _json_without_secrets(usage)
        result_json = _json_without_secrets(result) if result is not None else None
        now = _now()
        overrun = False

        connection = self.store.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                """SELECT request_id, job_id, cache_key, phase, status,
                          maximum_cost_microusd
                FROM evaluation_requests WHERE request_id = ?""",
                (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown evaluation request: {request_id}")
            if row["status"] != "dispatched":
                raise ValueError(
                    f"only a dispatched request can be finished; current status: {row['status']}"
                )

            job_id = row["job_id"]
            cache_key = row["cache_key"]
            maximum_microusd = int(row["maximum_cost_microusd"])

            connection.execute(
                """INSERT INTO evaluation_results (
                    request_id, job_id, cache_key, result_json, usage_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (request_id, job_id, cache_key, result_json, usage_json, now),
            )

            if actual_cost_usd is not None:
                actual_microusd = _exact_usd_to_microusd(actual_cost_usd)
                if actual_microusd > maximum_microusd:
                    overrun = True

                account = connection.execute(
                    """SELECT limit_microusd, spent_microusd, reserved_microusd
                    FROM budget_accounts WHERE job_id = ?""",
                    (job_id,),
                ).fetchone()
                if account is None:
                    raise KeyError(f"budget is not initialized: {job_id}")

                spent = int(account["spent_microusd"])
                reserved = int(account["reserved_microusd"])
                limit = int(account["limit_microusd"])

                new_reserved = max(0, reserved - maximum_microusd)
                if spent + actual_microusd + new_reserved <= limit:
                    new_spent = spent + actual_microusd
                else:
                    new_spent = limit - new_reserved

                connection.execute(
                    """UPDATE budget_accounts
                    SET reserved_microusd = ?,
                        spent_microusd = ?
                    WHERE job_id = ?""",
                    (new_reserved, new_spent, job_id),
                )
                connection.execute(
                    """UPDATE evaluation_requests
                    SET status = 'completed', actual_cost_microusd = ?, updated_at = ?
                    WHERE request_id = ?""",
                    (actual_microusd, now, request_id),
                )
            else:
                connection.execute(
                    """UPDATE evaluation_requests
                    SET status = 'billing_unknown', actual_cost_microusd = NULL, updated_at = ?
                    WHERE request_id = ?""",
                    (now, request_id),
                )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

        if overrun:
            raise VideoEditorError(
                ErrorCategory.BUDGET,
                "actual request cost exceeds reservation",
                code="actual_cost_exceeds_reservation",
            )

    def release_predispatch(self, request_id: str) -> None:
        """Release reservation hold for a provably reserved request before dispatch."""
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        now = _now()
        connection = self.store.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                """SELECT request_id, job_id, status, maximum_cost_microusd
                FROM evaluation_requests WHERE request_id = ?""",
                (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown evaluation request: {request_id}")
            if row["status"] != "reserved":
                raise ValueError(
                    f"only a reserved request can be released predispatch; current status: {row['status']}"
                )

            job_id = row["job_id"]
            maximum_microusd = int(row["maximum_cost_microusd"])

            connection.execute(
                """UPDATE budget_accounts
                SET reserved_microusd = reserved_microusd - ?
                WHERE job_id = ?""",
                (maximum_microusd, job_id),
            )
            connection.execute(
                """UPDATE evaluation_requests
                SET status = 'released', actual_cost_microusd = 0, updated_at = ?
                WHERE request_id = ?""",
                (now, request_id),
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        """Retrieve evaluation request details by request ID."""
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        row = self.store.connection.execute(
            """SELECT request_id, job_id, cache_key, phase, status,
                      maximum_cost_microusd, actual_cost_microusd, data_json
            FROM evaluation_requests WHERE request_id = ?""",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        actual_microusd = row["actual_cost_microusd"]
        return {
            "request_id": row["request_id"],
            "job_id": row["job_id"],
            "cache_key": row["cache_key"],
            "phase": row["phase"],
            "status": row["status"],
            "maximum_cost_usd": _microusd_to_usd(int(row["maximum_cost_microusd"])),
            "actual_cost_usd": (
                _microusd_to_usd(int(actual_microusd))
                if actual_microusd is not None
                else None
            ),
            "payload": _row_json(row["data_json"]),
        }

    def find_result(self, job_id: str, cache_key: str) -> dict[str, Any] | None:
        """Find evaluation result by job ID and cache key.

        Returns unknown-billing result with its status preserved; never silently promotes.
        """
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id must be a non-empty string")
        if not isinstance(cache_key, str) or not cache_key:
            raise ValueError("cache_key must be a non-empty string")
        row = self.store.connection.execute(
            """SELECT res.request_id, res.job_id, res.cache_key,
                      req.phase, req.status,
                      req.maximum_cost_microusd, req.actual_cost_microusd,
                      res.result_json, res.usage_json
            FROM evaluation_results AS res
            JOIN evaluation_requests AS req ON req.request_id = res.request_id
            WHERE res.job_id = ? AND res.cache_key = ?""",
            (job_id, cache_key),
        ).fetchone()
        if row is None:
            return None
        actual_microusd = row["actual_cost_microusd"]
        return {
            "request_id": row["request_id"],
            "job_id": row["job_id"],
            "cache_key": row["cache_key"],
            "phase": row["phase"],
            "status": row["status"],
            "maximum_cost_usd": _microusd_to_usd(int(row["maximum_cost_microusd"])),
            "actual_cost_usd": (
                _microusd_to_usd(int(actual_microusd))
                if actual_microusd is not None
                else None
            ),
            "result": _row_json(row["result_json"]),
            "usage": _row_json(row["usage_json"]),
        }
