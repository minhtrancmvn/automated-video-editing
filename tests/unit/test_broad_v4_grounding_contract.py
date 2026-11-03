"""Offline request contracts; mocked prose does not prove visual grounding."""

from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_broad_v3_timestamp_contract import _payload, _request

from video_editor.analysis.gemini import GeminiAdapter
from video_editor.analysis.models import AnalysisRequestContext, BroadScanResponse
from video_editor.analysis.pricing import pricing_for_model
from video_editor.errors import VideoEditorError

D = Decimal


def _client(start="1", end="2"):
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
    return client


def _scan(client, start="0", end="720", *, request=None, version="broad-v4"):
    return GeminiAdapter(client).broad_scan(
        SimpleNamespace(
            name="files/offline",
            uri="gs://offline/proxy",
            mime_type="video/mp4",
            manifest_id="manifest-1",
        ),
        SimpleNamespace(
            chunk_id=_request().chunk_id, proxy_start=D(start), proxy_end=D(end)
        ),
        request or _request(),
        prompt_version=version,
    )


def test_v4_outgoing_request_requires_candidate_local_visible_evidence():
    client = _client()
    _scan(client)

    sent = client.models.generate_content.call_args.kwargs
    prompt = sent["contents"][1]
    for rule in (
        "prompt_version: broad-v4",
        "elapsed decimal-second strings on supplied proxy timeline",
        "never reset interval origin",
        "MM:SS = minutes*60+seconds, not joined digits",
        "04:16 = 256, 06:45 = 405, 12:00 = 720",
        "Each candidate reason must describe only events visible within its own [start,end]",
        "not events from other times or whole-scene context",
        "If unsure, use plain visible facts or omit candidate.",
        "Cross-scene candidates are allowed when their reasons match their own interval.",
        "Return only JSON matching response schema.",
        "Do not transcribe speech.",
        "Do not infer speech meaning.",
        "Do not identify any person.",
    ):
        assert rule in prompt
    assert not any(word in prompt.lower() for word in ("bridge", "gx011485", "cand_2"))
    assert len(prompt.encode()) == 945
    assert sent["config"].max_output_tokens == 8192
    assert sent["config"].response_schema.title == "BroadScanResponse"
    metadata = sent["contents"][0].video_metadata
    assert (metadata.start_offset, metadata.end_offset, metadata.fps) == (
        "0s",
        "720s",
        0.5,
    )
    assert client.models.generate_content.call_count == 1


@pytest.mark.parametrize(
    "start,end,serialized_start,serialized_end,size",
    [
        ("0", "720", "0", "720", 945),
        ("600", "628.2240000000001600710675120", "600", "628.224", 979),
        ("600.0000000001", "628.2240000009", "600.000000001", "628.224", 985),
        ("0", "900.0324669999999969149939716", "0", "900.032466999", 981),
        ("600.0000000005", "628.2240000005", "600.000000001", "628.224", 985),
    ],
)
def test_v4_actual_prompt_fits_real_ids_with_inward_metadata(
    start, end, serialized_start, serialized_end, size
):
    client = _client(serialized_start, serialized_end)
    response = _scan(client, start, end).response

    sent = client.models.generate_content.call_args.kwargs
    prompt = sent["contents"][1]
    assert len(prompt.encode()) == size <= 1024
    assert (
        f"serialized interval: [{serialized_start}, {serialized_end}] seconds" in prompt
    )
    metadata = sent["contents"][0].video_metadata
    assert metadata.start_offset == f"{serialized_start}s"
    assert metadata.end_offset == f"{serialized_end}s"
    assert response.candidates[0].start >= D(start)
    assert response.candidates[0].end <= D(end)
    assert client.models.generate_content.call_count == 1


@pytest.mark.parametrize("end", ["416", "645", "705", "720"])
def test_v4_does_not_reinterpret_legal_seconds_as_clock_digits(end):
    response = _scan(_client("1", end)).response
    assert response.candidates[0].end == D(end)
    assert response.speech_presence_ranges[0].end == D(end)
    assert response.schema_version == "broad-v1"


@pytest.mark.parametrize(
    "start,end", [("600.0000000001", "600.0000000009"), ("600", "600.0000000001")]
)
def test_v4_collapsed_interval_fails_before_activation(start, end):
    client = _client()
    with pytest.raises(VideoEditorError) as caught:
        _scan(client, start, end)
    assert caught.value.code == "provider_invalid_request"
    assert "collapses at nanosecond precision" in str(caught.value)
    client.files.get.assert_not_called()
    client.models.generate_content.assert_not_called()


