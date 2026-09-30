"""Offline RED contract for Gemini Files and structured analysis responses."""

from __future__ import annotations

import json
import os
import re
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from video_editor.errors import VideoEditorError

try:
    from video_editor.analysis.gemini import (
        BroadScanResponse,
        CandidateRefinementResponse,
        CandidateWindow,
        GeminiAdapter,
        RetryPolicy,
    )
except ModuleNotFoundError as exc:
    _GEMINI_IMPORT_ERROR: ModuleNotFoundError | None = exc
    BroadScanResponse = None
    CandidateRefinementResponse = None
    CandidateWindow = None
    GeminiAdapter = None
    RetryPolicy = None
else:
    _GEMINI_IMPORT_ERROR = None

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "gemini"
SECRET = "AIza-not-a-real-key"


class FakeProviderError(RuntimeError):
    """SDK-shaped failure carrying an HTTP status without real network access."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ParsedResponse:
    parsed: object
    response_id: str = "request-123"
    usage_metadata: object = field(
        default_factory=lambda: SimpleNamespace(
            prompt_token_count=11,
            candidates_token_count=17,
            total_token_count=28,
        )
    )

    @property
    def text(self) -> str:
        raise AssertionError("adapter must use response.parsed, never response.text")


class FakeFiles:
    def __init__(self) -> None:
        self.uploads: list[tuple[object, object]] = []
        self.get_states: deque[str] = deque(["ACTIVE"])
        self.deleted: list[str] = []

    def upload(self, *, file: object, config: object) -> object:
        self.uploads.append((file, config))
        return SimpleNamespace(
            name=f"files/proxy-{len(self.uploads)}",
            uri=f"gs://offline/proxy-{len(self.uploads)}",
            mime_type="video/mp4",
            state="PROCESSING",
            expiration_time="2026-10-01T00:00:00Z",
        )

    def get(self, *, name: str) -> object:
        state = self.get_states.popleft() if self.get_states else "ACTIVE"
        return SimpleNamespace(
            name=name,
            uri=f"gs://offline/{name.rsplit('-', 1)[-1]}",
            mime_type="video/mp4",
            state=state,
            expiration_time="2026-10-01T00:00:00Z",
        )

    def delete(self, *, name: str) -> None:
        self.deleted.append(name)


class FakeModels:
    def __init__(self) -> None:
        self.responses: deque[object] = deque()
        self.requests: list[dict[str, object]] = []
        self.calls = 0

    def generate_content(self, **kwargs: object) -> object:
        self.calls += 1
        self.requests.append(kwargs)
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClient:
    def __init__(self) -> None:
        self.files = FakeFiles()
        self.models = FakeModels()


@pytest.fixture
def gemini_contract() -> None:
    assert _GEMINI_IMPORT_ERROR is None, (
        "Gemini adapter contract missing: "
        "video_editor.analysis.gemini public symbols do not exist"
    )


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def adapter(gemini_contract: None, fake_client: FakeClient) -> Any:
    assert GeminiAdapter is not None
    assert RetryPolicy is not None
    return GeminiAdapter(
        fake_client,
        api_key=SECRET,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=Decimal("0.1")),
        sleeper=lambda _: None,
    )


@pytest.fixture
def manifest() -> object:
    return SimpleNamespace(
        manifest_id="manifest-1",
        chunk_id="chunk-1",
        job_id="job-1",
        path=Path("/generated/job-1/proxy.mp4"),
        digest="a" * 64,
        data=SimpleNamespace(
            chunk_id="chunk-1",
            proxy_start=Decimal(0),
            proxy_end=Decimal(6),
        ),
    )


@pytest.fixture
def broad_chunk() -> object:
    return SimpleNamespace(
        chunk_id="chunk-1",
        proxy_start=Decimal(0),
        proxy_end=Decimal(6),
    )


@pytest.fixture
def candidate(gemini_contract: None) -> object:
    assert CandidateWindow is not None
    return CandidateWindow(
        chunk_id="chunk-1",
        candidate_id="candidate-1",
        start=Decimal(1),
        end=Decimal("3.5"),
    )


def fixture_payload(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text())


def queued_response(name: str) -> ParsedResponse:
    return ParsedResponse(parsed=fixture_payload(name))


def upload_active(adapter: Any, manifest: object) -> object:
    upload = adapter.upload(manifest)
    assert upload.name == "files/proxy-1"
    return upload


def test_sanitized_recorded_fixtures_have_no_credentials_or_identity_data() -> None:
    forbidden = re.compile(
        r"(api[_-]?key|authorization|bearer|token|email|phone|face|identity)",
        re.IGNORECASE,
    )
    for path in sorted(FIXTURE_ROOT.glob("*.json")):
        payload = fixture_payload(path.name)
        serialized = json.dumps(payload, sort_keys=True)
        assert "AIza" not in serialized
        assert "@" not in serialized
        assert not forbidden.search(serialized)


def test_upload_sets_video_mp4_waits_for_active_and_deletes_upload(
    adapter: Any, fake_client: FakeClient, manifest: object
) -> None:
    fake_client.files.get_states.extend(["PROCESSING", "ACTIVE"])

    upload = adapter.upload(manifest)
    active = adapter.wait_until_active(upload)
    adapter.delete_upload(active)

    uploaded_file, upload_config = fake_client.files.uploads[0]
    assert uploaded_file == manifest.path
    assert upload_config == {"mime_type": "video/mp4"}
    assert active.state == "ACTIVE"
    assert fake_client.files.deleted == ["files/proxy-1"]


def test_failed_file_processing_is_terminal_and_never_generates_content(
    adapter: Any, fake_client: FakeClient, manifest: object
) -> None:
    upload = upload_active(adapter, manifest)
    fake_client.files.get_states = deque(["FAILED"])

    with pytest.raises(VideoEditorError, match="processing") as caught:
        adapter.wait_until_active(upload)

    assert caught.value.code == "provider_file_failed"
    assert fake_client.models.calls == 0


def test_expired_file_reuploads_before_follow_up_analysis(
    adapter: Any,
    fake_client: FakeClient,
    manifest: object,
    broad_chunk: object,
) -> None:
    upload = upload_active(adapter, manifest)
    fake_client.files.get_states = deque(["EXPIRED"])
    fake_client.models.responses.append(queued_response("broad-response.json"))

    result = adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")

    assert result.response.chunk_id == "chunk-1"
    assert len(fake_client.files.uploads) == 2
    part = fake_client.models.requests[0]["contents"][0]
    assert part.file_data.file_uri == "gs://offline/proxy-2"


def test_broad_request_uses_exact_model_static_metadata_and_strict_schema(
    adapter: Any,
    fake_client: FakeClient,
    manifest: object,
    broad_chunk: object,
) -> None:
    upload = upload_active(adapter, manifest)
    fake_client.models.responses.append(queued_response("broad-response.json"))

    result = adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")

    assert BroadScanResponse is not None
    request = fake_client.models.requests[0]
    part = request["contents"][0]
    config = request["config"]
    assert request["model"] == "gemini-2.5-flash"
    assert part.video_metadata.fps == 0.5
    assert part.video_metadata.start_offset == "0s"
    assert part.video_metadata.end_offset == "6s"
    assert config.response_mime_type == "application/json"
    assert config.response_schema is BroadScanResponse
    assert isinstance(result.response, BroadScanResponse)
    assert result.response.chunk_id == broad_chunk.chunk_id


@pytest.mark.parametrize("fps", [2, 3, 5])
def test_candidate_request_uses_bounded_fps_and_duration_string_offsets(
    adapter: Any,
    fake_client: FakeClient,
    manifest: object,
    candidate: object,
    fps: int,
) -> None:
    upload = upload_active(adapter, manifest)
    fake_client.models.responses.append(queued_response("refinement-response.json"))

    result = adapter.refine_candidate(
        upload,
        candidate,
        fps=fps,
        prompt_version="candidate-v1",
    )

    assert CandidateRefinementResponse is not None
    request = fake_client.models.requests[0]
    part = request["contents"][0]
    assert request["model"] == "gemini-2.5-flash"
    assert part.video_metadata.fps == float(fps)
    assert part.video_metadata.start_offset == "1s"
    assert part.video_metadata.end_offset == "3.5s"
    assert request["config"].response_schema is CandidateRefinementResponse
    assert result.response.candidate_id == "candidate-1"


def test_response_rejects_wrong_chunk_identity_and_out_of_window_timestamps(
    adapter: Any,
    fake_client: FakeClient,
    manifest: object,
    broad_chunk: object,
) -> None:
    upload = upload_active(adapter, manifest)
    fake_client.models.responses.append(queued_response("malformed-response.json"))

    with pytest.raises(VideoEditorError, match="chunk|timestamp") as caught:
        adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")

    assert caught.value.code == "provider_response_invalid"
    assert fake_client.models.calls == 1


def test_broad_schema_forbids_transcript_speech_meaning_and_person_identity(
    gemini_contract: None,
) -> None:
    assert BroadScanResponse is not None
    payload = fixture_payload("broad-response.json")

    for forbidden in ("transcript", "speech_meaning_summary", "person_identity"):
        with pytest.raises(ValidationError):
            BroadScanResponse.model_validate({**payload, forbidden: "not allowed"})


def test_candidate_schema_allows_bounded_speech_meaning_but_rejects_identity(
    gemini_contract: None,
) -> None:
    assert CandidateRefinementResponse is not None
    payload = fixture_payload("refinement-response.json")

    response = CandidateRefinementResponse.model_validate(payload)
    assert response.speech_meaning_summary == "Speaker reacts to reaching the overlook."
    with pytest.raises(ValidationError):
        CandidateRefinementResponse.model_validate(
            {**payload, "person_identity": "not allowed"}
        )


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_calls"),
    [
        (FakeProviderError(401, "bad credentials"), "provider_authentication", 1),
        (FakeProviderError(400, "invalid request"), "provider_invalid_request", 1),
        (FakeProviderError(429, "rate limit"), "provider_rate_limited", 3),
        (FakeProviderError(503, "service unavailable"), "provider_unavailable", 3),
        (OSError("offline network"), "provider_network", 3),
    ],
)
def test_retry_policy_retries_only_transient_failures(
    adapter: Any,
    fake_client: FakeClient,
    manifest: object,
    broad_chunk: object,
    error: BaseException,
    expected_code: str,
    expected_calls: int,
) -> None:
    upload = upload_active(adapter, manifest)
    fake_client.models.responses.extend([error] * expected_calls)

    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")

    assert caught.value.code == expected_code
    assert fake_client.models.calls == expected_calls


def test_malformed_structured_response_receives_one_schema_repair_only(
    adapter: Any,
    fake_client: FakeClient,
    manifest: object,
    broad_chunk: object,
) -> None:
    upload = upload_active(adapter, manifest)
    fake_client.models.responses.extend(
        [
            ParsedResponse(parsed={"schema_version": "broad-v1"}),
            queued_response("broad-response.json"),
        ]
    )

    result = adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")

    assert result.response.chunk_id == "chunk-1"
    assert fake_client.models.calls == 2
    assert (
        fake_client.models.requests[1]["config"].response_mime_type
        == "application/json"
    )


def test_usage_payload_preserves_request_id_and_redacts_key_from_repr_and_errors(
    adapter: Any,
    fake_client: FakeClient,
    manifest: object,
    broad_chunk: object,
) -> None:
    upload = upload_active(adapter, manifest)
    fake_client.models.responses.append(queued_response("broad-response.json"))

    result = adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")
    payload = result.usage.safe_payload()

    assert payload == {
        "request_id": "request-123",
        "prompt_tokens": 11,
        "output_tokens": 17,
        "total_tokens": 28,
    }
    assert SECRET not in repr(adapter)
    fake_client.models.responses.append(FakeProviderError(401, f"failed {SECRET}"))
    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)


@pytest.mark.gemini_live
@pytest.mark.skipif(
    not os.getenv("RUN_GEMINI_LIVE_TESTS"),
    reason="explicit opt-in required; default suite is offline",
)
def test_live_broad_scan_preflights_maximum_cost_before_upload(
    gemini_contract: None,
    manifest: object,
    broad_chunk: object,
) -> None:
    assert GeminiAdapter is not None
    maximum = GeminiAdapter.estimate_broad_request_maximum(manifest, broad_chunk)
    assert maximum <= Decimal("0.01")

    adapter = GeminiAdapter.from_environment(maximum_request_cost_usd=maximum)
    upload = adapter.upload(manifest)
    result = adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")

    assert result.response.chunk_id == "chunk-1"
    assert result.usage.actual_cost_usd <= maximum
