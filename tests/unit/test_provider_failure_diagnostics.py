"""Provider failures keep safe transport facts: types, numbers, never text."""

from __future__ import annotations

import io
import json
import socket
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors
from test_gemini import SECRET, FakeClient, FakeFiles, _test_pricing

from video_editor.analysis.gemini import GeminiAdapter, RetryPolicy
from video_editor.analysis.models import AnalysisRequestContext, RequestReservation
from video_editor.analysis.orchestrator import _record_provider_failure
from video_editor.analysis.proxy_chunks import AuthorizedUpload
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.persistence.database import JobStore

UPLOAD_URL = "https://offline.invalid/upload?upload_id=private-session"


def _adapter(client: FakeClient) -> GeminiAdapter:
    return GeminiAdapter(
        client,
        api_key=SECRET,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=Decimal(0)),
        sleeper=lambda _: None,
        pricing=_test_pricing(),
    )


def _dns_failure() -> httpx.ConnectError:
    error = httpx.ConnectError(f"cannot reach {UPLOAD_URL} with {SECRET}")
    error.__cause__ = socket.gaierror(8, f"lookup failed for {SECRET}")
    return error


def _server_error() -> errors.ServerError:
    return errors.ServerError(503, {"error": {"message": f"down {SECRET}"}})


def _transport(error: VideoEditorError) -> dict[str, object]:
    raw = error.safe_details["transport"]
    assert SECRET not in raw
    assert "private-session" not in raw
    assert "lookup failed" not in raw
    return json.loads(raw)


def test_upload_failure_records_each_attempt_type_chain_status_and_timing() -> None:
    client = FakeClient()
    client.files.upload_responses.extend(
        [_server_error(), _dns_failure(), _dns_failure()]
    )

    with pytest.raises(VideoEditorError) as caught:
        _adapter(client).upload(AuthorizedUpload("manifest-1", io.BytesIO(b"proxy")))

    assert caught.value.code == "provider_unavailable"
    transport = _transport(caught.value)
    assert transport["operation"] == "upload"
    attempts = transport["attempts"]
    assert [attempt["code"] for attempt in attempts] == ["provider_unavailable"] * 3
    assert attempts[0]["http_status"] == 503
    assert attempts[0]["exception_chain"] == [
        {"type": "google.genai.errors.ServerError"}
    ]
    for attempt in attempts[1:]:
        assert "http_status" not in attempt
        assert attempt["exception_chain"] == [
            {"type": "httpx.ConnectError"},
            {"type": "socket.gaierror", "errno": 8},
        ]
    assert all(
        isinstance(attempt["elapsed_ms"], int) and attempt["elapsed_ms"] >= 0
        for attempt in attempts
    )


def test_non_retryable_upload_failure_records_single_attempt() -> None:
    client = FakeClient()
    client.files.upload_responses.append(
        errors.ClientError(401, {"error": {"message": f"bad key {SECRET}"}})
    )

    with pytest.raises(VideoEditorError) as caught:
        _adapter(client).upload(AuthorizedUpload("manifest-1", io.BytesIO(b"proxy")))

    assert caught.value.code == "provider_authentication"
    transport = _transport(caught.value)
    assert transport["operation"] == "upload"
    assert [attempt["http_status"] for attempt in transport["attempts"]] == [401]


@pytest.mark.parametrize(
    ("operation", "expected"), [("get", "file_status"), ("delete", "delete")]
)
def test_file_status_and_delete_failures_name_their_operation(
    operation: str, expected: str
) -> None:
    client = FakeClient()
    adapter = _adapter(client)
    upload = adapter.upload(AuthorizedUpload("manifest-1", io.BytesIO(b"proxy")))
    responses = (
        client.files.get_responses
        if operation == "get"
        else client.files.delete_responses
    )
    responses.extend([_server_error()] * 3)

    with pytest.raises(VideoEditorError) as caught:
        if operation == "get":
            adapter.wait_until_active(upload)
        else:
            adapter.delete_upload(upload)

    transport = _transport(caught.value)
    assert transport["operation"] == expected
    assert len(transport["attempts"]) == 3


