"""Fail-closed cloud analysis orchestration and exact coverage gates."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, cast

from pydantic import ValidationError

from video_editor.analysis.budget import reserve_all_uncached_broad_requests
from video_editor.analysis.models import (
    AnalysisChunkData,
    AnalysisModel,
    AnalysisProvider,
    AnalysisRequestContext,
    AnalysisResultData,
    BroadCandidate,
    BroadScanResponse,
    CandidateRefinementResponse,
    CandidateWindow,
    FiniteDecimal,
    LocalSegmentation,
    ProviderUsage,
    ProxyManifestData,
    RequestReservation,
    SourceRange,
)
from video_editor.analysis.proxy_chunks import (
    AuthorizedUpload,
    validate_upload_candidate,
)
from video_editor.config import PathSettings
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.persistence.database import JobStore

UploadValidator = Callable[[str, str, JobStore, PathSettings], AuthorizedUpload]


class AnalysisCacheIdentity(AnalysisModel):
    """Complete immutable identity for one analysis cache entry."""

    source_identity: str
    source_fingerprint: str
    proxy_digest: str
    mapping_version: str
    source_start: FiniteDecimal
    source_end: FiniteDecimal
    mode: Literal["broad", "candidate"]
    fps: FiniteDecimal
    media_width: int
    media_height: int
    provider: str
    model: str
    prompt_version: str
    response_schema_version: str
    implementation_version: str


class CoverageReport(AnalysisModel):
    """Exact broad-analysis coverage over usable source ranges."""

    expected_seconds: FiniteDecimal
    covered_seconds: FiniteDecimal
    ratio: FiniteDecimal
    missing: tuple[SourceRange, ...]
    complete: bool


@dataclass(frozen=True)
class BroadAnalysisRequest:
    """One registered broad chunk and its conservative reservation maximum."""

    manifest_id: str
    chunk_id: str
    cache_identity: AnalysisCacheIdentity
    maximum_cost_usd: Decimal
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class CandidateAnalysisRequest:
    """One bounded candidate and its conservative reservation maximum."""

    manifest_id: str
    candidate: CandidateWindow
    cache_identity: AnalysisCacheIdentity
    maximum_cost_usd: Decimal
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class AnalysisOutcome:
    """Gate result safe for downstream ranking, planning, and rendering."""

    state: Literal["complete", "analysis_incomplete", "budget_exhausted"]
    coverage: CoverageReport
    candidates: tuple[BroadCandidate | CandidateRefinementResponse, ...] = ()


@dataclass
class _ProviderChunk:
    chunk_id: str
    proxy_start: Decimal
    proxy_end: Decimal


def _canonical_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def analysis_cache_key(identity: AnalysisCacheIdentity) -> str:
    """Hash canonical JSON for every analysis-affecting identity field."""
    payload = identity.model_dump(mode="json")
    payload["source_start"] = _canonical_decimal(identity.source_start)
    payload["source_end"] = _canonical_decimal(identity.source_end)
    payload["fps"] = _canonical_decimal(identity.fps)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _group_ranges(
    ranges: Sequence[SourceRange],
) -> dict[str, list[tuple[Decimal, Decimal]]]:
    grouped: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for item in ranges:
        grouped.setdefault(item.source_id, []).append((item.start, item.end))
    return grouped


def _merge(
    intervals: Sequence[tuple[Decimal, Decimal]],
) -> list[tuple[Decimal, Decimal]]:
    merged: list[tuple[Decimal, Decimal]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _subtract(
    intervals: Sequence[tuple[Decimal, Decimal]],
    exclusions: Sequence[tuple[Decimal, Decimal]],
) -> list[tuple[Decimal, Decimal]]:
    remaining = list(intervals)
    for excluded_start, excluded_end in _merge(exclusions):
        updated: list[tuple[Decimal, Decimal]] = []
        for start, end in remaining:
            if excluded_end <= start or excluded_start >= end:
                updated.append((start, end))
                continue
            if start < excluded_start:
                updated.append((start, excluded_start))
            if excluded_end < end:
                updated.append((excluded_end, end))
        remaining = updated
    return remaining


def _intersect(
    left: Sequence[tuple[Decimal, Decimal]],
    right: Sequence[tuple[Decimal, Decimal]],
) -> list[tuple[Decimal, Decimal]]:
    intersections = [
        (max(left_start, right_start), min(left_end, right_end))
        for left_start, left_end in left
        for right_start, right_end in right
        if max(left_start, right_start) < min(left_end, right_end)
    ]
    return _merge(intersections)


def _seconds(grouped: dict[str, list[tuple[Decimal, Decimal]]]) -> Decimal:
    return sum(
        (end - start for intervals in grouped.values() for start, end in intervals),
        start=Decimal(0),
    )


def coverage_report(
    expected: Sequence[SourceRange],
    validated: Sequence[SourceRange],
    exclusions: Sequence[SourceRange] = (),
    *,
    persisted_inspection_exclusions: Sequence[SourceRange] = (),
) -> CoverageReport:
    """Calculate exact union coverage after authorized inspection exclusions."""
    expected_grouped = {
        source_id: _merge(intervals)
        for source_id, intervals in _group_ranges(expected).items()
    }
    persisted = set(persisted_inspection_exclusions)
    authorized = [item for item in exclusions if item in persisted]
    exclusions_grouped = {
        source_id: _merge(intervals)
        for source_id, intervals in _group_ranges(authorized).items()
    }
    usable = {
        source_id: _subtract(intervals, exclusions_grouped.get(source_id, ()))
        for source_id, intervals in expected_grouped.items()
    }
    validated_grouped = {
        source_id: _merge(intervals)
        for source_id, intervals in _group_ranges(validated).items()
    }
    covered = {
        source_id: _intersect(intervals, validated_grouped.get(source_id, ()))
        for source_id, intervals in usable.items()
    }
    missing_grouped = {
        source_id: _subtract(intervals, covered.get(source_id, ()))
        for source_id, intervals in usable.items()
    }
    expected_seconds = _seconds(usable)
    covered_seconds = _seconds(covered)
    missing = tuple(
        SourceRange(source_id=source_id, start=start, end=end)
        for source_id in sorted(missing_grouped)
        for start, end in missing_grouped[source_id]
    )
    ratio = Decimal(1) if expected_seconds == 0 else covered_seconds / expected_seconds
    return CoverageReport(
        expected_seconds=expected_seconds,
        covered_seconds=covered_seconds,
        ratio=ratio,
        missing=missing,
        complete=not missing and covered_seconds == expected_seconds,
    )


def _empty_coverage(expected_ranges: Sequence[SourceRange]) -> CoverageReport:
    return coverage_report(expected_ranges, ())


def _reservation(
    job_id: str,
    request_id: str,
    identity: AnalysisCacheIdentity,
    maximum_cost_usd: Decimal,
) -> RequestReservation:
    return RequestReservation(
        request_id=request_id,
        job_id=job_id,
        cache_key=analysis_cache_key(identity),
        mode=identity.mode,
        maximum_cost_usd=maximum_cost_usd,
    )


def _load_chunk(
    store: JobStore,
    job_id: str,
    chunk_id: str,
) -> tuple[dict[str, object], AnalysisChunkData]:
    record = store.get_analysis_chunk_for_job(job_id, chunk_id)
    if record is None:
        raise VideoEditorError(
            ErrorCategory.ANALYSIS,
            "analysis chunk is missing",
            code="analysis_incomplete",
        )
    try:
        chunk = AnalysisChunkData.model_validate(record["data"])
    except ValidationError as exc:
        raise VideoEditorError(
            ErrorCategory.ANALYSIS,
            "analysis chunk is invalid",
            code="analysis_incomplete",
        ) from exc
    return record, chunk


def _load_manifest(
    store: JobStore,
    job_id: str,
    manifest_id: str,
) -> ProxyManifestData:
    record = store.get_proxy_manifest_for_job(job_id, manifest_id)
    if record is None:
        raise VideoEditorError(
            ErrorCategory.ANALYSIS,
            "proxy manifest is missing",
            code="analysis_incomplete",
        )
    try:
        return ProxyManifestData.model_validate(record["data"])
    except ValidationError as exc:
        raise VideoEditorError(
            ErrorCategory.ANALYSIS,
            "proxy manifest is invalid",
            code="analysis_incomplete",
        ) from exc


def _validate_identity(
    identity: AnalysisCacheIdentity,
    manifest: ProxyManifestData,
    chunk: AnalysisChunkData,
    expected_mode: Literal["broad", "candidate"],
) -> None:
    if (
        identity.mode != expected_mode
        or identity.source_identity != manifest.source_identity
        or identity.source_fingerprint != manifest.source_fingerprint
        or identity.proxy_digest != manifest.file_digest_sha256
        or identity.mapping_version != manifest.mapping_version
        or identity.source_start != chunk.source_start
        or identity.source_end != chunk.source_end
        or identity.media_width != manifest.video_width
        or identity.media_height != manifest.video_height
    ):
        raise VideoEditorError(
            ErrorCategory.ANALYSIS,
            "analysis cache identity does not match persisted chunk",
            code="analysis_incomplete",
        )


def _metadata(
    request_id: str,
    identity: AnalysisCacheIdentity,
    usage: ProviderUsage,
    normalized_record_ids: tuple[str, ...],
) -> AnalysisResultData:
    return AnalysisResultData(
        schema_version=1,
        provider=identity.provider,
        model=identity.model,
        request_id=request_id,
        prompt_version=identity.prompt_version,
        response_schema_version=identity.response_schema_version,
        implementation_version=identity.implementation_version,
        token_count=usage.total_tokens,
        request_token_count=usage.prompt_tokens,
        output_token_count=usage.output_tokens,
        normalized_record_ids=normalized_record_ids,
    )


def _settle_usage(store: JobStore, request_id: str, usage: ProviderUsage) -> bool:
    if usage.reservation_id != request_id:
        store.mark_request_billing_unknown(request_id)
        return False
    if usage.has_unknown_billing or usage.actual_cost_usd is None:
        store.mark_request_billing_unknown(request_id)
        return False
    store.settle_request(request_id, usage.actual_cost_usd)
    return True


def _provider_error_usage(error: VideoEditorError) -> ProviderUsage | None:
    raw = error.safe_details.get("usage")
    if raw is None:
        return None
    try:
        return ProviderUsage.model_validate_json(raw)
    except ValidationError:
        return None


def _record_provider_failure(
    store: JobStore,
    request_id: str,
    error: VideoEditorError,
) -> None:
    usage = _provider_error_usage(error)
    if usage is not None:
        _settle_usage(store, request_id, usage)
    elif error.code in {
        "provider_authentication",
        "provider_invalid_request",
        "provider_invalid_upload_stream",
        "provider_file_not_found",
        "provider_file_reupload_required",
    }:
        store.release_confirmed_nonbillable(request_id)
    else:
        store.mark_request_billing_unknown(request_id)


def _persist_broad_result(
    store: JobStore,
    job_id: str,
    request: BroadAnalysisRequest,
    response: BroadScanResponse,
    usage: ProviderUsage,
) -> None:
    record_ids = tuple(candidate.candidate_id for candidate in response.candidates)
    store.save_analysis_result(
        result_id=str(uuid.uuid4()),
        job_id=job_id,
        cache_key=analysis_cache_key(request.cache_identity),
        chunk_id=request.chunk_id,
        mode="broad",
        data=_metadata(request.request_id, request.cache_identity, usage, record_ids),
        validated=True,
        normalized=response.model_dump(mode="json"),
    )


def _cached_broad(
    store: JobStore,
    job_id: str,
    request: BroadAnalysisRequest,
) -> BroadScanResponse | None:
    cached = store.find_analysis_result(
        job_id, analysis_cache_key(request.cache_identity)
    )
    if cached is None or not cached["validated"] or cached["mode"] != "broad":
        return None
    try:
        normalized = cached.get("normalized")
        if normalized is None:
            return None
        return BroadScanResponse.model_validate(normalized)
    except ValidationError:
        return None


def _broad_coverage_range(store: JobStore, job_id: str, chunk_id: str) -> SourceRange:
    record, _ = _load_chunk(store, job_id, chunk_id)
    return SourceRange(
        source_id=cast(str, record["source_id"]),
        start=cast(Decimal, record["source_start"]),
        end=cast(Decimal, record["source_end"]),
    )


def run_broad_analysis(
    *,
    store: JobStore,
    paths: PathSettings,
    provider: AnalysisProvider,
    job_id: str,
    expected_ranges: Sequence[SourceRange],
    requests: Sequence[BroadAnalysisRequest],
    exclusions: Sequence[SourceRange] = (),
    persisted_inspection_exclusions: Sequence[SourceRange] = (),
    upload_validator: UploadValidator = validate_upload_candidate,
) -> AnalysisOutcome:
    """Reserve all uncached broad work, dispatch safely, and enforce full coverage."""
    validated_ranges: list[SourceRange] = []
    candidates: list[BroadCandidate] = []
    pending: list[BroadAnalysisRequest] = []
    for request in requests:
        cached = _cached_broad(store, job_id, request)
        if cached is None:
            pending.append(request)
            continue
        validated_ranges.append(_broad_coverage_range(store, job_id, request.chunk_id))
        candidates.extend(cached.candidates)

    reservations = tuple(
        _reservation(
            job_id,
            request.request_id,
            request.cache_identity,
            request.maximum_cost_usd,
        )
        for request in pending
    )
    try:
        reserve_all_uncached_broad_requests(store, job_id, reservations)
    except VideoEditorError as error:
        if error.code != "budget_exhausted":
            raise
        return AnalysisOutcome(
            state="budget_exhausted",
            coverage=coverage_report(
                expected_ranges,
                validated_ranges,
                exclusions,
                persisted_inspection_exclusions=persisted_inspection_exclusions,
            ),
        )
    except (KeyError, ValueError):
        return AnalysisOutcome(
            state="analysis_incomplete",
            coverage=coverage_report(
                expected_ranges,
                validated_ranges,
                exclusions,
                persisted_inspection_exclusions=persisted_inspection_exclusions,
            ),
        )

    for request, reservation in zip(pending, reservations, strict=True):
        upload = None
        provider_dispatched = False
        try:
            store.verify_request_dispatchable(reservation)
            manifest = _load_manifest(store, job_id, request.manifest_id)
            _, chunk = _load_chunk(store, job_id, request.chunk_id)
            _validate_identity(request.cache_identity, manifest, chunk, "broad")
            authorization = upload_validator(request.manifest_id, job_id, store, paths)
            store.mark_request_dispatched(request.request_id)
            provider_dispatched = True
            upload = provider.upload(authorization)
            result = provider.broad_scan(
                upload,
                _ProviderChunk(
                    chunk_id=request.chunk_id,
                    proxy_start=chunk.proxy_start,
                    proxy_end=chunk.proxy_end,
                ),
                AnalysisRequestContext(
                    reservation_id=request.request_id,
                    job_id=job_id,
                    manifest_id=request.manifest_id,
                    mode="broad",
                    chunk_id=request.chunk_id,
                ),
                prompt_version=request.cache_identity.prompt_version,
            )
            if result.response.chunk_id != request.chunk_id:
                raise VideoEditorError(
                    ErrorCategory.ANALYSIS,
                    "provider broad response identity mismatch",
                    code="provider_response_invalid",
                )
            _persist_broad_result(store, job_id, request, result.response, result.usage)
            _settle_usage(store, request.request_id, result.usage)
            validated_ranges.append(
                _broad_coverage_range(store, job_id, request.chunk_id)
            )
            candidates.extend(result.response.candidates)
        except VideoEditorError as error:
            if provider_dispatched:
                _record_provider_failure(store, request.request_id, error)
            else:
                store.release_confirmed_nonbillable(request.request_id)
            break
        except (KeyError, ValueError, ValidationError):
            break
        finally:
            if upload is not None:
                provider.delete_upload(upload)

    report = coverage_report(
        expected_ranges,
        validated_ranges,
        exclusions,
        persisted_inspection_exclusions=persisted_inspection_exclusions,
    )
    return AnalysisOutcome(
        state="complete" if report.complete else "analysis_incomplete",
        coverage=report,
        candidates=tuple(candidates) if report.complete else (),
    )


def _candidate_fps(
    request: CandidateAnalysisRequest,
    segmentations: Sequence[LocalSegmentation],
) -> int:
    scores = [
        evidence.score
        for segmentation in segmentations
        if segmentation.source_identity == request.cache_identity.source_identity
        for evidence in segmentation.motion
        if evidence.source_range.start < request.cache_identity.source_end
        and evidence.source_range.end > request.cache_identity.source_start
    ]
    score = max(scores, default=0.0)
    if score < 0.35:
        return 2
    if score < 0.75:
        return 3
    return 5


def _candidate_identity(
    request: CandidateAnalysisRequest,
    fps: int,
) -> AnalysisCacheIdentity:
    return request.cache_identity.model_copy(update={"fps": Decimal(fps)})


def _persist_candidate_result(
    store: JobStore,
    job_id: str,
    request: CandidateAnalysisRequest,
    identity: AnalysisCacheIdentity,
    response: CandidateRefinementResponse,
    usage: ProviderUsage,
) -> None:
    store.save_analysis_result(
        result_id=str(uuid.uuid4()),
        job_id=job_id,
        cache_key=analysis_cache_key(identity),
        chunk_id=request.candidate.chunk_id,
        mode="candidate",
        data=_metadata(
            request.request_id,
            identity,
            usage,
            (response.candidate_id,),
        ),
        validated=True,
        normalized=response.model_dump(mode="json"),
    )


def _cached_candidate(
    store: JobStore,
    job_id: str,
    identity: AnalysisCacheIdentity,
    request: CandidateAnalysisRequest,
) -> CandidateRefinementResponse | None:
    cached = store.find_analysis_result(job_id, analysis_cache_key(identity))
    if cached is None or not cached["validated"] or cached["mode"] != "candidate":
        return None
    try:
        normalized = cached.get("normalized")
        if normalized is None:
            return None
        response = CandidateRefinementResponse.model_validate(normalized)
    except ValidationError:
        return None
    if (
        response.chunk_id != request.candidate.chunk_id
        or response.candidate_id != request.candidate.candidate_id
        or response.start != request.candidate.start
        or response.end != request.candidate.end
    ):
        return None
    return response


def run_candidate_refinement(
    *,
    store: JobStore,
    paths: PathSettings,
    provider: AnalysisProvider,
    job_id: str,
    broad_outcome: AnalysisOutcome,
    requests: Sequence[CandidateAnalysisRequest],
    segmentations: Sequence[LocalSegmentation],
    upload_validator: UploadValidator = validate_upload_candidate,
) -> AnalysisOutcome:
    """Refine bounded candidates only after exact broad coverage is complete."""
    if broad_outcome.state != "complete" or not broad_outcome.coverage.complete:
        return AnalysisOutcome(
            state="analysis_incomplete",
            coverage=broad_outcome.coverage,
        )

    responses: list[CandidateRefinementResponse] = []
    for request in requests:
        try:
            _, registered_chunk = _load_chunk(store, job_id, request.candidate.chunk_id)
        except VideoEditorError:
            return AnalysisOutcome(
                state="analysis_incomplete",
                coverage=broad_outcome.coverage,
            )
        if (
            request.candidate.start < registered_chunk.proxy_start
            or request.candidate.end > registered_chunk.proxy_end
        ):
            return AnalysisOutcome(
                state="analysis_incomplete",
                coverage=broad_outcome.coverage,
            )
        fps = _candidate_fps(request, segmentations)
        identity = _candidate_identity(request, fps)
        cached = _cached_candidate(store, job_id, identity, request)
        if cached is not None:
            responses.append(cached)
            continue
        reservation = _reservation(
            job_id,
            request.request_id,
            identity,
            request.maximum_cost_usd,
        )
        try:
            reserved = store.reserve_request(reservation)
        except VideoEditorError as error:
            if error.code != "budget_exhausted":
                raise
            return AnalysisOutcome(
                state="budget_exhausted",
                coverage=broad_outcome.coverage,
            )
        if reserved is None:
            continue

        upload = None
        provider_dispatched = False
        try:
            store.verify_request_dispatchable(reservation)
            manifest = _load_manifest(store, job_id, request.manifest_id)
            _, chunk = _load_chunk(store, job_id, request.candidate.chunk_id)
            _validate_identity(identity, manifest, chunk, "candidate")
            authorization = upload_validator(request.manifest_id, job_id, store, paths)
            store.mark_request_dispatched(request.request_id)
            provider_dispatched = True
            upload = provider.upload(authorization)
            result = provider.refine_candidate(
                upload,
                request.candidate,
                AnalysisRequestContext(
                    reservation_id=request.request_id,
                    job_id=job_id,
                    manifest_id=request.manifest_id,
                    mode="candidate",
                    chunk_id=request.candidate.chunk_id,
                    candidate_id=request.candidate.candidate_id,
                ),
                fps,
                prompt_version=identity.prompt_version,
            )
            response = result.response
            if (
                response.chunk_id != request.candidate.chunk_id
                or response.candidate_id != request.candidate.candidate_id
                or response.start != request.candidate.start
                or response.end != request.candidate.end
            ):
                raise VideoEditorError(
                    ErrorCategory.ANALYSIS,
                    "candidate response is outside requested identity or interval",
                    code="provider_response_invalid",
                )
            _persist_candidate_result(
                store,
                job_id,
                request,
                identity,
                response,
                result.usage,
            )
            _settle_usage(store, request.request_id, result.usage)
            responses.append(response)
        except VideoEditorError as error:
            if provider_dispatched:
                _record_provider_failure(store, request.request_id, error)
            else:
                store.release_confirmed_nonbillable(request.request_id)
            return AnalysisOutcome(
                state="analysis_incomplete",
                coverage=broad_outcome.coverage,
            )
        finally:
            if upload is not None:
                provider.delete_upload(upload)

    return AnalysisOutcome(
        state="complete",
        coverage=broad_outcome.coverage,
        candidates=tuple(responses),
    )
