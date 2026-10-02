"""Offline RED contract for Gemini Files and structured analysis responses."""

from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google import genai
from google.genai import errors, models, types
from pydantic import ValidationError

from video_editor.analysis.models import AnalysisRequestContext, RequestReservation
from video_editor.analysis.pricing import ModelPricing
from video_editor.analysis.proxy_chunks import (
    AuthorizedUpload,
    ProxyManifest,
    create_cloud_proxy_chunk,
    register_proxy_manifest,
    validate_upload_candidate,
)
from video_editor.config import PathSettings
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.discovery import IDENTITY_VERSION, bounded_fingerprint
from video_editor.media.proxies import ProxyMapping
from video_editor.persistence.database import JobStore

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
    usage_metadata: types.GenerateContentResponseUsageMetadata = field(
        default_factory=lambda: types.GenerateContentResponseUsageMetadata(
            prompt_token_count=11,
            candidates_token_count=17,
            thoughts_token_count=3,
            total_token_count=31,
            prompt_tokens_details=[
                types.ModalityTokenCount(modality="VIDEO", token_count=7),
                types.ModalityTokenCount(modality="TEXT", token_count=4),
            ],
        )
    )

    @property
    def text(self) -> str:
        raise AssertionError("adapter must use response.parsed, never response.text")


class FakeFiles:
    def __init__(self) -> None:
        self.uploads: list[tuple[object, object]] = []
        self.uploaded_bytes: list[bytes] = []
        self.upload_file_descriptors: list[int | None] = []
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
        fileno = getattr(file, "fileno", None)
        try:
            self.upload_file_descriptors.append(fileno() if callable(fileno) else None)
        except (OSError, io.UnsupportedOperation):
            self.upload_file_descriptors.append(None)
        read = getattr(file, "read", None)
        if not callable(read):
            raise TypeError("upload file must be readable")
        self.uploaded_bytes.append(read())
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
        pricing=_test_pricing(),
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


@pytest.fixture
def broad_request() -> AnalysisRequestContext:
    return AnalysisRequestContext(
        reservation_id="reservation-broad-1",
        job_id="job-1",
        manifest_id="manifest-1",
        mode="broad",
        chunk_id="chunk-1",
    )


@pytest.fixture
def candidate_request() -> AnalysisRequestContext:
    return AnalysisRequestContext(
        reservation_id="reservation-candidate-1",
        job_id="job-1",
        manifest_id="manifest-1",
        mode="candidate",
        chunk_id="chunk-1",
        candidate_id="candidate-1",
    )


def fixture_payload(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text())


def queued_response(name: str) -> ParsedResponse:
    return ParsedResponse(parsed=fixture_payload(name))


def _test_pricing() -> ModelPricing:
    return ModelPricing(
        model="gemini-3.8-flash",
        media_input_usd_per_million_tokens=Decimal("0.30"),
        audio_input_usd_per_million_tokens=Decimal("1.00"),
        text_input_usd_per_million_tokens=Decimal("0.10"),
        output_usd_per_million_tokens=Decimal("2.50"),
        source_url="https://example.invalid/pinned-task-2-pricing",
        effective_date=date(2026, 9, 28),
    )


def _paths(tmp_path: Path) -> PathSettings:
    return PathSettings(
        input_dir=tmp_path / "input",
        workspace_dir=tmp_path / "workspace",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
    )


