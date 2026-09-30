"""Google Gemini adapter for schema-constrained video analysis."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from io import IOBase
from typing import Any, TypeVar

from google import genai
from google.genai import types
from pydantic import ValidationError

from video_editor.analysis.models import (
    AnalysisChunk,
    AnalysisProvider,
    BroadScanResponse,
    CandidateRefinementResponse,
    CandidateWindow,
    ProviderResult,
    ProviderUsage,
    RetryPolicy,
    UploadedFile,
)
from video_editor.analysis.proxy_chunks import AuthorizedUpload
from video_editor.errors import ErrorCategory, VideoEditorError

MODEL_ID = "gemini-2.5-flash"
_BROAD_FPS = 0.5
_RESPONSE_MIME_TYPE = "application/json"
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

    def __repr__(self) -> str:
        """Return safe adapter representation without client or key data."""
        return f"{type(self).__name__}(model={MODEL_ID!r})"

    @classmethod
    def from_environment(
        cls, *, maximum_request_cost_usd: Decimal | None = None
    ) -> GeminiAdapter:
        """Create adapter from runtime environment without retaining its key."""
        return cls(
            api_key=os.getenv("GEMINI_API_KEY"),
            maximum_request_cost_usd=maximum_request_cost_usd,
        )

    @staticmethod
    def estimate_broad_request_maximum(
        authorization: AuthorizedUpload, chunk: AnalysisChunk
    ) -> Decimal:
        """Return conservative live-contract ceiling for one tiny broad request."""
        del authorization, chunk
        return Decimal("0.01")

    def upload(self, authorization: AuthorizedUpload) -> UploadedFile:
        """Upload exact bytes from one validated upload authorization."""
        if not isinstance(authorization, AuthorizedUpload):
            raise TypeError("upload requires AuthorizedUpload")
        with authorization:
            stream = authorization.stream
            if not isinstance(stream, IOBase):
                raise TypeError("AuthorizedUpload stream must be an IOBase")
            remote = self._retry(
                lambda: self._client.files.upload(
                    file=stream,
                    config={"mime_type": "video/mp4"},
                )
            )
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
        *,
        prompt_version: str,
    ) -> ProviderResult[BroadScanResponse]:
        """Analyze one whole proxy chunk at static 0.5 FPS."""
        return self._analyze(
            upload=upload,
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
        chunk_id: str,
        start: Decimal,
        end: Decimal,
        fps: float,
        prompt_version: str,
        response_model: type[_ResponseT],
        candidate_id: str | None = None,
    ) -> ProviderResult[_ResponseT]:
        prompt = self._prompt(
            prompt_version,
            chunk_id=chunk_id,
            candidate_id=candidate_id,
            start=start,
            end=end,
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
        )
        last_response: Any | None = None
        for _ in range(2):
            response = self._generate(part, prompt, config)
            last_response = response
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
                continue
            return ProviderResult(response=parsed, usage=self._usage(response))
        del last_response
        raise self._invalid_response()

    def _generate(
        self, part: types.Part, prompt: str, config: types.GenerateContentConfig
    ) -> Any:
        """Generate one response under shared bounded SDK retry policy."""
        return self._retry(
            lambda: self._client.models.generate_content(
                model=MODEL_ID,
                contents=[part, prompt],
                config=config,
            )
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
    def _usage(response: Any) -> ProviderUsage:
        metadata = getattr(response, "usage_metadata", None)
        return ProviderUsage(
            request_id=str(getattr(response, "response_id", None) or "unknown"),
            prompt_tokens=int(getattr(metadata, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(metadata, "response_token_count", 0) or 0),
            total_tokens=int(getattr(metadata, "total_token_count", 0) or 0),
        )

    @staticmethod
    def _prompt(
        prompt_version: str,
        *,
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
                    f"chunk_id: {chunk_id}",
                    f"interval: {interval}",
                    "Return only JSON matching response schema.",
                    "Do not transcribe speech.",
                    "Do not infer speech meaning.",
                    "Do not identify any person.",
                )
            )
        if prompt_version == "candidate-v1" and candidate_id is not None:
            return "\n".join(
                (
                    "mode: candidate",
                    "prompt_version: candidate-v1",
                    f"chunk_id: {chunk_id}",
                    f"candidate_id: {candidate_id}",
                    f"interval: {interval}",
                    "Return only JSON matching response schema.",
                    "Speech meaning may be summarized only for this candidate interval.",
                    "Do not identify any person.",
                )
            )
        raise VideoEditorError(
            ErrorCategory.PROVIDER,
            "Unsupported Gemini prompt version",
            code="provider_invalid_request",
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
