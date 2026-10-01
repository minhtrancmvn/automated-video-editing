"""Strict provider-neutral analysis persistence models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)

if TYPE_CHECKING:
    from video_editor.analysis.proxy_chunks import AuthorizedUpload


def _strict_json_decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004 - Pydantic must wrap this as ValidationError
            "provider timestamp must be a JSON string"
        )
    try:
        decimal = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("provider timestamp must be decimal text") from exc
    if not decimal.is_finite():
        raise ValueError("provider timestamp must be finite")
    return decimal


FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
ProviderDecimal = Annotated[
    Decimal,
    BeforeValidator(_strict_json_decimal),
    WithJsonSchema({"type": "string"}),
]
StrictScore = Annotated[StrictFloat, Field(ge=0, le=1, allow_inf_nan=False)]
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


class PairwiseSimilarity(AnalysisModel):
    """Supplied visual or semantic similarity to another candidate."""

    other_candidate_id: NonEmptyString
    visual: FiniteDecimal | None = None
    semantic: FiniteDecimal | None = None
    evidence_ids: tuple[NonEmptyString, ...] = ()

    @model_validator(mode="after")
    def validate_measurement(self) -> Self:
        """Require at least one supplied similarity measurement."""
        if self.visual is None and self.semantic is None:
            raise ValueError("visual or semantic similarity is required")
        return self


class RankingCandidateEvidence(AnalysisModel):
    """Validated normalized evidence consumed by deterministic ranking."""

    candidate_id: NonEmptyString
    source_id: NonEmptyString
    source_start: FiniteDecimal = Field(ge=0)
    source_end: FiniteDecimal = Field(gt=0)
    category: NonEmptyString
    event_id: NonEmptyString | None = None
    exact_event_id: NonEmptyString | None = None
    location_id: NonEmptyString | None = None
    confidence: FiniteDecimal
    action: FiniteDecimal
    scenic: FiniteDecimal
    human: FiniteDecimal
    story: FiniteDecimal
    technical: FiniteDecimal
    novelty: FiniteDecimal
    completeness: FiniteDecimal
    long_story: FiniteDecimal
    short: FiniteDecimal
    vertical: FiniteDecimal
    blur_exposure: FiniteDecimal
    shake_obstruction: FiniteDecimal
    incomplete: FiniteDecimal
    weak_boundary: FiniteDecimal
    repetition: FiniteDecimal
    overlap: FiniteDecimal
    evidence_ids: tuple[NonEmptyString, ...]
    technical_failure_codes: tuple[NonEmptyString, ...] = ()
    similarities: tuple[PairwiseSimilarity, ...] = ()

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        """Reject empty or reversed source intervals."""
        if self.source_end <= self.source_start:
            raise ValueError("candidate source end must be greater than start")
        return self


class UploadedFile(AnalysisModel):
    """Provider upload identity linked to its authorization manifest ID."""

    name: NonEmptyString
    uri: NonEmptyString
    mime_type: NonEmptyString
    state: NonEmptyString
    expiration_time: datetime | None = None
    manifest_id: NonEmptyString


class RetryPolicy(AnalysisModel):
    """Bounded provider retry policy."""

    max_attempts: int = Field(default=3, ge=1)
    base_delay_seconds: FiniteDecimal = Field(default=Decimal(1), ge=0)


class AnalysisRequestContext(AnalysisModel):
    """Persisted reservation identity required for provider generation."""

    reservation_id: NonEmptyString
    job_id: NonEmptyString
    manifest_id: NonEmptyString
    mode: Literal["broad", "candidate"]
    chunk_id: NonEmptyString
    candidate_id: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_mode_identity(self) -> Self:
        """Require candidate identity exactly for candidate requests."""
        if (self.mode == "candidate") != (self.candidate_id is not None):
            raise ValueError("candidate identity must match analysis mode")
        return self


class ProviderAttemptUsage(AnalysisModel):
    """Usage outcome from one provider generation call."""

    request_id: NonEmptyString
    status: Literal["succeeded", "failed_unknown_billing"]
    prompt_tokens: int | None = Field(default=None, ge=0)
    media_input_tokens: int | None = Field(default=None, ge=0)
    text_input_tokens: int | None = Field(default=None, ge=0)
    candidates_tokens: int | None = Field(default=None, ge=0)
    thoughts_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class ProviderUsage(AnalysisModel):
    """Known aggregate usage plus explicit unknown-billing state."""

    reservation_id: NonEmptyString
    attempts: tuple[ProviderAttemptUsage, ...] = Field(min_length=1)
    request_ids: tuple[NonEmptyString, ...]
    prompt_tokens: int = Field(ge=0)
    media_input_tokens: int = Field(ge=0)
    text_input_tokens: int = Field(ge=0)
    candidates_tokens: int = Field(ge=0)
    thoughts_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    has_unknown_billing: bool
    actual_cost_usd: FiniteDecimal | None = Field(default=None, ge=0)

    def safe_payload(self) -> dict[str, object]:
        """Return persistence-safe aggregate usage without secret data."""
        return self.model_dump(mode="json")


class TimeRange(AnalysisModel):
    """Strict provider-returned time interval with string timestamps."""

    start: ProviderDecimal = Field(ge=0)
    end: ProviderDecimal = Field(gt=0)

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
    scenic_interest: StrictScore
    human_interaction: StrictScore
    story_milestone: StrictBool
    technical_problems: tuple[NonEmptyString, ...]
    vertical_suitability: StrictScore
    confidence: StrictScore


class BroadCandidate(TimeRange):
    """Normalized broad-scan highlight candidate."""

    candidate_id: NonEmptyString
    category: NonEmptyString
    reason: NonEmptyString
    confidence: StrictScore


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
    action_completeness: StrictScore
    visual_composition: StrictScore
    novelty: StrictScore
    semantic_importance: StrictScore
    duplicate_similarity: StrictScore
    vertical_subject_priority: NonEmptyString
    crop_intent: NonEmptyString
    adjacent_scene_compatibility: StrictScore
    speech_meaning_summary: NonEmptyString | None = None
    confidence: StrictScore


class ProviderResult[ResponseT](AnalysisModel):
    """Validated provider response and safe usage metadata."""

    response: ResponseT
    usage: ProviderUsage


class AnalysisProvider(Protocol):
    """Provider-neutral static video analysis interface."""

    def upload(self, authorization: AuthorizedUpload) -> UploadedFile:
        """Upload exact bytes held by one validated authorization."""
        ...

    def broad_scan(
        self,
        upload: UploadedFile,
        chunk: AnalysisChunk,
        request: AnalysisRequestContext,
        *,
        prompt_version: str,
    ) -> ProviderResult[BroadScanResponse]:
        """Analyze one broad proxy interval under a persisted reservation."""
        ...

    def refine_candidate(
        self,
        upload: UploadedFile,
        candidate: CandidateWindow,
        request: AnalysisRequestContext,
        fps: int,
        *,
        prompt_version: str,
    ) -> ProviderResult[CandidateRefinementResponse]:
        """Analyze one candidate under a persisted reservation."""
        ...

    def delete_upload(self, upload: UploadedFile) -> None:
        """Delete one provider upload."""
        ...
