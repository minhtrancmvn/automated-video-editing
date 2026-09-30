"""Offline RED contract for Gemini Files and structured analysis responses."""

from __future__ import annotations

import io
import json
import math
import os
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors
from pydantic import ValidationError

from video_editor.analysis.proxy_chunks import AuthorizedUpload
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


@dataclass
class ParsedResponse:
    parsed: object
    response_id: str = "request-123"
    usage_metadata: object = field(
        default_factory=lambda: SimpleNamespace(
            prompt_token_count=11,
            response_token_count=17,
            candidates_token_count=999,
            total_token_count=28,
        )
    )

    @property
    def text(self) -> str:
        raise AssertionError("adapter must use response.parsed, never response.text")


class FakeFiles:
    def __init__(self) -> None:
        self.uploads: list[tuple[object, object]] = []
        self.upload_stream_closed: list[bool] = []
        self.upload_responses: deque[object] = deque()
        self.get_responses: deque[object] = deque()
        self.deleted: list[str] = []
        self.delete_responses: deque[object] = deque()
        self.upload_calls = 0
        self.get_calls = 0
        self.delete_calls = 0

    @staticmethod
    def response(
        *,
        name: str = "files/proxy-1",
        state: str = "ACTIVE",
        expiration_time: datetime | None = None,
    ) -> object:
        return SimpleNamespace(
            name=name,
            uri=f"gs://offline/{name.rsplit('-', 1)[-1]}",
            mime_type="video/mp4",
            state=state,
            expiration_time=expiration_time or datetime.now(UTC) + timedelta(hours=1),
        )

    def upload(self, *, file: object, config: object) -> object:
        self.upload_calls += 1
        self.uploads.append((file, config))
        self.upload_stream_closed.append(bool(getattr(file, "closed", False)))
        response = (
            self.upload_responses.popleft()
            if self.upload_responses
            else self.response(state="PROCESSING")
        )
        if isinstance(response, BaseException):
            raise response
        return response

    def get(self, *, name: str) -> object:
        self.get_calls += 1
        response = (
            self.get_responses.popleft()
            if self.get_responses
            else self.response(name=name)
        )
        if isinstance(response, BaseException):
            raise response
        return response

    def delete(self, *, name: str) -> None:
        self.delete_calls += 1
        response = self.delete_responses.popleft() if self.delete_responses else None
        if isinstance(response, BaseException):
            raise response
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
def upload_authorization() -> AuthorizedUpload:
    return AuthorizedUpload("manifest-1", io.BytesIO(b"registered proxy bytes"))


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


def request_video_part(request: dict[str, object]) -> object:
    contents = request["contents"]
    assert isinstance(contents, list)
    return next(item for item in contents if not isinstance(item, str))


def request_prompt(request: dict[str, object]) -> str:
    contents = request["contents"]
    assert isinstance(contents, list)
    return next(item for item in contents if isinstance(item, str))


def upload_active(adapter: Any) -> object:
    authorization = AuthorizedUpload(
        "manifest-1", io.BytesIO(b"registered proxy bytes")
    )
    upload = adapter.upload(authorization)
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


def test_upload_consumes_exact_authorized_stream_while_context_is_open(
    adapter: Any,
    fake_client: FakeClient,
    upload_authorization: AuthorizedUpload,
) -> None:
    stream = upload_authorization.stream

    upload = adapter.upload(upload_authorization)

    uploaded_file, upload_config = fake_client.files.uploads[0]
    assert uploaded_file is stream
    assert fake_client.files.upload_stream_closed == [False]
    assert stream.closed
    assert upload.manifest_id == "manifest-1"
    assert upload_config == {"mime_type": "video/mp4"}


def test_upload_rejects_path_and_manifest_protocol_before_client_call(
    adapter: Any, fake_client: FakeClient
) -> None:
    arbitrary_path = Path("/private/original.mp4")
    path_manifest = SimpleNamespace(
        manifest_id="manifest-original",
        path=arbitrary_path,
    )

    for unauthorized in (arbitrary_path, path_manifest):
        with pytest.raises((TypeError, VideoEditorError)):
            adapter.upload(unauthorized)

    assert fake_client.files.upload_calls == 0


