"""Tests for GeminiEvaluationProvider and cost estimators."""

from __future__ import annotations

import io
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from video_editor.analysis.evaluation import (
    Assessment,
    Claim,
    EditorialDecision,
    EditorialResult,
    GroundingResult,
    Observation,
    exact_offset,
)
from video_editor.analysis.evaluation_provider import (
    GeminiEvaluationProvider,
    estimate_editorial_cost,
    estimate_grounding_cost,
)
from video_editor.analysis.models import (
    BroadCandidate,
    ProviderResult,
    UploadedFile,
)
from video_editor.analysis.pricing import (
    GEMINI_MODEL,
    ModelPricing,
    maximum_request_cost,
)
from video_editor.analysis.proxy_chunks import AuthorizedUpload
from video_editor.analysis.selection import SelectionProposal, selection_digest
from video_editor.errors import VideoEditorError

_MICRO_USD = Decimal(1_000_000)
_TEST_PRICING = ModelPricing(
    model=GEMINI_MODEL,
    media_input_usd_per_million_tokens=Decimal("0.75"),
    audio_input_usd_per_million_tokens=Decimal("0.75"),
    text_input_usd_per_million_tokens=Decimal("0.75"),
    output_usd_per_million_tokens=Decimal("3.75"),
    source_url="https://ai.google.dev/gemini-api/docs/pricing",
    effective_date=date(2026, 10, 3),
    expires_on=date(2027, 1, 1),
)


class FakeFiles:
    """Mock Gemini Files API."""

    def __init__(self) -> None:
        self.upload_calls = 0
        self.get_calls = 0
        self.delete_calls = 0
        self.deleted: list[str] = []

    def upload(self, *, file: Any, config: dict[str, str]) -> SimpleNamespace:
        self.upload_calls += 1
        return SimpleNamespace(
            name="files/fake-upload-123",
            uri="https://generativelanguage.googleapis.com/v1beta/files/fake-upload-123",
            mime_type=config.get("mime_type", "video/mp4"),
            state="PROCESSING",
            expiration_time=None,
        )

    def get(self, *, name: str) -> SimpleNamespace:
        self.get_calls += 1
        return SimpleNamespace(
            name=name,
            uri=f"https://generativelanguage.googleapis.com/v1beta/{name}",
            mime_type="video/mp4",
            state="ACTIVE",
            expiration_time=None,
        )

    def delete(self, *, name: str) -> None:
        self.delete_calls += 1
        self.deleted.append(name)


class FakeModels:
    """Mock Gemini Models API."""

    def __init__(self) -> None:
        self.responses: deque[Any] = deque()
        self.requests: list[dict[str, Any]] = []
        self.calls = 0

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.requests.append(kwargs)
        if not self.responses:
            raise RuntimeError("no fake response queued")
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClient:
    """Mock google-genai Client."""

    def __init__(self) -> None:
        self.files = FakeFiles()
        self.models = FakeModels()


@dataclass
class ParsedResponse:
    """Mock response payload with usage metadata."""

    parsed: Any
    response_id: str = "req-test-123"
    usage_metadata: types.GenerateContentResponseUsageMetadata = field(
        default_factory=lambda: types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100,
            candidates_token_count=50,
            thoughts_token_count=10,
            total_token_count=160,
            prompt_tokens_details=[
                types.ModalityTokenCount(
                    modality=types.MediaModality.VIDEO, token_count=60
                ),
                types.ModalityTokenCount(
                    modality=types.MediaModality.AUDIO, token_count=20
                ),
                types.ModalityTokenCount(
                    modality=types.MediaModality.TEXT, token_count=20
                ),
            ],
        )
    )

    @property
    def candidates(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(finish_reason="STOP")]


def make_proposal(
    *,
    chunk_proxy_start: Decimal = Decimal(0),
    chunk_proxy_end: Decimal = Decimal(600),
    start: Decimal = Decimal("10.5"),
    end: Decimal = Decimal("25.0"),
    category: str = "action",
    reason: str = "Skier jumps over rock ledge",
) -> SelectionProposal:
    return SelectionProposal(
        job_id="job-eval-1",
        chunk_id="chunk-eval-1",
        broad_cache_key="a" * 64,
        broad_result_id="result-1",
        broad_request_id="request-1",
        source_identity="source-1",
        source_fingerprint="fingerprint-1",
        proxy_digest="b" * 64,
        mapping_version="mapping-v1",
        chunk_source_start=Decimal(0),
        chunk_source_end=Decimal(600),
        chunk_proxy_start=chunk_proxy_start,
        chunk_proxy_end=chunk_proxy_end,
        broad=BroadCandidate.model_validate(
            {
                "candidate_id": "cand-1",
                "start": str(start),
                "end": str(end),
                "category": category,
                "reason": reason,
                "confidence": 0.85,
            }
        ),
    )


