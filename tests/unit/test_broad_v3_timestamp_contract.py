"""Offline contracts for explicit broad timestamp units, not semantic grounding."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from video_editor.analysis.gemini import GeminiAdapter
from video_editor.analysis.models import AnalysisRequestContext, BroadScanResponse
from video_editor.analysis.orchestrator import analysis_cache_key
from video_editor.analysis.pricing import pricing_for_model
from video_editor.errors import VideoEditorError
from video_editor.workflow import BROAD_PROMPT_VERSION, WorkflowService

D = Decimal


def _request() -> AnalysisRequestContext:
    return AnalysisRequestContext(
        reservation_id="991955f5-bdb3-5482-9d95-c0f521f37517",
        job_id="5775c9e6-c91f-4e62-9411-e016b5c8ac14",
        manifest_id="manifest-1",
        mode="broad",
        chunk_id="analysis-chunk-" + "a" * 64,
    )


def _payload(start: str, end: str) -> dict[str, object]:
    return {
        "schema_version": "broad-v1",
        "chunk_id": _request().chunk_id,
        "scenes": [],
        "speech_presence_ranges": [{"start": start, "end": end}],
        "candidates": [
            {
                "candidate_id": "candidate-1",
                "start": start,
                "end": end,
                "category": "scenic",
                "reason": "Offline fixture; no claim of temporal grounding.",
                "confidence": 0.8,
            }
        ],
    }


def _scan(
    start: str, end: str, *, interval_start: str = "0", interval_end: str = "720"
) -> tuple[BroadScanResponse, Mock]:
    client = Mock()
    client.files.get.return_value = SimpleNamespace(
        name="files/offline",
        uri="gs://offline/proxy",
        mime_type="video/mp4",
        state="ACTIVE",
        expiration_time=None,
    )
    client.models.generate_content.return_value = SimpleNamespace(
        parsed=_payload(start, end), usage_metadata=None, response_id="offline"
    )
    adapter = GeminiAdapter(client)
    result = adapter.broad_scan(
        SimpleNamespace(
            name="files/offline",
            uri="gs://offline/proxy",
            mime_type="video/mp4",
            manifest_id="manifest-1",
        ),
        SimpleNamespace(
            chunk_id=_request().chunk_id,
            proxy_start=D(interval_start),
            proxy_end=D(interval_end),
        ),
        _request(),
        prompt_version="broad-v3",
    )
    return result.response, client


def test_broad_v3_outgoing_prompt_defines_elapsed_seconds_for_every_range() -> None:
    _, client = _scan("256", "280")

    sent = client.models.generate_content.call_args.kwargs
    prompt = sent["contents"][1]
    assert "prompt_version: broad-v3" in prompt
    assert "elapsed decimal seconds on supplied proxy timeline" in prompt
    assert "do not reset zero at interval start" in prompt
    assert "MM:SS by minutes*60+seconds, never concatenate digits" in prompt
    assert "04:16 = 256, 06:45 = 405, 12:00 = 720" in prompt
    assert "scenes, speech_presence_ranges and candidates" in prompt
    assert "Do not transcribe speech." in prompt
    assert "Do not infer speech meaning." in prompt
    assert "Do not identify any person." in prompt
    assert len(prompt.encode()) <= 1024
    assert sent["config"].max_output_tokens == 8192
    assert sent["config"].response_schema.title == "BroadScanResponse"
    part = sent["contents"][0]
    assert part.video_metadata.start_offset == "0s"
    assert part.video_metadata.end_offset == "720s"
    assert part.video_metadata.fps == 0.5
    assert client.models.generate_content.call_count == 1


@pytest.mark.parametrize("end", ["416", "645", "705", "720"])
def test_broad_v3_preserves_legal_clock_shaped_seconds_without_conversion(
    end: str,
) -> None:
    response, _ = _scan("1", end)

    assert response.candidates[0].start == D(1)
    assert response.candidates[0].end == D(end)
    assert response.speech_presence_ranges[0].end == D(end)
    assert response.schema_version == "broad-v1"


def test_broad_v3_keeps_nonzero_proxy_time_and_serialized_precision() -> None:
    response, client = _scan(
        "600",
        "628.224",
        interval_start="600",
        interval_end="628.2240000000001600710675120",
    )

    sent = client.models.generate_content.call_args.kwargs
    assert "serialized interval: [600, 628.224] seconds" in sent["contents"][1]
    assert response.candidates[0].start == D(600)
    assert response.candidates[0].end == D("628.224")
    assert sent["contents"][0].video_metadata.start_offset == "600s"
    assert sent["contents"][0].video_metadata.end_offset == "628.224s"


@pytest.mark.parametrize("start,end", [("599", "620"), ("600", "628.225")])
def test_broad_v3_still_rejects_timestamps_outside_proxy_interval(
    start: str,
    end: str,
) -> None:
    with pytest.raises(VideoEditorError) as caught:
        _scan(start, end, interval_start="600", interval_end="628.224")

    assert caught.value.code == "provider_response_invalid"


def test_broad_v3_keeps_default_three_attempt_reservation_within_fixed_cap() -> None:
    pricing = pricing_for_model("gemini-3.8-flash")
    chunk = SimpleNamespace(proxy_start=D(0), proxy_end=D(720))
    adapter = GeminiAdapter(Mock(), pricing=pricing)
    authorization = SimpleNamespace(manifest_id="manifest-1")

    historical = adapter.maximum_request_cost(
        "manifest-1", chunk, prompt_version="broad-v2"
    )
    assert historical == D("0.262051")
    assert (
        adapter.maximum_request_cost("manifest-1", chunk, prompt_version="broad-v3")
        == historical
    )
    assert (
        GeminiAdapter.estimate_broad_request_maximum(
            authorization, chunk, pricing, prompt_version="broad-v3"
        )
        == historical
    )


@pytest.mark.parametrize("version", ["broad-v1", "broad-v2"])
def test_historical_broad_prompt_text_stays_unchanged(version: str) -> None:
    request = _request()
    prompt = GeminiAdapter._prompt(
        version,
        request=request,
        chunk_id=request.chunk_id,
        candidate_id=None,
        start=D(0),
        end=D(720),
    )
    lines = [
        "mode: broad",
        f"prompt_version: {version}",
        f"reservation_id: {request.reservation_id}",
        f"job_id: {request.job_id}",
        f"chunk_id: {request.chunk_id}",
        "interval: [0, 720] seconds",
    ]
    if version == "broad-v2":
        lines.extend(
            [
                "serialized interval: [0, 720] seconds",
                "All returned timestamps must be within the serialized interval.",
            ]
        )
    lines.extend(
        [
            "Return only JSON matching response schema.",
            "Do not transcribe speech.",
            "Do not infer speech meaning.",
            "Do not identify any person.",
        ]
    )
    assert prompt == "\n".join(lines)


def test_default_broad_cache_identity_separates_v3_from_historical_results() -> None:
    service = WorkflowService(
        SimpleNamespace(
            cloud_enabled=True,
            gemini=SimpleNamespace(broad_fps=D("0.5"), model="gemini-3.8-flash"),
        ),
        Mock(),
    )
    identity = service._broad_identity(
        {
            "source_identity": "source-identity",
            "source_fingerprint": "fingerprint",
            "digest": "digest",
            "mapping_version": "mapping-v1",
            "source_start": "0",
            "source_end": "720",
            "width": 640,
            "height": 360,
        }
    )

    assert BROAD_PROMPT_VERSION == "broad-v3"
    assert identity.prompt_version == "broad-v3"
    assert identity.response_schema_version == "broad-v2"
    legacy = identity.model_copy(update={"prompt_version": "broad-v2"})
    assert analysis_cache_key(identity) != analysis_cache_key(legacy)
