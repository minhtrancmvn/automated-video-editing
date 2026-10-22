from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from video_editor.analysis.gemini import GeminiAdapter
from video_editor.analysis.models import (
    AnalysisBoundaryKind,
    AnalysisChunkData,
    AnalysisRequestContext,
    BroadScanResponse,
    RetryPolicy,
)
from video_editor.analysis.pricing import GEMINI_MODEL, ModelPricing
from video_editor.errors import VideoEditorError


def _pricing() -> ModelPricing:
    return ModelPricing(
        model=GEMINI_MODEL,
        media_input_usd_per_million_tokens=Decimal("0.75"),
        audio_input_usd_per_million_tokens=Decimal("0.75"),
        text_input_usd_per_million_tokens=Decimal("0.75"),
        output_usd_per_million_tokens=Decimal("3.75"),
        source_url="https://example.invalid/pricing",
        effective_date=date(2026, 10, 3),
        expires_on=date(2027, 1, 1),
    )


def _broad_payload(end: str) -> dict[str, Any]:
    return {
        "schema_version": "broad-v1",
        "chunk_id": "chunk-1",
        "scenes": [
            {
                "start": "0",
                "end": end,
                "scene_id": "scene-1",
                "summary": "A vehicle moves along a road.",
                "actions": ["driving"],
                "setting": "road",
                "scenic_interest": 0.4,
                "human_interaction": 0.0,
                "story_milestone": False,
                "technical_problems": [],
                "vertical_suitability": 0.5,
                "confidence": 0.8,
            }
        ],
        "speech_presence_ranges": [],
        "candidates": [],
    }


class _Files:
    def get(self, *, name: str) -> object:
        return SimpleNamespace(
            name=name,
            uri="gs://offline/video",
            mime_type="video/mp4",
            state="ACTIVE",
            expiration_time=None,
        )


class _Models:
    def __init__(self, response: object) -> None:
        self.response = response
        self.request: dict[str, object] | None = None
        self.calls = 0

    def generate_content(self, **kwargs: object) -> object:
        self.calls += 1
        self.request = kwargs
        return self.response


class _Client:
    def __init__(self, response: object) -> None:
        self.files = _Files()
        self.models = _Models(response)


def _response(payload: dict[str, Any]) -> object:
    return SimpleNamespace(
        parsed=payload,
        response_id="test-request",
        candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10,
            candidates_token_count=10,
            thoughts_token_count=0,
            total_token_count=20,
            prompt_tokens_details=[
                types.ModalityTokenCount(modality="TEXT", token_count=10)
            ],
        ),
    )


def _request_context() -> AnalysisRequestContext:
    return AnalysisRequestContext(
        reservation_id="reservation-1",
        job_id="job-1",
        manifest_id="manifest-1",
        mode="broad",
        chunk_id="chunk-1",
    )


def test_broad_v2_uses_serialized_offset_bounds_and_strict_validation() -> None:
    end = Decimal("628.2240000000001600710675120")
    rounded_end = Decimal(GeminiAdapter._duration(end)[:-1])
    client = _Client(_response(_broad_payload(str(rounded_end))))
    adapter = GeminiAdapter(
        client, retry_policy=RetryPolicy(max_attempts=1), pricing=_pricing()
    )
    upload = SimpleNamespace(
        name="files/proxy",
        uri="gs://offline/video",
        mime_type="video/mp4",
        state="ACTIVE",
        manifest_id="manifest-1",
    )
    chunk = SimpleNamespace(chunk_id="chunk-1", proxy_start=Decimal(0), proxy_end=end)

    result = adapter.broad_scan(
        upload, chunk, _request_context(), prompt_version="broad-v2"
    )

    prompt = client.models.request["contents"][1]  # type: ignore[index]
    assert "prompt_version: broad-v2" in prompt
    assert "serialized interval: [0, 628.224] seconds" in prompt
    assert "All returned timestamps must be within the serialized interval" in prompt
    assert result.response.scenes[0].end == rounded_end


def test_broad_v1_retains_strict_local_interval_validation() -> None:
    end = Decimal("628.2240000000001600710675120")
    rounded_end = Decimal(GeminiAdapter._duration(end)[:-1])
    client = _Client(_response(_broad_payload(str(rounded_end + Decimal("0.001")))))
    adapter = GeminiAdapter(
        client, retry_policy=RetryPolicy(max_attempts=1), pricing=_pricing()
    )
    upload = SimpleNamespace(
        name="files/proxy",
        uri="gs://offline/video",
        mime_type="video/mp4",
        state="ACTIVE",
        manifest_id="manifest-1",
    )
    chunk = SimpleNamespace(chunk_id="chunk-1", proxy_start=Decimal(0), proxy_end=end)

    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(upload, chunk, _request_context(), prompt_version="broad-v1")

    assert caught.value.code == "provider_response_invalid"


def test_broad_prompt_versions_have_identical_reservation_estimates() -> None:
    chunk = AnalysisChunkData(
        schema_version=1,
        job_id="job-1",
        source_id="source-1",
        source_identity="identity",
        mapping_version="mapping",
        source_start=Decimal(0),
        source_end=Decimal("628.2240000000001600710675120"),
        proxy_start=Decimal(0),
        proxy_end=Decimal("628.2240000000001600710675120"),
        boundary_kind=AnalysisBoundaryKind.SCENE,
        implementation_version="cloud-proxy-v1",
    )
    adapter = GeminiAdapter(
        _Client(_response(_broad_payload("1"))),
        retry_policy=RetryPolicy(max_attempts=1),
        pricing=_pricing(),
    )

    assert adapter.maximum_request_cost(
        "manifest-1", chunk, prompt_version="broad-v2"
    ) == adapter.maximum_request_cost("manifest-1", chunk, prompt_version="broad-v1")


def test_broad_v2_timestamp_ceiling_is_exactly_serialized_offset() -> None:
    end = Decimal("628.2240000000001600710675120")
    serialized = Decimal(GeminiAdapter._duration(end)[:-1])

    assert serialized == Decimal("628.224")
    assert serialized <= end
    with pytest.raises(VideoEditorError):
        GeminiAdapter._validate_response(
            BroadScanResponse.model_validate(_broad_payload("628.225")),
            chunk_id="chunk-1",
            candidate_id=None,
            start=Decimal(0),
            end=serialized,
        )
    GeminiAdapter._validate_response(
        BroadScanResponse.model_validate(_broad_payload(str(serialized))),
        chunk_id="chunk-1",
        candidate_id=None,
        start=Decimal(0),
        end=serialized,
    )