def test_generation_failure_keeps_transport_alongside_usage() -> None:
    client = FakeClient()
    adapter = _adapter(client)
    client.files.upload_responses.append(FakeFiles.response(state="ACTIVE"))
    upload = adapter.upload(AuthorizedUpload("manifest-1", io.BytesIO(b"proxy")))
    client.models.responses.extend([_server_error(), _dns_failure(), _dns_failure()])
    request = AnalysisRequestContext(
        reservation_id="reservation-broad-1",
        job_id="job-1",
        manifest_id="manifest-1",
        mode="broad",
        chunk_id="chunk-1",
    )
    chunk = SimpleNamespace(
        chunk_id="chunk-1", proxy_start=Decimal(0), proxy_end=Decimal(6)
    )

    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(upload, chunk, request, prompt_version="broad-v3")

    assert client.models.calls == 3
    assert "usage" in caught.value.safe_details
    transport = _transport(caught.value)
    assert transport["operation"] == "generate"
    assert [attempt.get("http_status") for attempt in transport["attempts"]] == [
        503,
        None,
        None,
    ]


def _dispatched_request(store: JobStore) -> str:
    job_id = store.create_job({}, {})
    store.initialize_budget(job_id, Decimal(1))
    request_id = "transport-evidence-request"
    store.reserve_request(
        RequestReservation(
            request_id=request_id,
            job_id=job_id,
            cache_key="transport-evidence-key",
            mode="broad",
            maximum_cost_usd=Decimal("0.20"),
        )
    )
    store.mark_request_dispatched(request_id)
    return request_id


def _evidence(store: JobStore, request_id: str) -> tuple[str, dict[str, object]]:
    path = next((store.db_path.parent / "analysis-failures").rglob(f"*{request_id}*"))
    text = path.read_text()
    return text, json.loads(text)


def test_failure_evidence_persists_validated_transport(tmp_path: Path) -> None:
    client = FakeClient()
    client.files.upload_responses.extend([_dns_failure()] * 3)
    with pytest.raises(VideoEditorError) as caught:
        _adapter(client).upload(AuthorizedUpload("manifest-1", io.BytesIO(b"proxy")))

    with JobStore(tmp_path / "state.db") as store:
        request_id = _dispatched_request(store)
        _record_provider_failure(store, request_id, caught.value)
        text, evidence = _evidence(store, request_id)

    assert SECRET not in text
    assert evidence["code"] == "provider_unavailable"
    assert evidence["transport"] == json.loads(caught.value.safe_details["transport"])


def test_failure_evidence_drops_unsafe_transport_entries(tmp_path: Path) -> None:
    hostile = {
        "operation": "upload",
        "message": SECRET,
        "attempts": [
            {
                "elapsed_ms": 12,
                "code": "provider_unavailable",
                "http_status": 503,
                "message": SECRET,
                "exception_chain": [
                    {"type": "httpx.ConnectError", "detail": SECRET},
                    {"type": f"evil<locals>{SECRET}"},
                    {"type": "socket.gaierror", "errno": "8"},
                ],
            },
            {"elapsed_ms": -1, "code": "provider_unavailable", "exception_chain": []},
            {"elapsed_ms": 5, "code": SECRET, "exception_chain": []},
            {"elapsed_ms": 5, "code": "provider_network", "http_status": 9999},
            "not an attempt",
        ],
    }
    error = VideoEditorError(
        ErrorCategory.PROVIDER,
        "offline provider failure",
        code="provider_unavailable",
        safe_details={"transport": json.dumps(hostile)},
    )

    with JobStore(tmp_path / "state.db") as store:
        request_id = _dispatched_request(store)
        _record_provider_failure(store, request_id, error)
        text, evidence = _evidence(store, request_id)

    assert SECRET not in text
    assert evidence["transport"] == {
        "operation": "upload",
        "attempts": [
            {
                "elapsed_ms": 12,
                "code": "provider_unavailable",
                "http_status": 503,
                "exception_chain": [
                    {"type": "httpx.ConnectError"},
                    {"type": "unrecognized"},
                    {"type": "socket.gaierror"},
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "raw",
    ["not json", json.dumps({"operation": "exfiltrate", "attempts": []}), "[]"],
)
def test_failure_evidence_omits_invalid_transport(tmp_path: Path, raw: str) -> None:
    error = VideoEditorError(
        ErrorCategory.PROVIDER,
        "offline provider failure",
        code="provider_unavailable",
        safe_details={"transport": raw},
    )

    with JobStore(tmp_path / "state.db") as store:
        request_id = _dispatched_request(store)
        _record_provider_failure(store, request_id, error)
        _, evidence = _evidence(store, request_id)

    assert "transport" not in evidence