def make_grounding(proposal: SelectionProposal) -> GroundingResult:
    digest = selection_digest(proposal)
    obs = Observation.model_validate(
        {
            "observation_id": "obs_1",
            "start": str(proposal.broad.start + Decimal(1)),
            "end": str(proposal.broad.end - Decimal(1)),
            "modality": "audiovisual",
            "description": "Person jumps over ledge on skis",
        }
    )
    claim_cat = Claim(
        text=proposal.broad.category,
        support="supported",
        observation_ids=("obs_1",),
    )
    claim_reason = Claim(
        text=proposal.broad.reason,
        support="supported",
        observation_ids=("obs_1",),
    )
    assessments = (
        Assessment(
            field="category",
            text=proposal.broad.category,
            claims=(claim_cat,),
            editorial_wording=(),
        ),
        Assessment(
            field="reason",
            text=proposal.broad.reason,
            claims=(claim_reason,),
            editorial_wording=(),
        ),
    )
    return GroundingResult(
        proposal_digest=digest,
        observations=(obs,),
        assessments=assessments,
    )


def make_upload() -> UploadedFile:
    return UploadedFile(
        name="files/fake-upload-123",
        uri="https://generativelanguage.googleapis.com/v1beta/files/fake-upload-123",
        mime_type="video/mp4",
        state="ACTIVE",
        expiration_time=None,
        manifest_id="manifest-test-1",
    )


# ---------------------------------------------------------------------------
# Preflight estimator tests (credential-free standalone functions)
# ---------------------------------------------------------------------------


def test_estimate_grounding_cost_conservative_bound() -> None:
    proposal = make_proposal(
        chunk_proxy_start=Decimal(0),
        chunk_proxy_end=Decimal(600),
    )
    schema_bytes = len(json.dumps(GroundingResult.model_json_schema()).encode())
    prompt_tokens = 32768 + schema_bytes
    output_tokens = 8192
    expected = maximum_request_cost(
        _TEST_PRICING,
        media_tokens=189600,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        audio_tokens=19200,
    )
    expected_ceil = (expected * _MICRO_USD).to_integral_value(
        rounding="ROUND_CEILING"
    ) / _MICRO_USD

    cost = estimate_grounding_cost(proposal, _TEST_PRICING)
    assert cost == expected_ceil
    assert cost > Decimal("0.10")


def test_estimate_grounding_cost_invalid_chunk_duration_refuses() -> None:
    invalid_proposal = make_proposal(
        chunk_proxy_start=Decimal(600),
        chunk_proxy_end=Decimal(600),
    )
    with pytest.raises(ValueError, match="chunk duration"):
        estimate_grounding_cost(invalid_proposal, _TEST_PRICING)


def test_estimate_editorial_cost_conservative_bound() -> None:
    schema_bytes = len(json.dumps(EditorialResult.model_json_schema()).encode())
    prompt_tokens = 32768 + schema_bytes
    output_tokens = 8192
    expected = maximum_request_cost(
        _TEST_PRICING,
        media_tokens=0,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        audio_tokens=0,
    )
    expected_ceil = (expected * _MICRO_USD).to_integral_value(
        rounding="ROUND_CEILING"
    ) / _MICRO_USD

    cost = estimate_editorial_cost(_TEST_PRICING)
    assert cost == expected_ceil
    assert cost > Decimal("0.01")


