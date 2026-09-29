from decimal import Decimal

import pytest
from pydantic import ValidationError

from video_editor.analysis.models import (
    AnalysisBoundaryKind,
    AnalysisChunkData,
    AnalysisResultData,
    BudgetState,
    ProxyManifestData,
    RequestReservation,
    SourceRange,
)


def test_source_range_accepts_exact_decimal_interval() -> None:
    source_range = SourceRange(source_id="source-1", start="0.125", end="1.250")

    assert source_range.start == Decimal("0.125")
    assert source_range.end == Decimal("1.250")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_source_range_rejects_non_finite_decimals(value: str) -> None:
    with pytest.raises(ValidationError):
        SourceRange(source_id="source-1", start=value, end="1")


@pytest.mark.parametrize(
    ("start", "end"),
    [("1", "1"), ("2", "1"), ("-0.1", "1")],
)
def test_source_range_rejects_invalid_intervals(start: str, end: str) -> None:
    with pytest.raises(ValidationError):
        SourceRange(source_id="source-1", start=start, end=end)


def test_source_range_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SourceRange(source_id="source-1", start="0", end="1", api_key="secret")


@pytest.mark.parametrize("maximum", ["0", "-0.01", "NaN", "Infinity"])
def test_request_reservation_rejects_non_positive_or_non_finite_cost(
    maximum: str,
) -> None:
    with pytest.raises(ValidationError):
        RequestReservation(
            request_id="request-1",
            job_id="job-1",
            cache_key="cache-1",
            mode="broad",
            maximum_cost_usd=maximum,
        )


def test_request_reservation_rejects_extra_fields_and_secret_fields() -> None:
    with pytest.raises(ValidationError):
        RequestReservation(
            request_id="request-1",
            job_id="job-1",
            cache_key="cache-1",
            mode="broad",
            maximum_cost_usd="0.25",
            api_key="secret",
        )


def test_budget_state_rejects_non_finite_values() -> None:
    with pytest.raises(ValidationError):
        BudgetState(
            limit_usd="1",
            spent_usd="0",
            reserved_usd="NaN",
            remaining_usd="1",
        )


@pytest.mark.parametrize(
    ("model_type", "payload"),
    [
        (
            ProxyManifestData,
            {
                "schema_version": 1,
                "source_fingerprint": "source-1",
                "source_identity": "identity-1",
                "artifact_path": "/generated/proxy.mp4",
                "generated_root": "/generated",
                "mapping_version": "v1",
                "source_start": "0",
                "source_end": "1",
                "proxy_start": "0",
                "proxy_end": "1",
                "video_width": 1920,
                "video_height": 1080,
                "video_fps": "30",
                "audio_codec": "aac",
                "audio_bitrate_bps": 128000,
                "implementation_version": "v1",
            },
        ),
        (
            AnalysisChunkData,
            {
                "schema_version": 1,
                "mapping_version": "v1",
                "proxy_start": "0",
                "proxy_end": "1",
                "boundary_kind": AnalysisBoundaryKind.SCENE,
                "implementation_version": "v1",
            },
        ),
        (
            AnalysisResultData,
            {
                "schema_version": 1,
                "provider": "provider",
                "model": "model",
                "request_id": "request-1",
                "prompt_version": "v1",
                "response_schema_version": "v1",
                "implementation_version": "v1",
                "token_count": 3,
                "request_token_count": 2,
                "output_token_count": 1,
                "normalized_record_ids": ("record-1",),
            },
        ),
    ],
)
@pytest.mark.parametrize(
    "forbidden_field",
    ["access_token", "client_secret", "private_key", "x-api-key", "unknown_field"],
)
def test_analysis_payload_models_reject_secret_aliases_and_unknown_fields(
    model_type: type[ProxyManifestData | AnalysisChunkData | AnalysisResultData],
    payload: dict[str, object],
    forbidden_field: str,
) -> None:
    with pytest.raises(ValidationError):
        model_type(**payload, **{forbidden_field: "secret"})


@pytest.mark.parametrize(
    ("model_type", "payload"),
    [
        (
            ProxyManifestData,
            {
                "schema_version": 1,
                "source_fingerprint": "source-1",
                "source_identity": "identity-1",
                "artifact_path": "/generated/proxy.mp4",
                "generated_root": "/generated",
                "mapping_version": "v1",
                "source_start": "0",
                "source_end": "1",
                "proxy_start": "0",
                "proxy_end": "1",
                "video_width": 1920,
                "video_height": 1080,
                "video_fps": "30",
                "audio_codec": "aac",
                "audio_bitrate_bps": 128000,
                "implementation_version": "v1",
            },
        ),
        (
            AnalysisChunkData,
            {
                "schema_version": 1,
                "mapping_version": "v1",
                "proxy_start": "0",
                "proxy_end": "1",
                "boundary_kind": "scene",
                "implementation_version": "v1",
            },
        ),
    ],
)
def test_analysis_timed_payload_models_reject_invalid_ranges(
    model_type: type[ProxyManifestData | AnalysisChunkData],
    payload: dict[str, object],
) -> None:
    payload["proxy_end"] = "NaN"
    with pytest.raises(ValidationError):
        model_type(**payload)


def test_analysis_models_are_immutable_after_validation() -> None:
    data = AnalysisResultData(
        schema_version=1,
        provider="provider",
        model="model",
        request_id="request-1",
        prompt_version="v1",
        response_schema_version="v1",
        implementation_version="v1",
        token_count=3,
        request_token_count=2,
        output_token_count=1,
        normalized_record_ids=("record-1",),
    )

    with pytest.raises(ValidationError, match="frozen_instance"):
        data.token_count = 4

    assert isinstance(data.normalized_record_ids, tuple)