def test_waits_from_processing_to_active_then_deletes_upload(
    adapter: Any, fake_client: FakeClient
) -> None:
    fake_client.files.get_responses.extend(
        [
            FakeFiles.response(state="PROCESSING"),
            FakeFiles.response(state="ACTIVE"),
        ]
    )

    upload = upload_active(adapter)
    active = adapter.wait_until_active(upload)
    adapter.delete_upload(active)

    assert active.state == "ACTIVE"
    assert fake_client.files.get_calls == 2
    assert fake_client.files.deleted == ["files/proxy-1"]


def test_failed_file_processing_is_terminal_and_never_generates_content(
    adapter: Any, fake_client: FakeClient
) -> None:
    upload = upload_active(adapter)
    fake_client.files.get_responses.append(FakeFiles.response(state="FAILED"))

    with pytest.raises(VideoEditorError, match="processing") as caught:
        adapter.wait_until_active(upload)

    assert caught.value.code == "provider_file_failed"
    assert fake_client.models.calls == 0


@pytest.mark.parametrize(
    "remote_response",
    [
        FakeFiles.response(expiration_time=datetime.now(UTC) - timedelta(seconds=1)),
        errors.ClientError(404, {"error": {"message": "not found"}}),
    ],
)
def test_expired_or_missing_file_requests_authorized_reupload(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    remote_response: object,
) -> None:
    upload = upload_active(adapter)
    fake_client.files.get_responses.append(remote_response)

    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")

    assert caught.value.code == "provider_file_reupload_required"
    assert caught.value.safe_details == {"manifest_id": "manifest-1"}
    assert fake_client.files.upload_calls == 1
    assert fake_client.models.calls == 0


