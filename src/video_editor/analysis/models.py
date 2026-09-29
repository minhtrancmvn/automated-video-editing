"""Strict provider-neutral analysis persistence models."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]


class AnalysisModel(BaseModel):
    """Base model that rejects unknown persistence fields."""

    model_config = ConfigDict(extra="forbid")


class SourceRange(AnalysisModel):
    """Half-open source-time interval with exact decimal timestamps."""

    source_id: NonEmptyString
    start: FiniteDecimal = Field(ge=0)
    end: FiniteDecimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        """Reject empty or reversed source intervals."""
        if self.end <= self.start:
            raise ValueError("source range end must be greater than start")
        return self


class RequestReservation(AnalysisModel):
    """Maximum-cost reservation for one provider request."""

    request_id: NonEmptyString
    job_id: NonEmptyString
    cache_key: NonEmptyString
    mode: Literal["broad", "candidate"]
    maximum_cost_usd: FiniteDecimal = Field(gt=0)


class BudgetState(AnalysisModel):
    """Exact persisted budget totals expressed in USD."""

    limit_usd: FiniteDecimal = Field(ge=0)
    spent_usd: FiniteDecimal = Field(ge=0)
    reserved_usd: FiniteDecimal = Field(ge=0)
    remaining_usd: FiniteDecimal = Field(ge=0)