def test_estimate_cost_zero_pricing_cap_refuses() -> None:
    zero_pricing = ModelPricing(
        model=GEMINI_MODEL,
        media_input_usd_per_million_tokens=Decimal(0),
        audio_input_usd_per_million_tokens=Decimal(0),
        text_input_usd_per_million_tokens=Decimal(0),
        output_usd_per_million_tokens=Decimal(0),
        source_url="https://example.invalid",
        effective_date=date(2026, 10, 3),
        expires_on=date(2027, 1, 1),
    )
    proposal = make_proposal()
    with pytest.raises(VideoEditorError) as caught_g:
        estimate_grounding_cost(proposal, zero_pricing)
    assert caught_g.value.code == "pricing_zero_cap"

    with pytest.raises(VideoEditorError) as caught_e:
        estimate_editorial_cost(zero_pricing)
    assert caught_e.value.code == "pricing_zero_cap"


def test_estimate_cost_mismatched_model_refuses() -> None:
    bad_model_pricing = ModelPricing(
        model="unsupported-model",
        media_input_usd_per_million_tokens=Decimal("0.75"),
        audio_input_usd_per_million_tokens=Decimal("0.75"),
        text_input_usd_per_million_tokens=Decimal("0.75"),
        output_usd_per_million_tokens=Decimal("3.75"),
        source_url="https://example.invalid",
        effective_date=date(2026, 10, 3),
    )
    proposal = make_proposal()
    with pytest.raises(VideoEditorError) as caught:
        estimate_grounding_cost(proposal, bad_model_pricing)
    assert caught.value.code == "pricing_unknown"


# ---------------------------------------------------------------------------
# Provider initialization, composition, repr and delegation tests
# ---------------------------------------------------------------------------


def test_provider_initialization_requires_pricing() -> None:
    fake_client = FakeClient()
    with pytest.raises((TypeError, ValueError)):
        GeminiEvaluationProvider(client=fake_client)  # type: ignore[call-arg]


def test_provider_initialization_retains_client() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    assert provider._client is fake_client


def test_provider_repr_sanitization() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(
        pricing=_TEST_PRICING,
        client=fake_client,
        api_key="secret-api-key-never-leak",
    )
    repr_str = repr(provider)
    assert repr_str == "GeminiEvaluationProvider(model='gemini-3.8-flash')"
    assert "secret" not in repr_str
    assert "api_key" not in repr_str
    assert "client" not in repr_str


def test_provider_maximum_cost_methods_delegate() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    proposal = make_proposal()

    assert provider.maximum_grounding_cost(proposal) == estimate_grounding_cost(
        proposal, _TEST_PRICING
    )
    assert provider.maximum_editorial_cost() == estimate_editorial_cost(_TEST_PRICING)


# ---------------------------------------------------------------------------
# Descriptor-only upload and delete tests
# ---------------------------------------------------------------------------


def test_upload_descriptor_only_and_waits_active() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    stream = io.BytesIO(b"descriptor-only proxy bytes")
    auth = AuthorizedUpload("manifest-upload-1", stream)

    uploaded = provider.upload(auth)
    assert uploaded.name == "files/fake-upload-123"
    assert uploaded.state == "ACTIVE"
    assert fake_client.files.upload_calls == 1
    assert fake_client.files.get_calls == 1


def test_delete_upload_deletes_remote_file() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    upload = make_upload()

    provider.delete_upload(upload)
    assert fake_client.files.delete_calls == 1
    assert fake_client.files.deleted == [upload.name]


# ---------------------------------------------------------------------------
# Grounding execution tests
# ---------------------------------------------------------------------------


def test_ground_exact_offsets_fps_and_schema_bounds() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    upload = make_upload()
    proposal = make_proposal(start=Decimal("12.345678901"), end=Decimal("28.987654321"))
    grounding = make_grounding(proposal)

    fake_client.models.responses.append(
        ParsedResponse(parsed=grounding.model_dump(mode="json"))
    )

    result = provider.ground(upload, request_id="req-g-1", proposal=proposal)
    assert isinstance(result, ProviderResult)
    assert result.response == grounding
    assert result.usage.reservation_id == "req-g-1"
    assert result.usage.output_tokens == 60
    assert fake_client.models.calls == 1

    # Check request payload structure sent to SDK
    req = fake_client.models.requests[0]
    assert req["model"] == GEMINI_MODEL
    contents = req["contents"]
    assert len(contents) == 2
    part = contents[0]
    prompt_str = contents[1]

    assert part.video_metadata.start_offset == exact_offset(proposal.broad.start)
    assert part.video_metadata.end_offset == exact_offset(proposal.broad.end)
    assert part.video_metadata.fps == 3
    assert len(prompt_str.encode("utf-8")) <= 32768
    assert req["config"].max_output_tokens == 8192


