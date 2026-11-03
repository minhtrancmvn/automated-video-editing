"""Synthetic evaluator contracts; no footage is observed by these tests."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from video_editor.analysis import evaluation

D = Decimal
DIGEST = "a" * 64


def grounding_payload():
    return {
        "proposal_digest": DIGEST,
        "observations": [
            {
                "observation_id": "o1",
                "start": "1",
                "end": "2",
                "modality": "visual",
                "description": "Synthetic motion observation.",
            }
        ],
        "assessments": [
            {
                "field": "category",
                "text": "incident",
                "claims": [
                    {
                        "text": "incident",
                        "support": "supported",
                        "observation_ids": ["o1"],
                    }
                ],
            },
            {
                "field": "reason",
                "text": "Synthetic incident",
                "claims": [
                    {"text": "incident", "support": "unknown", "observation_ids": []}
                ],
            },
        ],
    }


def test_unknown_claim_does_not_become_supported():
    result = evaluation.GroundingResult.model_validate(grounding_payload())
    assert result.factual_support == "unknown"


def test_contradiction_dominates_supported_claim():
    payload = grounding_payload()
    payload["assessments"][1]["claims"][0]["support"] = "contradicted"
    payload["assessments"][1]["claims"][0]["observation_ids"] = ["o1"]
    assert (
        evaluation.GroundingResult.model_validate(payload).factual_support
        == "contradicted"
    )


def test_empty_factual_claims_do_not_pass_vacuously():
    payload = grounding_payload()
    for block in payload["assessments"]:
        block["claims"] = []
    assert (
        evaluation.GroundingResult.model_validate(payload).factual_support == "unknown"
    )


def test_one_unassessed_field_cannot_become_supported():
    payload = grounding_payload()
    payload["assessments"][1]["claims"] = []
    assert (
        evaluation.GroundingResult.model_validate(payload).factual_support == "unknown"
    )


def test_missing_original_assessment_field_rejected():
    payload = grounding_payload()
    payload["assessments"].pop()
    with pytest.raises(ValidationError, match="at least 2 items"):
        evaluation.GroundingResult.model_validate(payload)


def test_supported_claim_requires_observation_reference():
    payload = grounding_payload()
    payload["assessments"][0]["claims"][0]["observation_ids"] = []
    with pytest.raises(ValidationError, match="observation"):
        evaluation.GroundingResult.model_validate(payload)


def test_unknown_observation_reference_rejected():
    payload = grounding_payload()
    payload["assessments"][0]["claims"][0]["observation_ids"] = ["absent"]
    with pytest.raises(ValidationError, match="observation"):
        evaluation.GroundingResult.model_validate(payload)


def test_timestamp_bounds_checked_against_exact_input():
    result = evaluation.GroundingResult.model_validate(grounding_payload())
    with pytest.raises(ValueError, match="interval"):
        evaluation.validate_grounding(
            result, DIGEST, "incident", "Synthetic incident", D("1.5"), D(2)
        )


def test_changed_echoed_claim_text_rejected():
    result = evaluation.GroundingResult.model_validate(grounding_payload())
    with pytest.raises(ValueError, match="claim"):
        evaluation.validate_grounding(result, DIGEST, "incident", "changed", D(1), D(2))


def test_nanosecond_serialization_never_rounds_interval():
    assert evaluation.exact_offset(D("1.000000001")) == "1.000000001s"
    with pytest.raises(ValueError, match="nanosecond"):
        evaluation.exact_offset(D("1.0000000001"))
