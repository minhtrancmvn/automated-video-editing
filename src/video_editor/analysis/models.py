"""Strict provider-neutral analysis persistence models."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]


class AnalysisModel(BaseModel):
    """Base model that rejects unknown persistence fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


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
    job_id: NonEmptyString
    source_id: NonEmptyString
    chunk_id: NonEmptyString
    source_fingerprint: NonEmptyString
    source_identity: NonEmptyString
    source_path: NonEmptyString
    source_device: int = Field(ge=0)
    source_inode: int = Field(ge=0)
    source_size_bytes: int = Field(ge=0)
    artifact_path: NonEmptyString
    generated_root: NonEmptyString
    generated_root_device: int = Field(ge=0)
    generated_root_inode: int = Field(ge=0)
    mapping_version: NonEmptyString
    upstream_settings_hash: NonEmptyString
    upstream_tool_version: NonEmptyString
    source_start: FiniteDecimal = Field(ge=0)
    source_end: FiniteDecimal = Field(gt=0)
    proxy_start: FiniteDecimal = Field(ge=0)
    proxy_end: FiniteDecimal = Field(gt=0)
    media_duration: FiniteDecimal = Field(gt=0)
    file_size_bytes: int = Field(ge=0)
    file_digest_sha256: NonEmptyString
    file_device: int = Field(ge=0)
    file_inode: int = Field(ge=0)
    video_codec: NonEmptyString
    video_width: int = Field(gt=0)
    video_height: int = Field(gt=0)
    video_fps: FiniteDecimal = Field(gt=0)
    audio_codec: NonEmptyString
    audio_channels: int = Field(gt=0)
    audio_bitrate_bps: int = Field(ge=0)
    audio_probe_bitrate_bps: int = Field(ge=0)
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
    job_id: NonEmptyString = "legacy"
    source_id: NonEmptyString = "legacy"
    source_identity: NonEmptyString = "legacy"
    mapping_version: NonEmptyString
    source_start: FiniteDecimal = Field(default=Decimal(0), ge=0)
    source_end: FiniteDecimal = Field(default=Decimal(1), gt=0)
    proxy_start: FiniteDecimal = Field(ge=0)
    proxy_end: FiniteDecimal = Field(gt=0)
    boundary_kind: AnalysisBoundaryKind
    implementation_version: NonEmptyString

    @model_validator(mode="after")
    def validate_source_range(self) -> Self:
        """Reject empty or reversed source intervals."""
        if self.source_end <= self.source_start:
            raise ValueError("source range end must be greater than start")
        return self

    @model_validator(mode="after")
    def validate_proxy_range(self) -> Self:
        """Reject empty or reversed proxy intervals."""
        if self.proxy_end <= self.proxy_start:
            raise ValueError("proxy range end must be greater than start")
        return self


class EvidenceRange(AnalysisModel):
    """Proxy interval mapped exactly to its original source interval."""

    start: FiniteDecimal = Field(ge=0)
    end: FiniteDecimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        """Reject empty or reversed evidence intervals."""
        if self.end <= self.start:
            raise ValueError("evidence range end must be greater than start")
        return self


class IntervalEvidence(AnalysisModel):
    """Identified local evidence spanning one exact interval."""

    evidence_id: NonEmptyString
    proxy_range: EvidenceRange
    source_range: SourceRange


class ScoredEvidence(IntervalEvidence):
    """Normalized local evidence score over one interval."""

    score: float = Field(ge=0, le=1, allow_inf_nan=False)


class BoundarySuitabilityEvidence(IntervalEvidence):
    """Normalized entry and exit suitability for one source-mapped interval."""

    entry_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    exit_score: float = Field(ge=0, le=1, allow_inf_nan=False)


class LocalSegmentation(AnalysisModel):
    """Deterministic source-mapped local media evidence."""

    schema_version: int = Field(ge=1)
    implementation_version: NonEmptyString
    settings_hash: NonEmptyString
    source_id: NonEmptyString
    source_identity: NonEmptyString
    proxy_settings_hash: NonEmptyString
    proxy_tool_version: NonEmptyString
    scenes: tuple[IntervalEvidence, ...]
    silence_ranges: tuple[IntervalEvidence, ...]
    speech_presence_ranges: tuple[IntervalEvidence, ...]
    audio_energy: tuple[ScoredEvidence, ...]
    audio_transients: tuple[ScoredEvidence, ...]
    motion: tuple[ScoredEvidence, ...]
    motion_continuity: tuple[ScoredEvidence, ...]
    blur: tuple[ScoredEvidence, ...]
    shake: tuple[ScoredEvidence, ...]
    exposure: tuple[ScoredEvidence, ...]
    obstruction: tuple[ScoredEvidence, ...]
    boundary_suitability: tuple[BoundarySuitabilityEvidence, ...]
    candidate_windows: tuple[IntervalEvidence, ...]


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