def _register_tiny_proxy(
    tmp_path: Path,
    store: JobStore,
    *,
    job_id: str,
) -> tuple[ProxyManifest, object]:
    paths = _paths(tmp_path)
    source = paths.input_dir / "live-source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x36:r=15",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "1",
            "-shortest",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    fingerprint = bounded_fingerprint(source)
    source_identity = f"{IDENTITY_VERSION}:{fingerprint}"
    mapping = ProxyMapping(
        source_id="live-source-1",
        source_start=Decimal(0),
        source_end=Decimal(1),
        proxy_start=Decimal(0),
        proxy_end=Decimal(1),
        source_identity=source_identity,
        settings_hash="live-settings",
        tool_version="live-contract",
    )
    store.save_sources(
        job_id,
        [
            {
                "source_id": "live-source-1",
                "path": str(source),
                "size_bytes": source.stat().st_size,
                "fingerprint": fingerprint,
                "identity_version": IDENTITY_VERSION,
            }
        ],
    )
    phase1_proxy = paths.cache_dir / "live-source-1.phase1.proxy.mp4"
    phase1_proxy.parent.mkdir(parents=True, exist_ok=True)
    phase1_proxy.write_bytes(b"phase1 proxy placeholder")
    mapping_data = {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in mapping.__dict__.items()
    }
    store.save_artifact(
        job_id,
        "proxy",
        phase1_proxy,
        {
            "kind": "proxy",
            "source_id": "live-source-1",
            "source_path": str(source),
            "source_identity": source_identity,
            "settings_hash": mapping.settings_hash,
            "tool_version": mapping.tool_version,
            "mapping": mapping_data,
        },
    )
    manifest = create_cloud_proxy_chunk(
        source,
        paths.cache_dir / job_id,
        mapping,
        Decimal(0),
        Decimal(1),
        job_id=job_id,
        source_fingerprint=fingerprint,
        paths=paths,
        store=store,
    )
    register_proxy_manifest(manifest, store)
    chunk = SimpleNamespace(
        chunk_id=manifest.chunk_id,
        proxy_start=manifest.data.proxy_start,
        proxy_end=manifest.data.proxy_end,
    )
    return manifest, chunk


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


def test_upload_retry_rewinds_same_authorized_stream_to_start_offset(
    adapter: Any,
    fake_client: FakeClient,
    tmp_path: Path,
) -> None:
    transient = errors.ServerError(503, {"error": {"message": "unavailable"}})
    fake_client.files.upload_responses.extend(
        [transient, FakeFiles.response(state="PROCESSING")]
    )
    proxy = tmp_path / "authorized-proxy.mp4"
    proxy.write_bytes(b"prefix-complete proxy bytes")
    stream = proxy.open("rb")
    stream.seek(len(b"prefix-"))
    descriptor = stream.fileno()

    adapter.upload(AuthorizedUpload("manifest-1", stream))

    assert fake_client.files.upload_calls == 2
    assert fake_client.files.uploaded_bytes == [b"complete proxy bytes"] * 2
    assert fake_client.files.uploads[0][0] is fake_client.files.uploads[1][0]
    assert fake_client.files.upload_file_descriptors == [descriptor, descriptor]
    assert stream.closed


class NonSeekableStream(io.BytesIO):
    def seekable(self) -> bool:
        return False


class MispositioningStream(io.BytesIO):
    def seek(self, offset: int, whence: int = 0) -> int:
        super().seek(offset + 1, whence)
        return self.tell()


@pytest.mark.parametrize(
    "stream",
    [NonSeekableStream(b"proxy"), MispositioningStream(b"proxy")],
)
def test_upload_fails_closed_for_unrewindable_stream(
    adapter: Any,
    fake_client: FakeClient,
    stream: io.BytesIO,
) -> None:
    with pytest.raises(VideoEditorError) as caught:
        adapter.upload(AuthorizedUpload("manifest-1", stream))

    assert caught.value.code == "provider_invalid_upload_stream"
    assert fake_client.files.upload_calls == 0
    assert stream.closed


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
    broad_request: AnalysisRequestContext,
    remote_response: object,
) -> None:
    upload = upload_active(adapter)
    fake_client.files.get_responses.append(remote_response)

    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(
            upload, broad_chunk, broad_request, prompt_version="broad-v1"
        )

    assert caught.value.code == "provider_file_reupload_required"
    assert caught.value.safe_details == {"manifest_id": "manifest-1"}
    assert fake_client.files.upload_calls == 1
    assert fake_client.models.calls == 0


