"""Synthetic editorial decisions, not live motorbike-vlog quality evidence."""

import pytest

from video_editor.analysis import evaluation

A = "a" * 64
B = "b" * 64


def decision(digest, disposition="primary", target=()):
    return {
        "proposal_digest": digest,
        "disposition": disposition,
        "reason": "Synthetic reason",
        "observation_refs": [f"{digest}:o1"],
        "alternative_to": target,
        "basis": "semantic",
    }


def test_editorial_requires_exact_proposal_set():
    result = evaluation.EditorialResult.model_validate(
        {"set_digest": A, "decisions": [decision(A)]}
    )
    with pytest.raises(ValueError, match="proposal set"):
        evaluation.validate_editorial(
            result, A, {A: ("supported", {"o1"}), B: ("unknown", {"o1"})}
        )


def test_unknown_facts_cannot_be_promoted():
    result = evaluation.EditorialResult.model_validate(
        {"set_digest": A, "decisions": [decision(A)]}
    )
    with pytest.raises(ValueError, match="unsupported"):
        evaluation.validate_editorial(result, A, {A: ("unknown", {"o1"})})


def test_alternative_must_name_supported_primary():
    result = evaluation.EditorialResult.model_validate(
        {
            "set_digest": A,
            "decisions": [decision(A, "alternative", (B,)), decision(B, "reject")],
        }
    )
    with pytest.raises(ValueError, match="primary"):
        evaluation.validate_editorial(
            result, A, {A: ("supported", {"o1"}), B: ("supported", {"o1"})}
        )


def test_visual_duplicate_basis_not_accepted_from_text_comparison():
    item = decision(A)
    item["basis"] = "visual_duplicate"
    with pytest.raises(ValueError):
        evaluation.EditorialResult.model_validate(
            {"set_digest": A, "decisions": [item]}
        )


def test_empty_primary_set_is_valid():
    result = evaluation.EditorialResult.model_validate(
        {"set_digest": A, "decisions": [decision(A, "reject")]}
    )
    evaluation.validate_editorial(result, A, {A: ("supported", {"o1"})})
    assert result.decisions[0].disposition == "reject"