def test_ground_invalid_response_schema_preserves_known_usage_no_retry() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    upload = make_upload()
    proposal = make_proposal()

    # Invalid: observations empty
    invalid_payload = {
        "proposal_digest": selection_digest(proposal),
        "observations": [],
        "assessments": [],
    }
    fake_client.models.responses.append(ParsedResponse(parsed=invalid_payload))

    with pytest.raises(VideoEditorError) as caught:
        provider.ground(upload, request_id="req-g-invalid", proposal=proposal)

    assert caught.value.code == "provider_response_invalid"
    # Exact 1 attempt - no retries or repairs
    assert fake_client.models.calls == 1
    # Usage must be preserved in safe_details
    assert "usage" in caught.value.safe_details
    usage_data = json.loads(caught.value.safe_details["usage"])
    assert usage_data["reservation_id"] == "req-g-invalid"
    assert usage_data["output_tokens"] == 60


def test_ground_semantic_validation_mismatch_preserves_known_usage() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    upload = make_upload()
    proposal = make_proposal()
    grounding = make_grounding(proposal)
    payload = grounding.model_dump(mode="json")
    # Mismatch proposal digest
    payload["proposal_digest"] = "f" * 64

    fake_client.models.responses.append(ParsedResponse(parsed=payload))

    with pytest.raises(VideoEditorError) as caught:
        provider.ground(upload, request_id="req-g-mismatch", proposal=proposal)

    assert caught.value.code == "provider_response_invalid"
    assert fake_client.models.calls == 1
    assert "usage" in caught.value.safe_details


def test_ground_sdk_error_preserves_unknown_usage_no_retry() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    upload = make_upload()
    proposal = make_proposal()

    err = RuntimeError("Gemini 503 backend failure")
    fake_client.models.responses.append(err)

    with pytest.raises(VideoEditorError) as caught:
        provider.ground(upload, request_id="req-g-err", proposal=proposal)

    assert caught.value.code == "provider_unavailable"
    assert fake_client.models.calls == 1
    assert "usage" in caught.value.safe_details
    usage_data = json.loads(caught.value.safe_details["usage"])
    assert usage_data["has_unknown_billing"] is True


def test_ground_expired_pricing_blocks_before_generation() -> None:
    fake_client = FakeClient()
    expired_pricing = ModelPricing(
        model=GEMINI_MODEL,
        media_input_usd_per_million_tokens=Decimal("0.75"),
        audio_input_usd_per_million_tokens=Decimal("0.75"),
        text_input_usd_per_million_tokens=Decimal("0.75"),
        output_usd_per_million_tokens=Decimal("3.75"),
        source_url="https://ai.google.dev/gemini-api/docs/pricing",
        effective_date=date(2025, 1, 1),
        expires_on=date(2026, 1, 1),  # Expired in past
    )
    provider = GeminiEvaluationProvider(pricing=expired_pricing, client=fake_client)
    upload = make_upload()
    proposal = make_proposal()

    with pytest.raises(VideoEditorError) as caught:
        provider.ground(upload, request_id="req-g-expired", proposal=proposal)

    assert caught.value.code == "pricing_unknown"
    assert fake_client.models.calls == 0


def test_ground_prompt_no_source_paths_or_secrets() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(
        pricing=_TEST_PRICING,
        client=fake_client,
        api_key="secret-key-12345",
    )
    upload = make_upload()
    proposal = make_proposal()
    grounding = make_grounding(proposal)
    fake_client.models.responses.append(
        ParsedResponse(parsed=grounding.model_dump(mode="json"))
    )

    provider.ground(upload, request_id="req-g-sanitize", proposal=proposal)
    prompt_str = fake_client.models.requests[0]["contents"][1]

    assert "secret-key-12345" not in prompt_str
    assert "/Users/" not in prompt_str
    assert "/Volumes/" not in prompt_str
    assert ".mp4" not in prompt_str


# ---------------------------------------------------------------------------
# Editorial comparison execution tests
# ---------------------------------------------------------------------------


