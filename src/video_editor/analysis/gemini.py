"""Google Gemini adapter for schema-constrained video analysis."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, TypeVar, cast

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
    ProxyManifest,
    RetryPolicy,
    UploadedFile,
)
from video_editor.errors import ErrorCategory, VideoEditorError

MODEL_ID = "gemini-2.5-flash"
_BROAD_FPS = 0.5
_RESPONSE_MIME_TYPE = "application/json"
_ResponseT = TypeVar("_ResponseT", BroadScanResponse, CandidateRefinementResponse)


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
        """Create adapter with an injected client or a runtime-only API key."""
        if client is None and not api_key:
            raise VideoEditorError(
                ErrorCategory.CONFIGURATION,
                "Gemini API key is required",
                code="provider_configuration",
            )
        self._client = client if client is not None else genai.Client(api_key=api_key)
        self._api_key = api_key
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
        """Create adapter from runtime environment without persisting its key."""
        return cls(
            api_key=os.getenv("GEMINI_API_KEY"),
            maximum_request_cost_usd=maximum_request_cost_usd,
        )

    @staticmethod
    def estimate_broad_request_maximum(
        manifest: ProxyManifest, chunk: AnalysisChunk
    ) -> Decimal:
        """Return conservative live-contract ceiling for one tiny broad request."""
        del manifest, chunk
        return Decimal("0.01")

    def upload(self, manifest: ProxyManifest) -> UploadedFile:
        """Upload one validated MP4 proxy and retain manifest for expiry recovery."""
        try:
            uploaded = self._client.files.upload(
                file=manifest.path,
                config={"mime_type": "video/mp4"},
            )
        except Exception as exc:  # noqa: BLE001 - SDK exposes heterogeneous errors
            raise self._provider_error(exc) from None
        return UploadedFile(
            name=str(uploaded.name),
            uri=str(uploaded.uri),
            mime_type=str(uploaded.mime_type or "video/mp4"),
            state=self._state_value(uploaded.state),
            expiration_time=self._optional_string(uploaded.expiration_time),
            manifest=manifest,
        )

    def wait_until_active(self, upload: UploadedFile) -> UploadedFile:
        """Poll file processing until ACTIVE or a terminal state is observed."""
        current = upload
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                remote = self._client.files.get(name=current.name)
            except Exception as exc:  # noqa: BLE001 - SDK exposes heterogeneous errors
                raise self._provider_error(exc) from None
            state = self._state_value(remote.state)
            if state == "ACTIVE":
                return current.model_copy(update={"state": state})
            if state == "FAILED":
                raise VideoEditorError(
                    ErrorCategory.PROVIDER,
                    "Gemini file processing failed",
                    code="provider_file_failed",
                )
            if state == "EXPIRED":
                return current.model_copy(update={"state": state})
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
            identity=chunk.chunk_id,
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
            identity=candidate.chunk_id,
            candidate_id=candidate.candidate_id,
            start=candidate.start,
            end=candidate.end,
            fps=float(fps),
            prompt_version=prompt_version,
            response_model=CandidateRefinementResponse,
        )

    def delete_upload(self, upload: UploadedFile) -> None:
        """Delete provider upload by Files API name."""
        try:
            self._client.files.delete(name=upload.name)
        except Exception as exc:  # noqa: BLE001 - SDK exposes heterogeneous errors
            raise self._provider_error(exc) from None

    def _analyze(
        self,
        *,
        upload: UploadedFile,
        identity: str,
        start: Decimal,
        end: Decimal,
        fps: float,
        prompt_version: str,
        response_model: type[_ResponseT],
        candidate_id: str | None = None,
    ) -> ProviderResult[_ResponseT]:
        if not prompt_version:
            raise VideoEditorError(
                ErrorCategory.PROVIDER,
                "Prompt version is required",
                code="provider_invalid_request",
            )
        active = self.wait_until_active(upload)
        if active.state == "EXPIRED":
            active = self.wait_until_active(
                self.upload(cast(ProxyManifest, upload.manifest))
            )
        part = types.Part(
            file_data=types.FileData(
                file_uri=active.uri,
                mime_type=active.mime_type,
            ),
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
        response = self._generate(part, config)
        try:
            parsed = response_model.model_validate(response.parsed)
        except (ValidationError, TypeError, ValueError):
            response = self._generate(part, config)
            try:
                parsed = response_model.model_validate(response.parsed)
            except (ValidationError, TypeError, ValueError):
                raise self._invalid_response() from None
        self._validate_response(
            parsed,
            chunk_id=identity,
            candidate_id=candidate_id,
            start=start,
            end=end,
        )
        usage = self._usage(response)
        return ProviderResult(response=parsed, usage=usage)

    def _generate(self, part: types.Part, config: types.GenerateContentConfig) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                return self._client.models.generate_content(
                    model=MODEL_ID,
                    contents=[part],
                    config=config,
                )
            except Exception as exc:  # noqa: BLE001 - SDK exposes heterogeneous errors
                last_error = exc
                mapped = self._provider_error(exc)
                if (
                    not self._is_retryable(mapped)
                    or attempt == self._retry_policy.max_attempts
                ):
                    raise mapped from None
                self._sleep(attempt)
        raise self._provider_error(cast(Exception, last_error))

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
            output_tokens=int(getattr(metadata, "candidates_token_count", 0) or 0),
            total_tokens=int(getattr(metadata, "total_token_count", 0) or 0),
            actual_cost_usd=Decimal(0),
        )

    def _provider_error(self, error: Exception) -> VideoEditorError:
        status = getattr(error, "status_code", None)
        if status in {401, 403}:
            code = "provider_authentication"
            message = "Gemini authentication failed"
        elif status is not None and 400 <= status < 500 and status != 429:
            code = "provider_invalid_request"
            message = "Gemini request was invalid"
        elif status == 429:
            code = "provider_rate_limited"
            message = "Gemini request was rate limited"
        elif status is not None and status >= 500:
            code = "provider_unavailable"
            message = "Gemini service is unavailable"
        elif isinstance(error, OSError):
            code = "provider_network"
            message = "Gemini network request failed"
        else:
            code = "provider_unavailable"
            message = "Gemini provider request failed"
        return VideoEditorError(ErrorCategory.PROVIDER, message, code=code)

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
        text = str(value)
        return text.rsplit(".", 1)[-1]

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return None if value is None else str(value)


__all__ = [
    "AnalysisProvider",
    "BroadScanResponse",
    "CandidateRefinementResponse",
    "CandidateWindow",
    "GeminiAdapter",
    "RetryPolicy",
]