class ProxyManifest(Protocol):
    """Minimum validated manifest interface required by providers."""

    path: Path


class AnalysisChunk(Protocol):
    """Provider-neutral broad-scan interval."""

    chunk_id: str
    proxy_start: Decimal
    proxy_end: Decimal


class CandidateWindow(AnalysisModel):
    """Provider-neutral candidate interval for detailed analysis."""

    chunk_id: NonEmptyString
    candidate_id: NonEmptyString
    start: FiniteDecimal = Field(ge=0)
    end: FiniteDecimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        """Reject empty or reversed candidate intervals."""
        if self.end <= self.start:
            raise ValueError("candidate end must be greater than start")
        return self


class UploadedFile(AnalysisModel):
    """Provider upload identity with local manifest retained for reupload."""

    name: NonEmptyString
    uri: NonEmptyString
    mime_type: NonEmptyString
    state: NonEmptyString
    expiration_time: str | None = None
    manifest: object


class RetryPolicy(AnalysisModel):
    """Bounded provider retry policy."""

    max_attempts: int = Field(default=3, ge=1)
    base_delay_seconds: FiniteDecimal = Field(default=Decimal(1), ge=0)


class ProviderUsage(AnalysisModel):
    """Safe provider request usage metadata."""

    request_id: NonEmptyString
    prompt_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    actual_cost_usd: FiniteDecimal = Field(default=Decimal(0), ge=0)

    def safe_payload(self) -> dict[str, str | int]:
        """Return persistence-safe usage fields without credentials or client data."""
        return {
            "request_id": self.request_id,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


class TimeRange(AnalysisModel):
    """Strict provider-returned time interval."""

    start: FiniteDecimal = Field(ge=0)
    end: FiniteDecimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        """Reject empty or reversed provider intervals."""
        if self.end <= self.start:
            raise ValueError("interval end must be greater than start")
        return self


class BroadScene(TimeRange):
    """Normalized broad-scan scene evidence."""

    scene_id: NonEmptyString
    summary: NonEmptyString
    actions: tuple[NonEmptyString, ...]
    setting: NonEmptyString
    scenic_interest: float = Field(ge=0, le=1, allow_inf_nan=False)
    human_interaction: float = Field(ge=0, le=1, allow_inf_nan=False)
    story_milestone: bool
    technical_problems: tuple[NonEmptyString, ...]
    vertical_suitability: float = Field(ge=0, le=1, allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


class BroadCandidate(TimeRange):
    """Normalized broad-scan highlight candidate."""

    candidate_id: NonEmptyString
    category: NonEmptyString
    reason: NonEmptyString
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


class BroadScanResponse(AnalysisModel):
    """Strict broad scan without transcript, identity, or speech meaning."""

    schema_version: Literal["broad-v1"]
    chunk_id: NonEmptyString
    scenes: tuple[BroadScene, ...]
    speech_presence_ranges: tuple[TimeRange, ...]
    candidates: tuple[BroadCandidate, ...]


class CandidateRefinementResponse(TimeRange):
    """Strict detailed candidate semantics bounded to one requested interval."""

    schema_version: Literal["candidate-v1"]
    chunk_id: NonEmptyString
    candidate_id: NonEmptyString
    action_completeness: float = Field(ge=0, le=1, allow_inf_nan=False)
    visual_composition: float = Field(ge=0, le=1, allow_inf_nan=False)
    novelty: float = Field(ge=0, le=1, allow_inf_nan=False)
    semantic_importance: float = Field(ge=0, le=1, allow_inf_nan=False)
    duplicate_similarity: float = Field(ge=0, le=1, allow_inf_nan=False)
    vertical_subject_priority: NonEmptyString
    crop_intent: NonEmptyString
    adjacent_scene_compatibility: float = Field(ge=0, le=1, allow_inf_nan=False)
    speech_meaning_summary: NonEmptyString | None = None
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


class ProviderResult[ResponseT](AnalysisModel):
    """Validated provider response and safe usage metadata."""

    response: ResponseT
    usage: ProviderUsage


class AnalysisProvider(Protocol):
    """Provider-neutral static video analysis interface."""

    def upload(self, manifest: ProxyManifest) -> UploadedFile:
        """Upload one validated generated proxy."""
        ...

    def broad_scan(
        self,
        upload: UploadedFile,
        chunk: AnalysisChunk,
        *,
        prompt_version: str,
    ) -> ProviderResult[BroadScanResponse]:
        """Analyze one broad proxy interval."""
        ...

    def refine_candidate(
        self,
        upload: UploadedFile,
        candidate: CandidateWindow,
        fps: int,
        *,
        prompt_version: str,
    ) -> ProviderResult[CandidateRefinementResponse]:
        """Analyze one candidate at approved sampling rate."""
        ...

    def delete_upload(self, upload: UploadedFile) -> None:
        """Delete one provider upload."""
        ...