def test_broad_request_uses_exact_model_static_metadata_and_strict_schema(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(queued_response("broad-response.json"))

    result = adapter.broad_scan(
        upload, broad_chunk, broad_request, prompt_version="broad-v1"
    )

    assert BroadScanResponse is not None
    request = fake_client.models.requests[0]
    part = request_video_part(request)
    config = request["config"]
    assert request["model"] == "gemini-3.8-flash"
    assert part.video_metadata.fps == 0.5
    assert part.video_metadata.start_offset == "0s"
    assert part.video_metadata.end_offset == "6s"
    assert config.response_mime_type == "application/json"
    assert isinstance(config.response_schema, types.Schema)
    assert config.response_schema.title == "BroadScanResponse"
    assert set(config.response_schema.properties) == set(BroadScanResponse.model_fields)
    assert isinstance(result.response, BroadScanResponse)
    assert result.response.chunk_id == broad_chunk.chunk_id


@pytest.mark.parametrize(
    "response_model",
    [BroadScanResponse, CandidateRefinementResponse],
)
def test_locked_sdk_prepares_string_only_timestamp_response_schema(
    gemini_contract: None,
    response_model: object,
) -> None:
    assert response_model is not None
    client = genai.Client(api_key=SECRET)
    try:
        transformed = models._GenerateContentConfig_to_mldev(
            client._api_client,
            types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=response_model,
            ),
            {},
            None,
        )
    finally:
        client.close()

    response_schema = transformed["responseSchema"].model_dump(
        by_alias=True,
        exclude_none=True,
    )
    timestamp_schemas: list[dict[str, object]] = []

    def collect_timestamps(value: object) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                for name in ("start", "end"):
                    timestamp = properties.get(name)
                    if isinstance(timestamp, dict):
                        timestamp_schemas.append(timestamp)
            for child in value.values():
                collect_timestamps(child)
        elif isinstance(value, list):
            for child in value:
                collect_timestamps(child)

    collect_timestamps(response_schema)
    assert timestamp_schemas
    assert all(schema.get("type") == "STRING" for schema in timestamp_schemas)
    assert all("anyOf" not in schema for schema in timestamp_schemas)
    assert all("exclusiveMinimum" not in schema for schema in timestamp_schemas)


def _wire_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys |= _wire_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            keys |= _wire_keys(child)
    elif hasattr(value, "model_dump"):
        keys |= _wire_keys(value.model_dump(exclude_none=True))
    return keys


