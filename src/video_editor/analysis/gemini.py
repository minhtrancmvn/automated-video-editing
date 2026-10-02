"""Google Gemini adapter for schema-constrained video analysis."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from io import IOBase
from typing import Any, TypeVar

from google import genai
from google.genai import types
from pydantic import ValidationError

from video_editor.analysis.models import (
    AnalysisChunk,
    AnalysisProvider,
    AnalysisRequestContext,
    BroadScanResponse,
    CandidateRefinementResponse,
    CandidateWindow,
    ProviderAttemptUsage,
    ProviderResult,
    ProviderUsage,
    RetryPolicy,
    UploadedFile,
)
from video_editor.analysis.pricing import ModelPricing, maximum_request_cost
from video_editor.analysis.proxy_chunks import AuthorizedUpload
from video_editor.errors import ErrorCategory, VideoEditorError

MODEL_ID = "gemini-2.5-flash"
_BROAD_FPS = 0.5
_RESPONSE_MIME_TYPE = "application/json"
_OUTPUT_TOKEN_MAXIMUM = 8192
_VIDEO_TOKENS_PER_FRAME = 258
_AUDIO_TOKENS_PER_SECOND = 32
_METADATA_TOKENS_PER_SECOND = 64
_PROMPT_OVERHEAD_TOKENS = 1024
_MICRO_USD = Decimal(1_000_000)
_ResponseT = TypeVar("_ResponseT", BroadScanResponse, CandidateRefinementResponse)
_ValueT = TypeVar("_ValueT")


class GeminiAdapter:
    """Adapt google-genai Files and Models APIs to provider-neutral records."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        api_key: str | None = None,
        retry_policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        maximum_request_cost_usd: Decimal | None = None,
        pricing: ModelPricing | None = None,
    ) -> None:
        """Create adapter with injected client or runtime-only API key."""
        if client is None and not api_key:
            raise VideoEditorError(
                ErrorCategory.CONFIGURATION,
                "Gemini API key is required",
                code="provider_configuration",
            )
        self._client = client if client is not None else genai.Client(api_key=api_key)
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleeper = sleeper
        self._maximum_request_cost_usd = maximum_request_cost_usd
        self._pricing = pricing

    def __repr__(self) -> str:
        """Return safe adapter representation without client or key data."""
        return f"{type(self).__name__}(model={MODEL_ID!r})"

    @classmethod
    def from_environment(
        cls,
        *,
        maximum_request_cost_usd: Decimal | None = None,
        pricing: ModelPricing | None = None,
    ) -> GeminiAdapter:
        """Create adapter from runtime environment without retaining its key."""
        return cls(
            api_key=os.getenv("GEMINI_API_KEY"),
            maximum_request_cost_usd=maximum_request_cost_usd,
            pricing=pricing,
        )

    @staticmethod
    def _estimate_request_maximum(
        chunk: AnalysisChunk,
        pricing: ModelPricing,
        *,
        fps: Decimal,
        attempts: int,
        response_model: type[BroadScanResponse | CandidateRefinementResponse],
    ) -> Decimal:
        """Reserve static-video media, metadata allowance, and capped output."""
        duration = chunk.proxy_end - chunk.proxy_start
        if duration <= 0 or not duration.is_finite():
            raise ValueError("chunk duration must be finite and positive")
        seconds = int(duration.to_integral_value(rounding=ROUND_CEILING))
        frames = int((fps * duration).to_integral_value(rounding=ROUND_CEILING))
        media_tokens = frames * _VIDEO_TOKENS_PER_FRAME + seconds * (
            _METADATA_TOKENS_PER_SECOND
        )
        audio_tokens = seconds * _AUDIO_TOKENS_PER_SECOND
        schema_bytes = len(json.dumps(response_model.model_json_schema()).encode())
        text_tokens = _PROMPT_OVERHEAD_TOKENS + schema_bytes
        per_attempt = maximum_request_cost(
            pricing,
            media_tokens,
            text_tokens,
            _OUTPUT_TOKEN_MAXIMUM,
            audio_tokens=audio_tokens,
        )
        maximum = per_attempt * attempts
        return (maximum * _MICRO_USD).to_integral_value(
            rounding=ROUND_CEILING
        ) / _MICRO_USD

    @staticmethod
    def estimate_broad_request_maximum(
        authorization: AuthorizedUpload,
        chunk: AnalysisChunk,
        pricing: ModelPricing,
        *,
        prompt_version: str = "broad-v1",
    ) -> Decimal:
        """Estimate default-policy broad cost for live preflight without dispatch."""
        if prompt_version != "broad-v1" or not authorization.manifest_id:
            raise ValueError("broad preflight requires a supported prompt and manifest")
        return GeminiAdapter._estimate_request_maximum(
            chunk,
            pricing,
            fps=Decimal(str(_BROAD_FPS)),
            attempts=RetryPolicy().max_attempts,
            response_model=BroadScanResponse,
        )

    def maximum_request_cost(
        self,
        manifest_id: str,
        chunk: AnalysisChunk,
        *,
        prompt_version: str,
    ) -> Decimal:
        """Estimate all generation attempts at the mode's maximum sampling rate."""
        if self._pricing is None:
            raise VideoEditorError(
                ErrorCategory.BUDGET,
                "Gemini pricing is required to bound request cost",
                code="provider_pricing_unavailable",
            )
        if not manifest_id or prompt_version not in {"broad-v1", "candidate-v2"}:
            raise VideoEditorError(
                ErrorCategory.BUDGET,
                "Unsupported request cost identity",
                code="provider_invalid_request",
            )
        fps = Decimal(str(_BROAD_FPS)) if prompt_version == "broad-v1" else Decimal(5)
        return self._estimate_request_maximum(
            chunk,
            self._pricing,
            fps=fps,
            attempts=self._retry_policy.max_attempts,
            response_model=(
                BroadScanResponse
                if prompt_version == "broad-v1"
                else CandidateRefinementResponse
            ),
        )

    def upload(self, authorization: AuthorizedUpload) -> UploadedFile:
        """Upload exact bytes from one validated upload authorization."""
        if not isinstance(authorization, AuthorizedUpload):
            raise TypeError("upload requires AuthorizedUpload")
        with authorization:
            stream = authorization.stream
            if not isinstance(stream, IOBase):
                raise TypeError("AuthorizedUpload stream must be an IOBase")
            try:
                if not stream.seekable():
                    raise OSError("stream is not seekable")
                start_offset = stream.tell()
                if (
                    stream.seek(start_offset) != start_offset
                    or stream.tell() != start_offset
                ):
                    raise OSError("stream did not preserve its authorized offset")
            except (OSError, ValueError) as exc:
                raise self._invalid_upload_stream() from exc

            def upload_attempt() -> Any:
                try:
                    position = stream.seek(start_offset)
                except (OSError, ValueError) as exc:
                    raise self._invalid_upload_stream() from exc
                if position != start_offset or stream.tell() != start_offset:
                    raise self._invalid_upload_stream()
                return self._client.files.upload(
                    file=stream,
                    config={"mime_type": "video/mp4"},
                )

            remote = self._retry(upload_attempt)
        return self._uploaded_file(remote, authorization.manifest_id)

    def wait_until_active(self, upload: UploadedFile) -> UploadedFile:
        """Poll file processing independently from individual SDK retry attempts."""
        current = upload
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                remote = self._retry(lambda: self._client.files.get(name=current.name))
            except VideoEditorError as error:
                if error.code == "provider_file_not_found":
                    raise self._reupload_required(current) from None
                raise
            refreshed = self._uploaded_file(remote, current.manifest_id)
            if self._is_expired(refreshed.expiration_time):
                raise self._reupload_required(current)
            if refreshed.state == "ACTIVE":
                return refreshed
            if refreshed.state == "FAILED":
                raise VideoEditorError(
                    ErrorCategory.PROVIDER,
                    "Gemini file processing failed",
                    code="provider_file_failed",
                )
            if attempt < self._retry_policy.max_attempts:
                self._sleep(attempt)
        raise VideoEditorError(
            ErrorCategory.PROVIDER,
            "Gemini file processing did not become active",
            code="provider_file_timeout",
        )

    def broad_scan(
        self,
        upload: UploadedFile,
        chunk: AnalysisChunk,
        request: AnalysisRequestContext,
        *,
        prompt_version: str,
    ) -> ProviderResult[BroadScanResponse]:
        """Analyze one whole proxy chunk at static 0.5 FPS."""
        return self._analyze(
            upload=upload,
            request=request,
            chunk_id=chunk.chunk_id,
            start=chunk.proxy_start,
            end=chunk.proxy_end,
            fps=_BROAD_FPS,
            prompt_version=prompt_version,
            response_model=BroadScanResponse,
        )

    def refine_candidate(
        self,
        upload: UploadedFile,
        candidate: CandidateWindow,
        request: AnalysisRequestContext,
        fps: int,
        *,
        prompt_version: str,
    ) -> ProviderResult[CandidateRefinementResponse]:
        """Analyze one candidate interval at approved 2, 3, or 5 FPS."""
        if fps not in {2, 3, 5}:
            raise VideoEditorError(
                ErrorCategory.PROVIDER,
                "Candidate FPS must be 2, 3, or 5",
                code="provider_invalid_request",
            )
        return self._analyze(
            upload=upload,
            request=request,
            chunk_id=candidate.chunk_id,
            candidate_id=candidate.candidate_id,
            start=candidate.start,
            end=candidate.end,
            fps=float(fps),
            prompt_version=prompt_version,
            response_model=CandidateRefinementResponse,
        )

    def delete_upload(self, upload: UploadedFile) -> None:
        """Delete provider upload by Files API name."""
        self._retry(lambda: self._client.files.delete(name=upload.name))

    def _analyze(
        self,
        *,
        upload: UploadedFile,
        request: AnalysisRequestContext,
        chunk_id: str,
        start: Decimal,
        end: Decimal,
        fps: float,
        prompt_version: str,
        response_model: type[_ResponseT],
        candidate_id: str | None = None,
    ) -> ProviderResult[_ResponseT]:
        self._validate_request_context(
            request,
            upload=upload,
            chunk_id=chunk_id,
            candidate_id=candidate_id,
        )
        prompt = self._prompt(
            prompt_version,
            request=request,
            chunk_id=chunk_id,
            candidate_id=candidate_id,
            start=start,
            end=end,
        )
        if len(prompt.encode()) > _PROMPT_OVERHEAD_TOKENS:
            raise VideoEditorError(
                ErrorCategory.PROVIDER,
                "Gemini prompt exceeds reserved text allowance",
                code="provider_invalid_request",
            )
        active = self.wait_until_active(upload)
        part = types.Part(
            file_data=types.FileData(file_uri=active.uri, mime_type=active.mime_type),
            video_metadata=types.VideoMetadata(
                start_offset=self._duration(start),
                end_offset=self._duration(end),
                fps=fps,
            ),
        )
        config = types.GenerateContentConfig(
            response_mime_type=_RESPONSE_MIME_TYPE,
            response_schema=response_model,
            max_output_tokens=_OUTPUT_TOKEN_MAXIMUM,
        )
        attempts: list[ProviderAttemptUsage] = []
        remaining_attempts = self._retry_policy.max_attempts
        repair_available = True
        while remaining_attempts > 0:
            response, generation_attempts, generation_calls, generation_error = (
                self._generate(
                    part,
                    prompt,
                    config,
                    max_attempts=remaining_attempts,
                )
            )
            attempts.extend(generation_attempts)
            remaining_attempts -= generation_calls
            if generation_error is not None:
                raise self._provider_failure(
                    generation_error,
                    reservation_id=request.reservation_id,
                    attempts=attempts,
                )
            if response is None:
                raise self._invalid_response()
            try:
                parsed = response_model.model_validate(response.parsed)
                self._validate_response(
                    parsed,
                    chunk_id=chunk_id,
                    candidate_id=candidate_id,
                    start=start,
                    end=end,
                )
            except (ValidationError, TypeError, ValueError, VideoEditorError):
                if repair_available and remaining_attempts > 0:
                    repair_available = False
                    continue
                raise self._invalid_response()
            return ProviderResult(
                response=parsed,
                usage=self._aggregate_usage(request.reservation_id, attempts),
            )
        raise self._invalid_response()

    def _generate(
        self,
        part: types.Part,
        prompt: str,
        config: types.GenerateContentConfig,
        *,
        max_attempts: int,
    ) -> tuple[Any | None, list[ProviderAttemptUsage], int, VideoEditorError | None]:
        """Generate within one total attempt budget and retain returned usage."""
        attempts: list[ProviderAttemptUsage] = []
        for attempt in range(1, max_attempts + 1):
            mapped: VideoEditorError | None = None
            response: Any | None = None
            try:
                response = self._client.models.generate_content(
                    model=MODEL_ID,
                    contents=[part, prompt],
                    config=config,
                )
            except Exception as error:  # noqa: BLE001 - SDK hierarchy is open-ended
                attempts.append(self._unknown_attempt(error))
                mapped = self._provider_error(error)
            if mapped is not None:
                if not self._is_retryable(mapped) or attempt == max_attempts:
                    return None, attempts, attempt, mapped
                self._sleep(attempt)
                continue
            attempts.append(self._attempt_usage(response))
            return response, attempts, attempt, None
        return (
            None,
            attempts,
            max_attempts,
            VideoEditorError(
                ErrorCategory.PROVIDER,
                "Gemini provider request failed",
                code="provider_unavailable",
            ),
        )

    def _retry(self, operation: Callable[[], _ValueT]) -> _ValueT:
        """Run one SDK operation with retries only for sanitized transient errors."""
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                return operation()
            except Exception as error:  # noqa: BLE001 - SDK error hierarchy is open-ended
                mapped = self._provider_error(error)
            if (
                not self._is_retryable(mapped)
                or attempt == self._retry_policy.max_attempts
            ):
                raise mapped
            self._sleep(attempt)
        raise VideoEditorError(
            ErrorCategory.PROVIDER,
            "Gemini provider request failed",
            code="provider_unavailable",
        )

    @staticmethod
    def _validate_request_context(
        request: AnalysisRequestContext,
        *,
        upload: UploadedFile,
        chunk_id: str,
        candidate_id: str | None,
    ) -> None:
        if (
            request.manifest_id != upload.manifest_id
            or request.chunk_id != chunk_id
            or request.candidate_id != candidate_id
            or request.mode != ("candidate" if candidate_id is not None else "broad")
        ):
            raise VideoEditorError(
                ErrorCategory.BUDGET,
                "Analysis request does not match its reservation identity",
                code="reservation_identity_mismatch",
            )

    @staticmethod
    def _validate_response(
        response: BroadScanResponse | CandidateRefinementResponse,
        *,
        chunk_id: str,
        candidate_id: str | None,
        start: Decimal,
        end: Decimal,
    ) -> None:
        if response.chunk_id != chunk_id:
            raise GeminiAdapter._invalid_response()
        intervals: tuple[tuple[Decimal, Decimal], ...]
        if isinstance(response, CandidateRefinementResponse):
            if response.candidate_id != candidate_id:
                raise GeminiAdapter._invalid_response()
            intervals = ((response.start, response.end),)
        else:
            intervals = tuple(
                (item.start, item.end)
                for item in (
                    *response.scenes,
                    *response.speech_presence_ranges,
                    *response.candidates,
                )
            )
        if any(
            item_start < start or item_end > end for item_start, item_end in intervals
        ):
            raise GeminiAdapter._invalid_response()

    @staticmethod
    def _attempt_usage(response: Any) -> ProviderAttemptUsage:
        metadata = getattr(response, "usage_metadata", None)
        request_id = str(getattr(response, "response_id", None) or "unknown")
        required_counts = (
            getattr(metadata, "prompt_token_count", None),
            getattr(metadata, "candidates_token_count", None),
            getattr(metadata, "thoughts_token_count", None),
            getattr(metadata, "total_token_count", None),
        )
        if metadata is None or any(count is None for count in required_counts):
            return ProviderAttemptUsage(
                request_id=request_id,
                status="failed_unknown_billing",
            )
        details = getattr(metadata, "prompt_tokens_details", None)
        prompt_tokens = int(getattr(metadata, "prompt_token_count", 0) or 0)
        if details is None and prompt_tokens:
            return ProviderAttemptUsage(
                request_id=request_id,
                status="failed_unknown_billing",
            )
        media_input_tokens = 0
        audio_input_tokens = 0
        text_input_tokens = 0
        for detail in details or ():
            modality = getattr(detail, "modality", None)
            modality_value = str(getattr(modality, "value", modality)).upper()
            count = getattr(detail, "token_count", None)
            if not isinstance(count, int) or count < 0:
                return ProviderAttemptUsage(
                    request_id=request_id,
                    status="failed_unknown_billing",
                )
            if modality_value in {"VIDEO", "IMAGE"}:
                media_input_tokens += count
            elif modality_value == "AUDIO":
                audio_input_tokens += count
            elif modality_value == "TEXT":
                text_input_tokens += count
            else:
                return ProviderAttemptUsage(
                    request_id=request_id,
                    status="failed_unknown_billing",
                )
        if media_input_tokens + audio_input_tokens + text_input_tokens != prompt_tokens:
            return ProviderAttemptUsage(
                request_id=request_id,
                status="failed_unknown_billing",
            )
        candidates_tokens = int(getattr(metadata, "candidates_token_count", 0) or 0)
        thoughts_tokens = int(getattr(metadata, "thoughts_token_count", 0) or 0)
        return ProviderAttemptUsage(
            request_id=request_id,
            status="succeeded",
            prompt_tokens=prompt_tokens,
            media_input_tokens=media_input_tokens,
            audio_input_tokens=audio_input_tokens,
            text_input_tokens=text_input_tokens,
            candidates_tokens=candidates_tokens,
            thoughts_tokens=thoughts_tokens,
            output_tokens=candidates_tokens + thoughts_tokens,
            total_tokens=int(getattr(metadata, "total_token_count", 0) or 0),
        )

    @staticmethod
    def _unknown_attempt(error: Exception) -> ProviderAttemptUsage:
        response = getattr(error, "response", None)
        request_id = "unknown"
        if response is not None:
            headers = getattr(response, "headers", None)
            if headers is not None:
                request_id = str(
                    headers.get("x-request-id")
                    or headers.get("x-goog-request-id")
                    or "unknown"
                )
        return ProviderAttemptUsage(
            request_id=request_id,
            status="failed_unknown_billing",
        )

    def _aggregate_usage(
        self, reservation_id: str, attempts: list[ProviderAttemptUsage]
    ) -> ProviderUsage:
        actual_cost: Decimal | None = None
        if self._pricing is not None and all(
            attempt.status == "succeeded" for attempt in attempts
        ):
            actual_cost = Decimal(0)
            for attempt in attempts:
                media_tokens = attempt.media_input_tokens
                audio_tokens = attempt.audio_input_tokens
                text_tokens = attempt.text_input_tokens
                output_tokens = attempt.output_tokens
                if (
                    media_tokens is None
                    or audio_tokens is None
                    or text_tokens is None
                    or output_tokens is None
                ):
                    actual_cost = None
                    break
                actual_cost += maximum_request_cost(
                    self._pricing,
                    media_tokens,
                    text_tokens,
                    output_tokens,
                    audio_tokens=audio_tokens,
                )
        settled_cost = (
            None
            if actual_cost is None
            else (actual_cost * _MICRO_USD).to_integral_value(rounding=ROUND_CEILING)
            / _MICRO_USD
        )
        return ProviderUsage(
            reservation_id=reservation_id,
            attempts=tuple(attempts),
            request_ids=tuple(attempt.request_id for attempt in attempts),
            prompt_tokens=sum(attempt.prompt_tokens or 0 for attempt in attempts),
            media_input_tokens=sum(
                attempt.media_input_tokens or 0 for attempt in attempts
            ),
            audio_input_tokens=sum(
                attempt.audio_input_tokens or 0 for attempt in attempts
            ),
            text_input_tokens=sum(
                attempt.text_input_tokens or 0 for attempt in attempts
            ),
            candidates_tokens=sum(
                attempt.candidates_tokens or 0 for attempt in attempts
            ),
            thoughts_tokens=sum(attempt.thoughts_tokens or 0 for attempt in attempts),
            output_tokens=sum(attempt.output_tokens or 0 for attempt in attempts),
            total_tokens=sum(attempt.total_tokens or 0 for attempt in attempts),
            has_unknown_billing=any(
                attempt.status == "failed_unknown_billing" for attempt in attempts
            ),
            actual_cost_usd=settled_cost,
        )

    @staticmethod
    def _prompt(
        prompt_version: str,
        *,
        request: AnalysisRequestContext,
        chunk_id: str,
        candidate_id: str | None,
        start: Decimal,
        end: Decimal,
    ) -> str:
        interval = f"[{format(start, 'f')}, {format(end, 'f')}] seconds"
        if prompt_version == "broad-v1" and candidate_id is None:
            return "\n".join(
                (
                    "mode: broad",
                    "prompt_version: broad-v1",
                    f"reservation_id: {request.reservation_id}",
                    f"job_id: {request.job_id}",
                    f"chunk_id: {chunk_id}",
                    f"interval: {interval}",
                    "Return only JSON matching response schema.",
                    "Do not transcribe speech.",
                    "Do not infer speech meaning.",
                    "Do not identify any person.",
                )
            )
        if prompt_version == "candidate-v2" and candidate_id is not None:
            return "\n".join(
                (
                    "mode: candidate",
                    "prompt_version: candidate-v2",
                    f"reservation_id: {request.reservation_id}",
                    f"job_id: {request.job_id}",
                    f"chunk_id: {chunk_id}",
                    f"candidate_id: {candidate_id}",
                    f"interval: {interval}",
                    "Return only JSON matching response schema.",
                    "Speech meaning may be summarized only for this candidate interval.",
                    (
                        "Return timestamped normalized subject_boxes (time, x, y, w, h,"
                        " priority) inside the interval as tracking seeds only."
                    ),
                    "Do not identify any person.",
                )
            )
        raise VideoEditorError(
            ErrorCategory.PROVIDER,
            "Unsupported Gemini prompt version",
            code="provider_invalid_request",
        )

    def _provider_failure(
        self,
        error: VideoEditorError,
        *,
        reservation_id: str,
        attempts: list[ProviderAttemptUsage],
    ) -> VideoEditorError:
        if not attempts:
            return error
        usage = self._aggregate_usage(reservation_id, attempts)
        return VideoEditorError(
            error.category,
            str(error),
            code=error.code,
            safe_details={"usage": json.dumps(usage.safe_payload(), sort_keys=True)},
        )

    @staticmethod
    def _provider_error(error: Exception) -> VideoEditorError:
        status = getattr(error, "code", None)
        if status in {401, 403}:
            code, message = "provider_authentication", "Gemini authentication failed"
        elif status == 404:
            code, message = (
                "provider_file_not_found",
                "Gemini file is no longer available",
            )
        elif status is not None and 400 <= status < 500 and status != 429:
            code, message = "provider_invalid_request", "Gemini request was invalid"
        elif status == 429:
            code, message = "provider_rate_limited", "Gemini request was rate limited"
        elif status is not None and status >= 500:
            code, message = "provider_unavailable", "Gemini service is unavailable"
        elif isinstance(error, OSError):
            code, message = "provider_network", "Gemini network request failed"
        else:
            code, message = "provider_unavailable", "Gemini provider request failed"
        return VideoEditorError(ErrorCategory.PROVIDER, message, code=code)

    @staticmethod
    def _uploaded_file(remote: Any, manifest_id: str) -> UploadedFile:
        return UploadedFile(
            name=str(remote.name),
            uri=str(remote.uri),
            mime_type=str(remote.mime_type or "video/mp4"),
            state=GeminiAdapter._state_value(remote.state),
            expiration_time=getattr(remote, "expiration_time", None),
            manifest_id=manifest_id,
        )

    @staticmethod
    def _is_expired(expiration_time: datetime | None) -> bool:
        if expiration_time is None:
            return False
        if expiration_time.tzinfo is None or expiration_time.utcoffset() is None:
            return True
        return expiration_time <= datetime.now(UTC)

    @staticmethod
    def _reupload_required(upload: UploadedFile) -> VideoEditorError:
        return VideoEditorError(
            ErrorCategory.PROVIDER,
            "Gemini file must be uploaded again",
            code="provider_file_reupload_required",
            safe_details={"manifest_id": upload.manifest_id},
        )

    @staticmethod
    def _is_retryable(error: VideoEditorError) -> bool:
        return error.code in {
            "provider_rate_limited",
            "provider_unavailable",
            "provider_network",
        }

    def _sleep(self, attempt: int) -> None:
        delay = self._retry_policy.base_delay_seconds * (2 ** (attempt - 1))
        self._sleeper(float(delay))

    @staticmethod
    def _invalid_upload_stream() -> VideoEditorError:
        return VideoEditorError(
            ErrorCategory.PROVIDER,
            "Authorized upload stream cannot be safely rewound",
            code="provider_invalid_upload_stream",
        )

    @staticmethod
    def _invalid_response() -> VideoEditorError:
        return VideoEditorError(
            ErrorCategory.PROVIDER,
            "Gemini response has invalid chunk identity or timestamp data",
            code="provider_response_invalid",
        )

    @staticmethod
    def _duration(value: Decimal) -> str:
        return f"{format(value, 'f')}s"

    @staticmethod
    def _state_value(state: object) -> str:
        value = getattr(state, "value", state)
        return str(value).rsplit(".", 1)[-1]


__all__ = [
    "AnalysisProvider",
    "BroadScanResponse",
    "CandidateRefinementResponse",
    "CandidateWindow",
    "GeminiAdapter",
    "RetryPolicy",
]