def test_broad_request_uses_exact_model_static_metadata_and_strict_schema(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(queued_response("broad-response.json"))

    result = adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")

    assert BroadScanResponse is not None
    request = fake_client.models.requests[0]
    part = request_video_part(request)
    config = request["config"]
    assert request["model"] == "gemini-2.5-flash"
    assert part.video_metadata.fps == 0.5
    assert part.video_metadata.start_offset == "0s"
    assert part.video_metadata.end_offset == "6s"
    assert config.response_mime_type == "application/json"
    assert config.response_schema is BroadScanResponse
    assert isinstance(result.response, BroadScanResponse)
    assert result.response.chunk_id == broad_chunk.chunk_id


def test_broad_prompt_is_versioned_bounded_and_forbids_semantic_expansion(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(queued_response("broad-response.json"))

    adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")

    prompt = request_prompt(fake_client.models.requests[0])
    for required in (
        "mode: broad",
        "prompt_version: broad-v1",
        "chunk_id: chunk-1",
        "interval: [0, 6] seconds",
        "Return only JSON",
        "Do not transcribe speech",
        "Do not infer speech meaning",
        "Do not identify any person",
    ):
        assert required in prompt


@pytest.mark.parametrize("fps", [2, 3, 5])
def test_candidate_request_uses_bounded_fps_and_duration_string_offsets(
    adapter: Any,
    fake_client: FakeClient,
    candidate: object,
    fps: int,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(queued_response("refinement-response.json"))

    result = adapter.refine_candidate(
        upload,
        candidate,
        fps=fps,
        prompt_version="candidate-v1",
    )

    assert CandidateRefinementResponse is not None
    request = fake_client.models.requests[0]
    part = request_video_part(request)
    assert request["model"] == "gemini-2.5-flash"
    assert part.video_metadata.fps == float(fps)
    assert part.video_metadata.start_offset == "1s"
    assert part.video_metadata.end_offset == "3.5s"
    assert request["config"].response_schema is CandidateRefinementResponse
    assert result.response.candidate_id == "candidate-1"


def test_candidate_prompt_is_versioned_and_bounds_speech_meaning(
    adapter: Any,
    fake_client: FakeClient,
    candidate: object,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(queued_response("refinement-response.json"))

    adapter.refine_candidate(
        upload,
        candidate,
        fps=3,
        prompt_version="candidate-v1",
    )

    prompt = request_prompt(fake_client.models.requests[0])
    for required in (
        "mode: candidate",
        "prompt_version: candidate-v1",
        "chunk_id: chunk-1",
        "candidate_id: candidate-1",
        "interval: [1, 3.5] seconds",
        "Return only JSON",
        "Speech meaning may be summarized only for this candidate interval",
        "Do not identify any person",
    ):
        assert required in prompt


@pytest.mark.parametrize(
    ("operation", "prompt_version"),
    [("broad", "broad-v2"), ("candidate", "candidate-v2")],
)
def test_unknown_prompt_version_is_rejected_before_generation(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    candidate: object,
    operation: str,
    prompt_version: str,
) -> None:
    upload = upload_active(adapter)

    with pytest.raises(VideoEditorError) as caught:
        if operation == "broad":
            adapter.broad_scan(upload, broad_chunk, prompt_version=prompt_version)
        else:
            adapter.refine_candidate(
                upload,
                candidate,
                fps=3,
                prompt_version=prompt_version,
            )

    assert caught.value.code == "provider_invalid_request"
    assert fake_client.models.calls == 0


def test_semantic_response_failure_receives_one_repair_only(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.extend(
        [
            queued_response("malformed-response.json"),
            queued_response("malformed-response.json"),
        ]
    )

    with pytest.raises(VideoEditorError, match="chunk|timestamp") as caught:
        adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")

    assert caught.value.code == "provider_response_invalid"
    assert fake_client.models.calls == 2


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
        (
            errors.ClientError(401, {"error": {"message": "bad credentials"}}),
            "provider_authentication",
            1,
        ),
        (
            errors.ClientError(400, {"error": {"message": "invalid request"}}),
            "provider_invalid_request",
            1,
        ),
        (
            errors.ClientError(429, {"error": {"message": "rate limit"}}),
            "provider_rate_limited",
            3,
        ),
        (
            errors.ServerError(503, {"error": {"message": "unavailable"}}),
            "provider_unavailable",
            3,
        ),
        (OSError("offline network"), "provider_network", 3),
    ],
)
def test_retry_policy_retries_only_transient_failures(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    error: BaseException,
    expected_code: str,
    expected_calls: int,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.extend([error] * expected_calls)

    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")

    assert caught.value.code == expected_code
    assert fake_client.models.calls == expected_calls


@pytest.mark.parametrize("operation", ["upload", "get", "delete"])
def test_non_generation_sdk_calls_share_bounded_retry_policy(
    adapter: Any,
    fake_client: FakeClient,
    operation: str,
) -> None:
    transient = errors.ServerError(503, {"error": {"message": "unavailable"}})
    if operation == "upload":
        fake_client.files.upload_responses.extend(
            [transient, transient, FakeFiles.response(state="PROCESSING")]
        )
        adapter.upload(AuthorizedUpload("manifest-1", io.BytesIO(b"proxy")))
        assert fake_client.files.upload_calls == 3
    elif operation == "get":
        upload = upload_active(adapter)
        fake_client.files.get_responses.extend(
            [transient, transient, FakeFiles.response(state="ACTIVE")]
        )
        adapter.wait_until_active(upload)
        assert fake_client.files.get_calls == 3
    else:
        upload = upload_active(adapter)
        fake_client.files.delete_responses.extend([transient, transient, None])
        adapter.delete_upload(upload)
        assert fake_client.files.delete_calls == 3


@pytest.mark.parametrize("operation", ["upload", "get", "delete"])
def test_non_generation_sdk_calls_do_not_retry_invalid_requests(
    adapter: Any,
    fake_client: FakeClient,
    operation: str,
) -> None:
    invalid = errors.ClientError(400, {"error": {"message": "invalid"}})
    if operation == "upload":
        fake_client.files.upload_responses.append(invalid)
        invoke = lambda: adapter.upload(
            AuthorizedUpload("manifest-1", io.BytesIO(b"proxy"))
        )
        calls = lambda: fake_client.files.upload_calls
    elif operation == "get":
        upload = upload_active(adapter)
        fake_client.files.get_responses.append(invalid)
        invoke = lambda: adapter.wait_until_active(upload)
        calls = lambda: fake_client.files.get_calls
    else:
        upload = upload_active(adapter)
        fake_client.files.delete_responses.append(invalid)
        invoke = lambda: adapter.delete_upload(upload)
        calls = lambda: fake_client.files.delete_calls

    with pytest.raises(VideoEditorError) as caught:
        invoke()

    assert caught.value.code == "provider_invalid_request"
    assert calls() == 1


@pytest.mark.parametrize(
    ("response_file", "field_path", "invalid_value"),
    [
        ("broad-response.json", ("scenes", 0, "confidence"), "0.95"),
        ("broad-response.json", ("scenes", 0, "story_milestone"), "false"),
        ("broad-response.json", ("scenes", 0, "scenic_interest"), True),
        ("refinement-response.json", ("confidence",), math.inf),
    ],
)
def test_provider_response_rejects_coercive_and_nonfinite_scalars(
    gemini_contract: None,
    response_file: str,
    field_path: tuple[str | int, ...],
    invalid_value: object,
) -> None:
    payload = fixture_payload(response_file)
    target: Any = payload
    for component in field_path[:-1]:
        target = target[component]
    target[field_path[-1]] = invalid_value
    response_model = (
        BroadScanResponse
        if response_file == "broad-response.json"
        else CandidateRefinementResponse
    )
    assert response_model is not None

    with pytest.raises(ValidationError):
        response_model.model_validate(payload)


def test_provider_response_requires_decimal_timestamps_as_json_strings(
    gemini_contract: None,
) -> None:
    assert BroadScanResponse is not None
    payload = fixture_payload("broad-response.json")
    payload["scenes"][0]["start"] = 0

    with pytest.raises(ValidationError):
        BroadScanResponse.model_validate(payload)

    payload["scenes"][0]["start"] = "0"
    assert BroadScanResponse.model_validate(payload).scenes[0].start == Decimal(0)


def test_malformed_structured_response_receives_one_schema_repair_only(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
) -> None:
    upload = upload_active(adapter)
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
    broad_chunk: object,
) -> None:
    upload = upload_active(adapter)
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
    sdk_error = errors.ClientError(
        401,
        {"error": {"message": f"failed {SECRET}"}},
    )
    fake_client.models.responses.append(sdk_error)
    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")
    serialized = {
        "category": caught.value.category.value,
        "code": caught.value.code,
        "details": caught.value.safe_details,
        "message": str(caught.value),
    }
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)
    assert SECRET not in repr(serialized)
    assert repr(sdk_error) not in repr(caught.value)
    assert repr(sdk_error) not in repr(serialized)


@pytest.mark.gemini_live
@pytest.mark.skipif(
    not os.getenv("RUN_GEMINI_LIVE_TESTS"),
    reason="explicit opt-in required; default suite is offline",
)
def test_live_broad_scan_preflights_maximum_cost_before_upload(
    gemini_contract: None,
    upload_authorization: AuthorizedUpload,
    broad_chunk: object,
) -> None:
    assert GeminiAdapter is not None
    maximum = GeminiAdapter.estimate_broad_request_maximum(
        upload_authorization,
        broad_chunk,
    )
    assert maximum <= Decimal("0.01")

    adapter = GeminiAdapter.from_environment(maximum_request_cost_usd=maximum)
    upload = adapter.upload(upload_authorization)
    result = adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")

    assert result.response.chunk_id == "chunk-1"
    assert result.usage.actual_cost_usd <= maximum