def test_compare_exact_text_only_schema_and_prompt_bounds() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    proposal1 = make_proposal(reason="First candidate")
    proposal2 = make_proposal(reason="Second candidate")
    grounding1 = make_grounding(proposal1)
    grounding2 = make_grounding(proposal2)

    set_digest = "c" * 64
    decision1 = EditorialDecision(
        proposal_digest=grounding1.proposal_digest,
        disposition="primary",
        reason="Primary scene with strong action",
        observation_refs=(f"{grounding1.proposal_digest}:obs_1",),
        alternative_to=(),
        basis="semantic",
    )
    decision2 = EditorialDecision(
        proposal_digest=grounding2.proposal_digest,
        disposition="reject",
        reason="Redundant secondary action",
        observation_refs=(f"{grounding2.proposal_digest}:obs_1",),
        alternative_to=(),
        basis="semantic",
    )
    editorial = EditorialResult(set_digest=set_digest, decisions=(decision1, decision2))

    fake_client.models.responses.append(
        ParsedResponse(parsed=editorial.model_dump(mode="json"))
    )

    result = provider.compare(
        request_id="req-c-1",
        set_digest=set_digest,
        groundings=(grounding1, grounding2),
    )
    assert isinstance(result, ProviderResult)
    assert result.response == editorial
    assert result.usage.reservation_id == "req-c-1"
    assert fake_client.models.calls == 1

    # Check request payload structure sent to SDK
    req = fake_client.models.requests[0]
    assert req["model"] == GEMINI_MODEL
    # Text-only comparison: contents is [prompt], no video part!
    contents = req["contents"]
    assert len(contents) == 1
    prompt_str = contents[0]
    assert isinstance(prompt_str, str)
    assert len(prompt_str.encode("utf-8")) <= 32768
    assert req["config"].max_output_tokens == 8192


def test_compare_invalid_response_preserves_known_usage_no_retry() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    proposal = make_proposal()
    grounding = make_grounding(proposal)

    # Missing decisions
    invalid_editorial = {"set_digest": "c" * 64, "decisions": []}
    fake_client.models.responses.append(ParsedResponse(parsed=invalid_editorial))

    with pytest.raises(VideoEditorError) as caught:
        provider.compare(
            request_id="req-c-invalid",
            set_digest="c" * 64,
            groundings=(grounding,),
        )

    assert caught.value.code == "provider_response_invalid"
    assert fake_client.models.calls == 1
    assert "usage" in caught.value.safe_details
    usage_data = json.loads(caught.value.safe_details["usage"])
    assert usage_data["reservation_id"] == "req-c-invalid"
    assert usage_data["output_tokens"] == 60
    assert usage_data["has_unknown_billing"] is False


def test_compare_editorial_validation_failure_preserves_usage() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    proposal = make_proposal()
    grounding = make_grounding(proposal)

    # Decision cites unknown observation
    decision = EditorialDecision(
        proposal_digest=grounding.proposal_digest,
        disposition="primary",
        reason="Bad observation ref",
        observation_refs=(f"{grounding.proposal_digest}:nonexistent_obs",),
        alternative_to=(),
        basis="semantic",
    )
    editorial = EditorialResult(set_digest="c" * 64, decisions=(decision,))
    fake_client.models.responses.append(
        ParsedResponse(parsed=editorial.model_dump(mode="json"))
    )

    with pytest.raises(VideoEditorError) as caught:
        provider.compare(
            request_id="req-c-cite-fail",
            set_digest="c" * 64,
            groundings=(grounding,),
        )

    assert caught.value.code == "provider_response_invalid"
    assert fake_client.models.calls == 1
    assert "usage" in caught.value.safe_details
    usage_data = json.loads(caught.value.safe_details["usage"])
    assert usage_data["reservation_id"] == "req-c-cite-fail"
    assert usage_data["output_tokens"] == 60
    assert usage_data["has_unknown_billing"] is False


def test_compare_sdk_error_preserves_unknown_usage_no_retry() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    proposal = make_proposal()
    grounding = make_grounding(proposal)

    fake_client.models.responses.append(RuntimeError("API gateway timeout"))

    with pytest.raises(VideoEditorError) as caught:
        provider.compare(
            request_id="req-c-timeout",
            set_digest="c" * 64,
            groundings=(grounding,),
        )

    assert caught.value.code == "provider_unavailable"
    assert fake_client.models.calls == 1
    assert "usage" in caught.value.safe_details


