"""Strict provider-neutral analysis persistence models."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
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


class AnalysisBoundaryKind(StrEnum):
    """Reason an analysis chunk begins and ends."""

    SCENE = "scene"
    CHRONOLOGY = "chronology"
    FIXED = "fixed"


class ProxyManifestData(AnalysisModel):
    """Provider-neutral payload for one generated proxy manifest."""

    schema_version: int = Field(ge=1)
    source_fingerprint: NonEmptyString
    source_identity: NonEmptyString
    artifact_path: NonEmptyString
    generated_root: NonEmptyString
    mapping_version: NonEmptyString
    source_start: FiniteDecimal = Field(ge=0)
    source_end: FiniteDecimal = Field(gt=0)
    proxy_start: FiniteDecimal = Field(ge=0)
    proxy_end: FiniteDecimal = Field(gt=0)
    video_width: int = Field(gt=0)
    video_height: int = Field(gt=0)
    video_fps: FiniteDecimal = Field(gt=0)
    audio_codec: NonEmptyString
    audio_bitrate_bps: int = Field(ge=0)
    implementation_version: NonEmptyString

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        """Reject empty or reversed source and proxy intervals."""
        if self.source_end <= self.source_start:
            raise ValueError("source range end must be greater than start")
        if self.proxy_end <= self.proxy_start:
            raise ValueError("proxy range end must be greater than start")
        return self


class AnalysisChunkData(AnalysisModel):
    """Provider-neutral payload for one source-mapped analysis chunk."""

    schema_version: int = Field(ge=1)
    mapping_version: NonEmptyString
    proxy_start: FiniteDecimal = Field(ge=0)
    proxy_end: FiniteDecimal = Field(gt=0)
    boundary_kind: AnalysisBoundaryKind
    implementation_version: NonEmptyString

    @model_validator(mode="after")
    def validate_proxy_range(self) -> Self:
        """Reject empty or reversed proxy intervals."""
        if self.proxy_end <= self.proxy_start:
            raise ValueError("proxy range end must be greater than start")
        return self


class AnalysisResultData(AnalysisModel):
    """Provider-neutral metadata for one normalized analysis result."""

    schema_version: int = Field(ge=1)
    provider: NonEmptyString
    model: NonEmptyString
    request_id: NonEmptyString
    prompt_version: NonEmptyString
    response_schema_version: NonEmptyString
    implementation_version: NonEmptyString
    token_count: int = Field(ge=0)
    request_token_count: int = Field(ge=0)
    output_token_count: int = Field(ge=0)
    normalized_record_ids: tuple[NonEmptyString, ...]


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
