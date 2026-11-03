"""Selection contract tests use synthetic evidence, never footage observations."""

import importlib
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

D = Decimal


def gate():
    name = "video_editor.analysis.selection"
    assert importlib.util.find_spec(name) is not None, "selection gate is missing"
    return importlib.import_module(name)


def proposal():
    return gate().SelectionProposal(
        job_id="job",
        chunk_id="chunk",
        broad_cache_key="a" * 64,
        broad_result_id="result",
        broad_request_id="request",
        source_identity="source-identity",
        source_fingerprint="source-fingerprint",
        proxy_digest="b" * 64,
        mapping_version="mapping-v1",
        chunk_source_start=D("720"),
        chunk_source_end=D("1440"),
        chunk_proxy_start=D("0"),
        chunk_proxy_end=D("720"),
        broad={
            "candidate_id": "cand_1",
            "start": "368.0",
            "end": "445.0",
            "category": "scenic",
            "reason": "Synthetic claim",
            "confidence": 0.8,
        },
    )


def review(tmp_path: Path, item=None, **changes):
    module = gate()
    item = item or proposal()
    path = tmp_path / "synthetic-evidence.bin"
    path.write_bytes(b"synthetic, not audiovisual evidence")
    data = {
        "proposal_digest": module.selection_digest(item),
        "evaluator": "synthetic-test",
        "evaluator_version": "v1",
        "observations": ("Synthetic test assertion, not a footage observation.",),
        "factual_support": "supported",
        "editorial": "primary",
        "reason": "Synthetic distinct incident.",
        "alternative_to": (),
        "evidence": (
            {
                "path": path,
                "sha256": module.file_digest(path),
                "scope": "video",
                "source_start": "1088",
                "source_end": "1165",
            },
        ),
    }
    data.update(changes)
    return module.SelectionReview.model_validate(data)


def test_gate_module_exists():
    assert gate().SELECTION_VERSION == "evidence-selection-v1"


def test_missing_review_awaits_evidence():
    module = gate()
    result = module.evaluate_selection((proposal(),), ())
    assert result.decisions[0].state == "awaiting_evidence"
    assert result.eligible == ()


def test_sparse_review_is_only_provisional(tmp_path):
    module = gate()
    item = proposal()
    evidence = review(tmp_path)
    sparse = evidence.model_copy(
        update={
            "evidence": (
                evidence.evidence[0].model_copy(update={"scope": "sparse_frames"}),
            )
        }
    )
    result = module.evaluate_selection((item,), (sparse,))
    assert result.decisions[0].state == "provisional_shortlist"
    assert result.eligible == ()
    assert result.render_ready is False
    assert result.portrait_ready is False


def test_video_review_can_admit_refinement_not_render(tmp_path):
    module = gate()
    item = proposal()
    result = module.evaluate_selection((item,), (review(tmp_path),))
    assert result.decisions[0].state == "eligible_for_refinement"
    assert result.eligible == (item,)
    assert result.render_ready is False
    assert result.paid_dispatch_authorized is False


def test_decision_preserves_review_observations_for_audit(tmp_path):
    module = gate()
    item = proposal()
    assessment = review(tmp_path, item)
    report = module.evaluate_selection((item,), (assessment,))
    payload = report.model_dump(mode="json")
    assert payload["decisions"][0]["review"] == assessment.model_dump(mode="json")


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_identity", "foreign"),
        ("broad_request_id", "foreign"),
        ("broad_result_id", "foreign"),
        ("broad_cache_key", "c" * 64),
        ("proxy_digest", "d" * 64),
    ],
)
def test_review_cannot_be_reused_for_substituted_lineage(tmp_path, field, value):
    module = gate()
    old = proposal()
    changed = old.model_copy(update={field: value})
    result = module.evaluate_selection((changed,), (review(tmp_path, old),))
    assert result.decisions[0].state == "awaiting_evidence"


def test_evidence_byte_change_blocks(tmp_path):
    module = gate()
    item = proposal()
    evidence = review(tmp_path)
    evidence.evidence[0].path.write_bytes(b"changed")
    result = module.evaluate_selection((item,), (evidence,))
    assert result.decisions[0].reason == "evidence_digest_mismatch"
    assert result.eligible == ()


def test_partial_video_coverage_awaits_evidence(tmp_path):
    module = gate()
    item = proposal()
    evidence = review(tmp_path)
    evidence = evidence.model_copy(
        update={
            "evidence": (
                evidence.evidence[0].model_copy(update={"source_end": D("1100")}),
            )
        }
    )
    result = module.evaluate_selection((item,), (evidence,))
    assert result.decisions[0].state == "awaiting_evidence"
    assert result.decisions[0].reason == "video_coverage_incomplete"


@pytest.mark.parametrize(
    "support,editorial,state",
    [
        ("contradicted", "primary", "rejected"),
        ("unknown", "primary", "awaiting_evidence"),
        ("supported", "reject", "rejected"),
    ],
)
def test_semantic_assertions_stay_separate_from_editorial(
    tmp_path, support, editorial, state
):
    module = gate()
    result = module.evaluate_selection(
        (proposal(),), (review(tmp_path, factual_support=support, editorial=editorial),)
    )
    assert result.decisions[0].state == state


def test_duplicate_review_blocks(tmp_path):
    module = gate()
    evidence = review(tmp_path)
    result = module.evaluate_selection((proposal(),), (evidence, evidence))
    assert result.decisions[0].reason == "duplicate_review"


def test_mapping_is_exact_and_raw_proxy_interval_preserved():
    item = proposal()
    assert item.source_start == D("1088.0")
    assert item.source_end == D("1165.0")
    assert item.broad.model_dump(mode="json")["start"] == "368.0"


def test_invalid_mapping_blocks_even_with_matching_review(tmp_path):
    module = gate()
    item = proposal().model_copy(update={"chunk_source_end": D("1100")})
    result = module.evaluate_selection((item,), (review(tmp_path, item),))
    assert result.decisions[0].reason == "proposal_interval_invalid"


def test_approval_cannot_round_down_a_cost_above_its_exact_cap():
    module = gate()
    item = proposal()
    cost = D("0.20000000000000000000000000001")
    approval = module.RefinementApproval(
        approval_id="synthetic-exact-cap",
        job_id="job",
        proposal_digests=(module.selection_digest(item),),
        maximum_cost_usd=D("0.2"),
        maximum_requests=1,
    )
    assert module.refinement_authorized("job", (item,), (cost,), approval) is False


def test_paid_approval_is_independent_and_exactly_bounded(tmp_path):
    module = gate()
    item = proposal()
    report = module.evaluate_selection((item,), (review(tmp_path),))
    assert (
        module.refinement_authorized("job", report.eligible, (D("0.2"),), None) is False
    )
    approval = module.RefinementApproval(
        approval_id="synthetic-approval",
        job_id="job",
        proposal_digests=(module.selection_digest(item),),
        maximum_cost_usd=D("0.2"),
        maximum_requests=1,
    )
    assert (
        module.refinement_authorized("job", report.eligible, (D("0.2"),), approval)
        is True
    )
    assert (
        module.refinement_authorized(
            "other-job", report.eligible, (D("0.2"),), approval
        )
        is False
    )
    assert (
        module.refinement_authorized("job", report.eligible, (D("0.200001"),), approval)
        is False
    )
    assert (
        module.refinement_authorized(
            "job", (item, item), (D("0.1"), D("0.1")), approval
        )
        is False
    )
