"""Additional selection boundaries; all observations and media bytes are synthetic."""

from decimal import Decimal

import pytest
from test_selection_gate import gate, proposal, review


def test_alternative_retained_only_with_available_explicit_primary(tmp_path):
    module = gate()
    primary = proposal()
    alternative = primary.model_copy(update={"chunk_id": "alternative-chunk"})
    report = module.evaluate_selection(
        (primary, alternative),
        (
            review(tmp_path, primary),
            review(
                tmp_path,
                alternative,
                editorial="alternative",
                alternative_to=(module.selection_digest(primary),),
            ),
        ),
    )
    assert [row.state for row in report.decisions] == [
        "eligible_for_refinement",
        "reserve_alternative",
    ]
    assert report.eligible == (primary,)


@pytest.mark.parametrize("targets", [(), ("c" * 64,)])
def test_alternative_without_available_primary_blocks(tmp_path, targets):
    module = gate()
    item = proposal()
    report = module.evaluate_selection(
        (item,),
        (review(tmp_path, item, editorial="alternative", alternative_to=targets),),
    )
    assert report.decisions[0].reason == "alternative_primary_unavailable"
    assert report.eligible == ()


def test_duplicate_proposal_identity_blocks_both_records(tmp_path):
    module = gate()
    item = proposal()
    report = module.evaluate_selection((item, item), (review(tmp_path, item),))
    assert [row.reason for row in report.decisions] == [
        "duplicate_proposal_identity",
        "duplicate_proposal_identity",
    ]
    assert report.eligible == ()


@pytest.mark.parametrize("gap", [Decimal(0), Decimal("0.00001")])
def test_continuous_declared_video_coverage_distinguishes_gaps(tmp_path, gap):
    module = gate()
    item = proposal()
    assessment = review(tmp_path, item)
    evidence = assessment.evidence[0]
    assessment = assessment.model_copy(
        update={
            "evidence": (
                evidence.model_copy(update={"source_end": Decimal(1100)}),
                evidence.model_copy(update={"source_start": Decimal(1100) + gap}),
            )
        }
    )
    report = module.evaluate_selection((item,), (assessment,))
    assert report.decisions[0].state == (
        "eligible_for_refinement" if not gap else "awaiting_evidence"
    )


def test_mixed_decisions_do_not_promote_rejected_or_missing_reviews(tmp_path):
    module = gate()
    primary = proposal()
    rejected = primary.model_copy(update={"chunk_id": "rejected"})
    missing = primary.model_copy(update={"chunk_id": "missing"})
    report = module.evaluate_selection(
        (primary, rejected, missing),
        (review(tmp_path, primary), review(tmp_path, rejected, editorial="reject")),
    )
    assert [row.state for row in report.decisions] == [
        "eligible_for_refinement",
        "rejected",
        "awaiting_evidence",
    ]
    assert report.eligible == (primary,)


def test_missing_evidence_file_blocks_without_claiming_semantic_rejection(tmp_path):
    module = gate()
    item = proposal()
    assessment = review(tmp_path, item)
    assessment.evidence[0].path.unlink()
    report = module.evaluate_selection((item,), (assessment,))
    assert report.decisions[0].state == "blocked"
    assert report.decisions[0].reason == "evidence_digest_mismatch"


def test_empty_proposal_set_has_no_refinement_or_readiness():
    report = gate().evaluate_selection((), ())
    assert report.decisions == ()
    assert report.eligible == ()
    assert report.render_ready is False
    assert report.portrait_ready is False
    assert report.paid_dispatch_authorized is False