def test_ground_prompt_requires_all_material_compound_subclaims() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    upload = make_upload()
    proposal = make_proposal()
    grounding = make_grounding(proposal)
    fake_client.models.responses.append(
        ParsedResponse(parsed=grounding.model_dump(mode="json"))
    )

    provider.ground(upload, request_id="req-g-compound", proposal=proposal)
    req = fake_client.models.requests[0]
    prompt_payload = json.loads(req["contents"][1])
    rules = prompt_payload["instructions"]["rules"]

    # Must require all material compound subclaims, not just one claim per field
    found_compound_rule = any(
        "compound" in r.lower() and "material" in r.lower() and "subclaim" in r.lower()
        for r in rules
    )
    assert found_compound_rule, (
        f"Rules missing material compound subclaim requirement: {rules}"
    )


def test_compare_prompt_motorbike_vlog_rubric_and_warnings() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    proposal = make_proposal()
    grounding = make_grounding(proposal)
    decision = EditorialDecision(
        proposal_digest=grounding.proposal_digest,
        disposition="primary",
        reason="Primary scenic incident",
        observation_refs=(f"{grounding.proposal_digest}:obs_1",),
        alternative_to=(),
        basis="semantic",
    )
    editorial = EditorialResult(set_digest="c" * 64, decisions=(decision,))
    fake_client.models.responses.append(
        ParsedResponse(parsed=editorial.model_dump(mode="json"))
    )

    provider.compare(
        request_id="req-c-rubric",
        set_digest="c" * 64,
        groundings=(grounding,),
    )
    req = fake_client.models.requests[0]
    prompt_payload = json.loads(req["contents"][0])
    instructions = prompt_payload["instructions"]

    # 1. Motorbike-vlog rubric: distinct incident, reveal/change, interaction, scenic reveal, trip progression vs routine waiting/transit/semantic repetition
    rubric = instructions.get("rubric", "")
    assert "distinct incident" in rubric
    assert "reveal/change" in rubric or "action or setting" in rubric
    assert "meaningful interaction" in rubric or "interaction" in rubric
    assert "scenic reveal" in rubric
    assert "trip progression" in rubric
    assert "routine waiting/transit" in rubric or "routine" in rubric
    assert "semantic repetition" in rubric

    # 2. Untrusted grounding text warning
    warning = instructions.get("untrusted_text_warning", "")
    assert "untrusted" in warning.lower()

    # 3. Explicit prohibition of text-only visual duplication
    rules = instructions.get("rules", [])
    found_visual_dup_prohibition = any(
        "visual duplication" in r.lower() and "cannot" in r.lower() for r in rules
    )
    assert found_visual_dup_prohibition, (
        f"Rules missing text-only visual duplication prohibition: {rules}"
    )


def test_compare_oversized_serialization_fails_before_generation() -> None:
    fake_client = FakeClient()
    provider = GeminiEvaluationProvider(pricing=_TEST_PRICING, client=fake_client)
    proposal = make_proposal()
    digest = selection_digest(proposal)

    # Build observations with large descriptions to exceed 32KiB
    large_obs = [
        Observation.model_validate(
            {
                "observation_id": f"obs_{i}",
                "start": "10.0",
                "end": "20.0",
                "modality": "audiovisual",
                "description": f"Detailed observation number {i}: " + ("x" * 1500),
            }
        )
        for i in range(25)
    ]
    claim = Claim(
        text=proposal.broad.reason,
        support="supported",
        observation_ids=("obs_0",),
    )
    assessments = (
        Assessment(
            field="category",
            text=proposal.broad.category,
            claims=(claim,),
            editorial_wording=(),
        ),
        Assessment(
            field="reason",
            text=proposal.broad.reason,
            claims=(claim,),
            editorial_wording=(),
        ),
    )
    oversized_grounding = GroundingResult(
        proposal_digest=digest,
        observations=tuple(large_obs),
        assessments=assessments,
    )

    with pytest.raises(VideoEditorError) as caught:
        provider.compare(
            request_id="req-c-oversized",
            set_digest="c" * 64,
            groundings=(oversized_grounding,),
        )

    assert caught.value.code == "provider_invalid_request"
    assert "32KiB" in str(caught.value)
    # Verification that it failed BEFORE generate_content
    assert fake_client.models.calls == 0
