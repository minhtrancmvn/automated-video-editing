from decimal import Decimal

import pytest
from pydantic import ValidationError

from video_editor.analysis.models import BudgetState, RequestReservation, SourceRange


def test_source_range_accepts_exact_decimal_interval() -> None:
    source_range = SourceRange(source_id="source-1", start="0.125", end="1.250")

    assert source_range.start == Decimal("0.125")
    assert source_range.end == Decimal("1.250")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_source_range_rejects_non_finite_decimals(value: str) -> None:
    with pytest.raises(ValidationError):
        SourceRange(source_id="source-1", start=value, end="1")


@pytest.mark.parametrize(
    ("start", "end"),
    [("1", "1"), ("2", "1"), ("-0.1", "1")],
)
def test_source_range_rejects_invalid_intervals(start: str, end: str) -> None:
    with pytest.raises(ValidationError):
        SourceRange(source_id="source-1", start=start, end=end)


def test_source_range_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SourceRange(source_id="source-1", start="0", end="1", api_key="secret")


@pytest.mark.parametrize("maximum", ["0", "-0.01", "NaN", "Infinity"])
def test_request_reservation_rejects_non_positive_or_non_finite_cost(
    maximum: str,
) -> None:
    with pytest.raises(ValidationError):
        RequestReservation(
            request_id="request-1",
            job_id="job-1",
            cache_key="cache-1",
            mode="broad",
            maximum_cost_usd=maximum,
        )


def test_request_reservation_rejects_extra_fields_and_secret_fields() -> None:
    with pytest.raises(ValidationError):
        RequestReservation(
            request_id="request-1",
            job_id="job-1",
            cache_key="cache-1",
            mode="broad",
            maximum_cost_usd="0.25",
            api_key="secret",
        )


def test_budget_state_rejects_non_finite_values() -> None:
    with pytest.raises(ValidationError):
        BudgetState(
            limit_usd="1",
            spent_usd="0",
            reserved_usd="NaN",
            remaining_usd="1",
        )
