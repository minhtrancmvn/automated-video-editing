"""Offline prompt provenance and transport-attempt limits."""

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from google import genai
from google.genai import types

from video_editor.analysis.gemini import GeminiAdapter
from video_editor.analysis.models import (
    AnalysisRequestContext,
    RetryPolicy,
    UploadedFile,
)
from video_editor.analysis.pricing import pricing_for_model
from video_editor.cli import _service
from video_editor.config import AppConfig, GeminiSettings, PathSettings
from video_editor.errors import VideoEditorError
from video_editor.workflow import BROAD_PROMPT_VERSION, CANDIDATE_PROMPT_VERSION


@pytest.mark.parametrize("attempts", [1, 3])
def test_cloud_service_passes_configured_attempt_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attempts: int
) -> None:
    config = AppConfig(
        paths=PathSettings(
            *(
                tmp_path / name
                for name in ("input", "workspace", "cache", "output", "state")
            )
        ),
        storage_reserve_bytes=0,
        cloud_enabled=True,
        gemini=GeminiSettings(enabled=True, max_transient_attempts=attempts),
    )
    factory = Mock()
    monkeypatch.setattr("video_editor.cli.resolve_config", lambda _path: config)
    monkeypatch.setattr(
        "video_editor.cli.WorkflowService.validate_configured_roots",
        lambda *_args: None,
    )
    monkeypatch.setattr("video_editor.cli.GeminiAdapter.from_environment", factory)
    _, store = _service(tmp_path / "config.toml")
    try:
        assert factory.call_args.kwargs["retry_policy"].max_attempts == attempts
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize("attempts", [1, 3])
def test_environment_factory_preserves_attempt_limit(
    monkeypatch: pytest.MonkeyPatch, attempts: int
) -> None:
    factory = Mock()
    monkeypatch.setenv("GEMINI_API_KEY", "offline-key")
    monkeypatch.setattr("video_editor.analysis.gemini.genai.Client", factory)
    adapter = GeminiAdapter.from_environment(
        retry_policy=RetryPolicy(max_attempts=attempts)
    )
    assert adapter._retry_policy.max_attempts == attempts
    assert factory.call_args.kwargs["http_options"].retry_options.attempts == 1


def test_adapter_disables_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = Mock()
    monkeypatch.setattr("video_editor.analysis.gemini.genai.Client", factory)
    GeminiAdapter(api_key="offline-key")
    assert "http_options" in factory.call_args.kwargs
    assert factory.call_args.kwargs["http_options"].retry_options.attempts == 1


@pytest.mark.parametrize("attempts", [1, 3])
def test_real_sdk_transport_does_not_multiply_adapter_attempts(
    monkeypatch: pytest.MonkeyPatch, attempts: int
) -> None:
    calls: list[httpx.Request] = []

    def unavailable(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, json={"error": {"code": 503, "message": "offline"}})

    real_client = genai.Client
    transport_client = httpx.Client(transport=httpx.MockTransport(unavailable))
    clients = []

    def factory(**kwargs: object) -> genai.Client:
        options = kwargs.get("http_options") or types.HttpOptions()
        options = options.model_copy(update={"httpx_client": transport_client})
        client = real_client(api_key="offline-key", http_options=options)
        clients.append(client)
        return client

    monkeypatch.setattr("video_editor.analysis.gemini.genai.Client", factory)
    adapter = GeminiAdapter(
        api_key="offline-key",
        retry_policy=RetryPolicy(max_attempts=attempts),
        pricing=pricing_for_model("gemini-3.8-flash"),
        sleeper=lambda _: None,
    )
    upload = UploadedFile(
        name="files/offline",
        uri="https://example.invalid/proxy",
        mime_type="video/mp4",
        state="ACTIVE",
        manifest_id="manifest-1",
    )
    monkeypatch.setattr(adapter, "wait_until_active", lambda current: current)
    try:
        with pytest.raises(VideoEditorError) as caught:
            adapter.broad_scan(
                upload,
                SimpleNamespace(
                    chunk_id="chunk-1", proxy_start=Decimal(0), proxy_end=Decimal(1)
                ),
                AnalysisRequestContext(
                    reservation_id="request-1",
                    job_id="job-1",
                    manifest_id="manifest-1",
                    mode="broad",
                    chunk_id="chunk-1",
                ),
                prompt_version="broad-v1",
            )
        assert caught.value.code == "provider_unavailable"
        assert len(calls) == attempts
    finally:
        for client in clients:
            client.close()
        transport_client.close()


def test_exact_echo_prompt_has_new_version_without_changing_broad() -> None:
    assert BROAD_PROMPT_VERSION == "broad-v2"
    assert CANDIDATE_PROMPT_VERSION == "candidate-v4"
    request = AnalysisRequestContext(
        reservation_id="request-1",
        job_id="job-1",
        manifest_id="manifest-1",
        mode="candidate",
        chunk_id="chunk-1",
        candidate_id="candidate-1",
    )
    prompt = GeminiAdapter._prompt(
        CANDIDATE_PROMPT_VERSION,
        request=request,
        chunk_id="chunk-1",
        candidate_id="candidate-1",
        start=Decimal(0),
        end=Decimal(1),
    )
    assert "prompt_version: candidate-v4" in prompt
    assert "Echo chunk_id, candidate_id, start, and end exactly as requested." in prompt