@pytest.mark.parametrize("start,end", [("599", "620"), ("600", "628.224000001")])
def test_v4_rejects_response_outside_original_bounds_with_existing_repair_limit(
    start, end
):
    client = _client(start, end)
    with pytest.raises(VideoEditorError) as caught:
        _scan(client, "600", "628.2240000009")
    assert caught.value.code == "provider_response_invalid"
    assert client.models.generate_content.call_count == 2


def test_v4_cost_admission_remains_broad_with_unchanged_nominal_cap():
    pricing = pricing_for_model("gemini-3.8-flash")
    client = Mock()
    adapter = GeminiAdapter(client, pricing=pricing)
    chunk = SimpleNamespace(proxy_start=D(0), proxy_end=D(720))
    authorization = SimpleNamespace(manifest_id="manifest-1")
    for version in ("broad-v1", "broad-v2", "broad-v3", "broad-v4"):
        assert adapter.maximum_request_cost(
            "manifest-1", chunk, prompt_version=version
        ) == D("0.262051")
        assert GeminiAdapter.estimate_broad_request_maximum(
            authorization, chunk, pricing, prompt_version=version
        ) == D("0.262051")
    assert adapter.maximum_request_cost(
        "manifest-1", chunk, prompt_version="candidate-v4"
    ) > D("0.262051")
    client.assert_not_called()


def test_unknown_version_stays_blocked_in_cost_and_dispatch():
    pricing = pricing_for_model("gemini-3.8-flash")
    client = _client()
    chunk = SimpleNamespace(proxy_start=D(0), proxy_end=D(720))
    with pytest.raises(ValueError, match="supported prompt and manifest"):
        GeminiAdapter.estimate_broad_request_maximum(
            SimpleNamespace(manifest_id="manifest-1"),
            chunk,
            pricing,
            prompt_version="broad-v99",
        )
    with pytest.raises(VideoEditorError) as cost:
        GeminiAdapter(client, pricing=pricing).maximum_request_cost(
            "manifest-1", chunk, prompt_version="broad-v99"
        )
    assert cost.value.code == "provider_invalid_request"
    with pytest.raises(VideoEditorError) as scan:
        _scan(client, version="broad-v99")
    assert scan.value.code == "provider_invalid_request"
    client.files.get.assert_not_called()
    client.models.generate_content.assert_not_called()


def test_v4_overlong_prompt_still_fails_before_activation_not_upload_claim():
    client = _client()
    request = _request().model_copy(update={"reservation_id": "x" * 1100})
    with pytest.raises(VideoEditorError) as caught:
        _scan(client, request=request)
    assert caught.value.code == "provider_invalid_request"
    assert "exceeds reserved text allowance" in str(caught.value)
    client.files.get.assert_not_called()
    client.models.generate_content.assert_not_called()


def test_historical_v3_dispatch_prompt_digest_is_unchanged():
    request = AnalysisRequestContext(
        reservation_id="a105f92a-ca06-5be4-9a96-4bae8a9dd683",
        job_id="5986fd6a-b56d-4c47-94e0-96c002edf18e",
        manifest_id="historical-manifest",
        mode="broad",
        chunk_id=(
            "analysis-chunk-"
            "7c9dc9a8bd567529cb4e29a30813f6e671f78b066b89cfe313be9309d87fe977"
        ),
    )
    prompt = GeminiAdapter._prompt(
        "broad-v3",
        request=request,
        chunk_id=request.chunk_id,
        candidate_id=None,
        start=D(0),
        end=D(720),
    )
    assert sha256(prompt.encode()).hexdigest() == (
        "b3dd6e1d9674fcbafa29098c6dcc2f4dbba21db8715e58b3328b994d8baae999"
    )


def test_schema_cannot_distinguish_wrong_prose_or_ban_cross_scene_candidates():
    payload = _payload("415", "460")
    payload["candidates"][0]["reason"] = (
        "Synthetic bridge claim; intentionally not visual evidence."
    )
    scene = {
        "scene_id": "scene-1",
        "start": "255",
        "end": "425",
        "summary": "Synthetic scene",
        "actions": [],
        "setting": "Synthetic",
        "scenic_interest": 0.5,
        "human_interaction": 0.5,
        "story_milestone": False,
        "technical_problems": [],
        "vertical_suitability": 0.5,
        "confidence": 0.5,
    }
    payload["scenes"] = [
        scene,
        {**scene, "scene_id": "scene-2", "start": "425", "end": "720"},
    ]
    response = BroadScanResponse.model_validate(payload)
    assert response.candidates[0].start < response.scenes[0].end
    assert response.candidates[0].end > response.scenes[1].start
    assert response.candidates[0].reason == payload["candidates"][0]["reason"]