def test_outgoing_broad_request_has_no_unsupported_additional_properties(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(queued_response("broad-response.json"))

    adapter.broad_scan(upload, broad_chunk, broad_request, prompt_version="broad-v1")

    wire_keys = _outgoing_wire_keys(fake_client.models.requests[0])
    assert "additional_properties" not in wire_keys
    assert "additionalProperties" not in wire_keys


def test_outgoing_candidate_request_has_no_unsupported_additional_properties(
    adapter: Any,
    fake_client: FakeClient,
    candidate: object,
    candidate_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(queued_response("refinement-response.json"))

    adapter.refine_candidate(
        upload, candidate, candidate_request, fps=3, prompt_version="candidate-v2"
    )

    wire_keys = _outgoing_wire_keys(fake_client.models.requests[0])
    assert "additional_properties" not in wire_keys
    assert "additionalProperties" not in wire_keys


def _timestamp_descriptions(schema: object) -> list[str | None]:
    found: list[str | None] = []
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name in ("start", "end", "time"):
                if isinstance(properties.get(name), dict):
                    found.append(properties[name].get("description"))
        for child in schema.values():
            found += _timestamp_descriptions(child)
    elif isinstance(schema, list):
        for child in schema:
            found += _timestamp_descriptions(child)
    return found


@pytest.mark.parametrize("model_name", ["broad", "candidate"])
def test_outgoing_schema_tells_model_how_to_write_timestamps(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
    candidate: object,
    candidate_request: AnalysisRequestContext,
    model_name: str,
) -> None:
    upload = upload_active(adapter)
    if model_name == "broad":
        fake_client.models.responses.append(queued_response("broad-response.json"))
        adapter.broad_scan(
            upload, broad_chunk, broad_request, prompt_version="broad-v1"
        )
    else:
        fake_client.models.responses.append(queued_response("refinement-response.json"))
        adapter.refine_candidate(
            upload, candidate, candidate_request, fps=3, prompt_version="candidate-v2"
        )

    schema = fake_client.models.requests[0]["config"].response_schema
    descriptions = _timestamp_descriptions(schema.model_dump(exclude_none=True))

    assert descriptions
    assert all(
        isinstance(text, str) and "decimal seconds" in text and "no units" in text
        for text in descriptions
    )


def _outgoing_wire_keys(request: dict[str, object]) -> set[str]:
    client = genai.Client(api_key=SECRET)
    try:
        params = types._GenerateContentParameters(
            model=request["model"],
            contents=request["contents"],
            config=request["config"],
        )
        wire = models._GenerateContentParameters_to_mldev(client._api_client, params)
    finally:
        client.close()
    return _wire_keys(wire)


def test_broad_prompt_is_versioned_bounded_and_forbids_semantic_expansion(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(queued_response("broad-response.json"))

    adapter.broad_scan(upload, broad_chunk, broad_request, prompt_version="broad-v1")

    prompt = request_prompt(fake_client.models.requests[0])
    for required in (
        "mode: broad",
        "prompt_version: broad-v1",
        "reservation_id: reservation-broad-1",
        "job_id: job-1",
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
    candidate_request: AnalysisRequestContext,
    fps: int,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(queued_response("refinement-response.json"))

    result = adapter.refine_candidate(
        upload,
        candidate,
        candidate_request,
        fps=fps,
        prompt_version="candidate-v2",
    )

    assert CandidateRefinementResponse is not None
    request = fake_client.models.requests[0]
    part = request_video_part(request)
    assert request["model"] == "gemini-3.8-flash"
    assert part.video_metadata.fps == float(fps)
    assert part.video_metadata.start_offset == "1s"
    assert part.video_metadata.end_offset == "3.5s"
    schema = request["config"].response_schema
    assert isinstance(schema, types.Schema)
    assert schema.title == "CandidateRefinementResponse"
    assert set(schema.properties) == set(CandidateRefinementResponse.model_fields)
    assert result.response.candidate_id == "candidate-1"


def test_candidate_prompt_is_versioned_and_bounds_speech_meaning(
    adapter: Any,
    fake_client: FakeClient,
    candidate: object,
    candidate_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(queued_response("refinement-response.json"))

    adapter.refine_candidate(
        upload,
        candidate,
        candidate_request,
        fps=3,
        prompt_version="candidate-v2",
    )

    prompt = request_prompt(fake_client.models.requests[0])
    for required in (
        "mode: candidate",
        "prompt_version: candidate-v2",
        "reservation_id: reservation-candidate-1",
        "job_id: job-1",
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
    [("broad", "broad-v2"), ("candidate", "candidate-v1")],
)
def test_unknown_prompt_version_is_rejected_before_generation(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    candidate: object,
    broad_request: AnalysisRequestContext,
    candidate_request: AnalysisRequestContext,
    operation: str,
    prompt_version: str,
) -> None:
    upload = upload_active(adapter)

    with pytest.raises(VideoEditorError) as caught:
        if operation == "broad":
            adapter.broad_scan(
                upload, broad_chunk, broad_request, prompt_version=prompt_version
            )
        else:
            adapter.refine_candidate(
                upload,
                candidate,
                candidate_request,
                fps=3,
                prompt_version=prompt_version,
            )

    assert caught.value.code == "provider_invalid_request"
    assert fake_client.models.calls == 0


def test_generation_refuses_missing_or_mismatched_reservation_before_provider_call(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    mismatched = broad_request.model_copy(update={"chunk_id": "other-chunk"})

    with pytest.raises(TypeError):
        adapter.broad_scan(upload, broad_chunk, prompt_version="broad-v1")
    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(upload, broad_chunk, mismatched, prompt_version="broad-v1")

    assert caught.value.code == "reservation_identity_mismatch"
    assert fake_client.models.calls == 0


def test_semantic_response_failure_receives_one_repair_only(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.extend(
        [
            queued_response("malformed-response.json"),
            queued_response("malformed-response.json"),
        ]
    )

    with pytest.raises(VideoEditorError, match="chunk|timestamp") as caught:
        adapter.broad_scan(
            upload, broad_chunk, broad_request, prompt_version="broad-v1"
        )

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
    broad_request: AnalysisRequestContext,
    error: BaseException,
    expected_code: str,
    expected_calls: int,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.extend([error] * expected_calls)

    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(
            upload, broad_chunk, broad_request, prompt_version="broad-v1"
        )

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


def test_total_generation_attempt_cap_includes_transient_retry_and_repair(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    transient = errors.ServerError(503, {"error": {"message": "unavailable"}})
    fake_client.models.responses.extend(
        [
            transient,
            ParsedResponse(
                parsed={"schema_version": "broad-v1"},
                response_id="request-malformed",
            ),
            queued_response("broad-response.json"),
        ]
    )
    upload = upload_active(adapter)

    result = adapter.broad_scan(
        upload, broad_chunk, broad_request, prompt_version="broad-v1"
    )

    assert fake_client.models.calls == 3
    assert result.usage.request_ids == ("unknown", "request-malformed", "request-123")
    assert result.usage.prompt_tokens == 22
    assert result.usage.candidates_tokens == 34
    assert result.usage.thoughts_tokens == 6
    assert result.usage.output_tokens == 40
    assert result.usage.total_tokens == 62
    assert result.usage.has_unknown_billing
    assert result.usage.actual_cost_usd is None


def test_malformed_structured_response_receives_one_schema_repair_only(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.extend(
        [
            ParsedResponse(parsed={"schema_version": "broad-v1"}),
            queued_response("broad-response.json"),
        ]
    )

    result = adapter.broad_scan(
        upload, broad_chunk, broad_request, prompt_version="broad-v1"
    )

    assert result.response.chunk_id == "chunk-1"
    assert fake_client.models.calls == 2
    assert (
        fake_client.models.requests[1]["config"].response_mime_type
        == "application/json"
    )


def test_terminal_provider_error_records_unknown_billing_attempt(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    http_response = httpx.Response(
        400,
        headers={"x-goog-request-id": "http-request-456"},
    )
    failure = errors.ClientError(
        400,
        {"error": {"message": "invalid request"}},
        response=http_response,
    )
    assert failure.response is http_response
    assert not hasattr(failure.response, "usage_metadata")
    fake_client.models.responses.append(failure)
    upload = upload_active(adapter)

    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(
            upload, broad_chunk, broad_request, prompt_version="broad-v1"
        )

    usage = json.loads(caught.value.safe_details["usage"])
    assert usage["reservation_id"] == "reservation-broad-1"
    assert usage["request_ids"] == ["http-request-456"]
    assert usage["attempts"] == [
        {
            "request_id": "http-request-456",
            "status": "failed_unknown_billing",
            "prompt_tokens": None,
            "media_input_tokens": None,
            "audio_input_tokens": None,
            "text_input_tokens": None,
            "candidates_tokens": None,
            "thoughts_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }
    ]
    assert usage["has_unknown_billing"] is True
    assert usage["actual_cost_usd"] is None


def test_broad_estimate_prices_audio_at_separate_rate(broad_chunk: object) -> None:
    assert GeminiAdapter is not None
    pricing = ModelPricing(
        model="gemini-3.8-flash",
        media_input_usd_per_million_tokens=Decimal("0.30"),
        audio_input_usd_per_million_tokens=Decimal("1.00"),
        text_input_usd_per_million_tokens=Decimal("0.30"),
        output_usd_per_million_tokens=Decimal(0),
        source_url="https://example.invalid/separate-audio-test",
        effective_date=date(2026, 10, 2),
    )
    with AuthorizedUpload("manifest-1", io.BytesIO(b"proxy")) as authorization:
        bound = GeminiAdapter.estimate_broad_request_maximum(
            authorization, broad_chunk, pricing
        )
    video = 3 * 258 + 6 * 64
    audio = 6 * 32
    minimum = (
        Decimal(3)
        * (Decimal(video) * Decimal("0.30") + Decimal(audio) * Decimal("1.00"))
        / Decimal(1_000_000)
    )
    assert bound >= minimum


def test_audio_usage_is_billed_at_audio_rate(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    assert GeminiAdapter is not None
    pricing = ModelPricing(
        model="gemini-3.8-flash",
        media_input_usd_per_million_tokens=Decimal("0.30"),
        audio_input_usd_per_million_tokens=Decimal("1.00"),
        text_input_usd_per_million_tokens=Decimal("0.30"),
        output_usd_per_million_tokens=Decimal(0),
        source_url="https://example.invalid/separate-audio-test",
        effective_date=date(2026, 10, 2),
    )
    adapter = GeminiAdapter(fake_client, pricing=pricing, sleeper=lambda _: None)
    upload = upload_active(adapter)
    response = ParsedResponse(parsed=fixture_payload("broad-response.json"))
    response.usage_metadata = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=42,
        candidates_token_count=0,
        thoughts_token_count=0,
        total_token_count=42,
        prompt_tokens_details=[
            types.ModalityTokenCount(modality="VIDEO", token_count=10),
            types.ModalityTokenCount(modality="AUDIO", token_count=30),
            types.ModalityTokenCount(modality="TEXT", token_count=2),
        ],
    )
    fake_client.models.responses.append(response)
    result = adapter.broad_scan(
        upload, broad_chunk, broad_request, prompt_version="broad-v1"
    )
    assert result.usage.actual_cost_usd == Decimal("0.000034")


def test_broad_estimate_covers_video_audio_output_and_all_attempts(
    broad_chunk: object,
) -> None:
    assert GeminiAdapter is not None
    with AuthorizedUpload("manifest-1", io.BytesIO(b"proxy")) as authorization:
        maximum = GeminiAdapter.estimate_broad_request_maximum(
            authorization, broad_chunk, _test_pricing()
        )

    # Six seconds at 0.5 FPS: three frames and six seconds of audio.
    media_tokens = 3 * 258 + 6 * 32
    minimum_per_attempt = (
        Decimal(media_tokens) * _test_pricing().media_input_usd_per_million_tokens
        + Decimal(8192) * _test_pricing().output_usd_per_million_tokens
    ) / Decimal(1_000_000)
    assert maximum >= minimum_per_attempt * 3


def test_candidate_estimate_uses_five_fps_over_its_interval() -> None:
    assert GeminiAdapter is not None
    candidate_interval = SimpleNamespace(
        chunk_id="chunk-1", proxy_start=Decimal(1), proxy_end=Decimal("3.5")
    )
    adapter = GeminiAdapter(FakeClient(), pricing=_test_pricing())

    maximum = adapter.maximum_request_cost(
        "manifest-1", candidate_interval, prompt_version="candidate-v2"
    )

    # Ceiling of 2.5 seconds at 5 FPS is 13 frames, not broad 0.5 FPS.
    media_tokens = 13 * 258 + 80
    minimum_per_attempt = (
        Decimal(media_tokens) * _test_pricing().media_input_usd_per_million_tokens
        + Decimal(8192) * _test_pricing().output_usd_per_million_tokens
    ) / Decimal(1_000_000)
    assert maximum >= minimum_per_attempt * 3


def test_estimate_scales_with_total_retry_and_repair_attempt_budget(
    broad_chunk: object,
) -> None:
    assert GeminiAdapter is not None
    two_attempts = GeminiAdapter(
        FakeClient(), retry_policy=RetryPolicy(max_attempts=2), pricing=_test_pricing()
    )
    four_attempts = GeminiAdapter(
        FakeClient(), retry_policy=RetryPolicy(max_attempts=4), pricing=_test_pricing()
    )

    first = two_attempts.maximum_request_cost(
        "manifest-1", broad_chunk, prompt_version="broad-v1"
    )
    second = four_attempts.maximum_request_cost(
        "manifest-1", broad_chunk, prompt_version="broad-v1"
    )

    assert second >= first * 2 - Decimal("0.000001")


def test_generation_caps_output_across_broad_retry_and_schema_repair(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.extend(
        [
            errors.ServerError(503, {"error": {"message": "unavailable"}}),
            queued_response("malformed-response.json"),
            queued_response("broad-response.json"),
        ]
    )

    adapter.broad_scan(upload, broad_chunk, broad_request, prompt_version="broad-v1")

    assert fake_client.models.calls == 3
    assert [
        request["config"].max_output_tokens for request in fake_client.models.requests
    ] == [8192, 8192, 8192]


def test_candidate_generation_has_same_output_cap(
    adapter: Any,
    fake_client: FakeClient,
    candidate: object,
    candidate_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(queued_response("refinement-response.json"))

    adapter.refine_candidate(
        upload, candidate, candidate_request, fps=5, prompt_version="candidate-v2"
    )

    assert fake_client.models.requests[0]["config"].max_output_tokens == 8192


def test_oversized_prompt_identity_never_dispatches_generation(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    oversized = broad_request.model_copy(update={"job_id": "x" * 4096})

    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(upload, broad_chunk, oversized, prompt_version="broad-v1")

    assert caught.value.code == "provider_invalid_request"
    assert fake_client.models.calls == 0


def test_estimate_includes_response_schema_allowance(broad_chunk: object) -> None:
    assert GeminiAdapter is not None
    pricing = ModelPricing(
        model="gemini-3.8-flash",
        media_input_usd_per_million_tokens=Decimal(0),
        audio_input_usd_per_million_tokens=Decimal(0),
        text_input_usd_per_million_tokens=Decimal(1),
        output_usd_per_million_tokens=Decimal(0),
        source_url="https://example.invalid/fixture",
        effective_date=date(2026, 9, 28),
    )
    schema_bytes = len(json.dumps(BroadScanResponse.model_json_schema()).encode())
    with AuthorizedUpload("manifest-1", io.BytesIO(b"proxy")) as authorization:
        maximum = GeminiAdapter.estimate_broad_request_maximum(
            authorization, broad_chunk, pricing
        )

    assert maximum >= Decimal(3 * (1024 + schema_bytes)) / Decimal(1_000_000)


def test_live_contract_preflight_rejects_before_upload(
    fake_client: FakeClient,
    broad_chunk: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert GeminiAdapter is not None
    authorization = AuthorizedUpload("manifest-1", io.BytesIO(b"proxy"))
    expensive = ModelPricing(
        model="gemini-3.8-flash",
        media_input_usd_per_million_tokens=Decimal(10000),
        audio_input_usd_per_million_tokens=Decimal(10000),
        text_input_usd_per_million_tokens=Decimal(10000),
        output_usd_per_million_tokens=Decimal(10000),
        source_url="https://example.invalid/expensive-test-pricing",
        effective_date=date(2026, 9, 28),
    )

    def fail_before_upload(reason: str) -> None:
        raise VideoEditorError(
            ErrorCategory.BUDGET,
            reason,
            code="live_preflight_exceeded",
        )

    monkeypatch.setattr(pytest, "skip", fail_before_upload)
    maximum = GeminiAdapter.estimate_broad_request_maximum(
        authorization,
        broad_chunk,
        expensive,
    )

    with pytest.raises(VideoEditorError, match="exceeds USD 0.01"):
        if maximum > Decimal("0.01"):
            pytest.skip("computed live request maximum exceeds USD 0.01")
        GeminiAdapter(fake_client, pricing=expensive).upload(authorization)

    assert fake_client.files.upload_calls == 0


def test_remote_upload_cleanup_runs_from_finally_on_analysis_failure(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    fake_client.models.responses.append(
        errors.ClientError(400, {"error": {"message": "invalid request"}})
    )
    upload = upload_active(adapter)

    with pytest.raises(VideoEditorError):
        try:
            adapter.broad_scan(
                upload, broad_chunk, broad_request, prompt_version="broad-v1"
            )
        finally:
            adapter.delete_upload(upload)

    assert fake_client.files.deleted == ["files/proxy-1"]


def test_successful_response_without_usage_metadata_is_unknown_billing(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(
        ParsedResponse(
            parsed=fixture_payload("broad-response.json"),
            response_id="request-no-usage",
            usage_metadata=None,  # type: ignore[arg-type] - SDK response can omit output metadata
        )
    )

    result = adapter.broad_scan(
        upload, broad_chunk, broad_request, prompt_version="broad-v1"
    )

    assert result.usage.request_ids == ("request-no-usage",)
    assert result.usage.attempts[0].status == "failed_unknown_billing"
    assert result.usage.attempts[0].prompt_tokens is None
    assert result.usage.has_unknown_billing
    assert result.usage.actual_cost_usd is None


def test_unknown_modality_keeps_billing_unknown(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    response = ParsedResponse(parsed=fixture_payload("broad-response.json"))
    response.usage_metadata = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=10,
        candidates_token_count=0,
        thoughts_token_count=0,
        total_token_count=10,
        prompt_tokens_details=[
            types.ModalityTokenCount(modality="MODALITY_UNSPECIFIED", token_count=10)
        ],
    )
    fake_client.models.responses.append(response)
    result = adapter.broad_scan(
        upload, broad_chunk, broad_request, prompt_version="broad-v1"
    )
    assert result.usage.has_unknown_billing
    assert result.usage.actual_cost_usd is None


def test_explicit_complete_zero_usage_remains_known_zero_cost(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(
        ParsedResponse(
            parsed=fixture_payload("broad-response.json"),
            response_id="request-zero-usage",
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=0,
                candidates_token_count=0,
                thoughts_token_count=0,
                total_token_count=0,
                prompt_tokens_details=[],
            ),
        )
    )

    result = adapter.broad_scan(
        upload, broad_chunk, broad_request, prompt_version="broad-v1"
    )

    assert result.usage.attempts[0].status == "succeeded"
    assert not result.usage.has_unknown_billing
    assert result.usage.actual_cost_usd == Decimal(0)


def test_usage_payload_preserves_request_id_and_redacts_key_from_repr_and_errors(
    adapter: Any,
    fake_client: FakeClient,
    broad_chunk: object,
    broad_request: AnalysisRequestContext,
) -> None:
    upload = upload_active(adapter)
    fake_client.models.responses.append(queued_response("broad-response.json"))

    result = adapter.broad_scan(
        upload, broad_chunk, broad_request, prompt_version="broad-v1"
    )
    payload = result.usage.safe_payload()

    assert payload == {
        "reservation_id": "reservation-broad-1",
        "attempts": [
            {
                "request_id": "request-123",
                "status": "succeeded",
                "prompt_tokens": 11,
                "media_input_tokens": 7,
                "audio_input_tokens": 0,
                "text_input_tokens": 4,
                "candidates_tokens": 17,
                "thoughts_tokens": 3,
                "output_tokens": 20,
                "total_tokens": 31,
            }
        ],
        "request_ids": ["request-123"],
        "prompt_tokens": 11,
        "media_input_tokens": 7,
        "audio_input_tokens": 0,
        "text_input_tokens": 4,
        "candidates_tokens": 17,
        "thoughts_tokens": 3,
        "output_tokens": 20,
        "total_tokens": 31,
        "has_unknown_billing": False,
        "actual_cost_usd": "0.000053",
    }
    assert SECRET not in repr(adapter)
    sdk_error = errors.ClientError(
        401,
        {"error": {"message": f"failed {SECRET}"}},
    )
    fake_client.models.responses.append(sdk_error)
    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(
            upload, broad_chunk, broad_request, prompt_version="broad-v1"
        )
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
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_live_broad_scan_reserves_settles_and_deletes_registered_proxy(
    gemini_contract: None,
    tmp_path: Path,
) -> None:
    assert GeminiAdapter is not None
    pricing = _test_pricing()
    paths = _paths(tmp_path)
    with JobStore(paths.state_dir / "live-jobs.db") as store:
        job_id = store.create_job({"contract": "gemini-live"}, {})
        manifest, chunk = _register_tiny_proxy(tmp_path, store, job_id=job_id)
        authorization = validate_upload_candidate(
            manifest.manifest_id,
            job_id,
            store,
            paths,
        )
        maximum = GeminiAdapter.estimate_broad_request_maximum(
            authorization,
            chunk,
            pricing,
        )
        if maximum > Decimal("0.01"):
            authorization.close()
            pytest.skip("computed live request maximum exceeds USD 0.01")
        assert maximum <= Decimal("0.01")

        store.initialize_budget(job_id, Decimal("0.01"))
        reservation_id = "gemini-live-broad-1"
        reserved = store.reserve_request(
            RequestReservation(
                request_id=reservation_id,
                job_id=job_id,
                cache_key=f"gemini-live:{manifest.manifest_id}:{manifest.chunk_id}",
                mode="broad",
                maximum_cost_usd=maximum,
            )
        )
        assert reserved == reservation_id
        adapter = GeminiAdapter.from_environment(
            maximum_request_cost_usd=maximum,
            pricing=pricing,
        )
        upload = adapter.upload(authorization)
        try:
            store.mark_request_dispatched(reservation_id)
            result = adapter.broad_scan(
                upload,
                chunk,
                AnalysisRequestContext(
                    reservation_id=reservation_id,
                    job_id=job_id,
                    manifest_id=manifest.manifest_id,
                    mode="broad",
                    chunk_id=manifest.chunk_id,
                ),
                prompt_version="broad-v1",
            )
            actual_cost = result.usage.actual_cost_usd
            if result.usage.has_unknown_billing or actual_cost is None:
                store.mark_request_billing_unknown(reservation_id)
                pytest.fail("provider billing unknown; reservation retained")
            assert actual_cost <= maximum
            store.settle_request(reservation_id, actual_cost)
            assert store.budget_state(job_id).spent_usd == actual_cost
            assert result.response.chunk_id == manifest.chunk_id
        finally:
            adapter.delete_upload(upload)
