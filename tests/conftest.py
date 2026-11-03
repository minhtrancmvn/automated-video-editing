"""Explicit synthetic admission for legacy offline pipeline tests only."""

from decimal import Decimal
from pathlib import Path

import pytest

from video_editor import workflow
from video_editor.analysis.selection import (
    RefinementApproval,
    SelectionReview,
    evaluate_selection,
    file_digest,
    selection_digest,
)


@pytest.fixture
def synthetic_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exercise gates with synthetic assertions; never used for real footage."""
    evidence_path = tmp_path / "synthetic-selection-evidence.bin"
    evidence_path.write_bytes(b"Synthetic contract data, not video observations.")

    def install(service, *, allow_test_planning=False, approval_id=None):
        def evaluate(proposals, reviews):
            del reviews
            synthetic_reviews = tuple(
                SelectionReview.model_validate(
                    {
                        "proposal_digest": selection_digest(item),
                        "evaluator": "synthetic-offline-test",
                        "evaluator_version": "1",
                        "observations": [
                            "Synthetic test assertion; no footage reviewed."
                        ],
                        "factual_support": "supported",
                        "editorial": "primary",
                        "reason": "Synthetic contract admission for existing pipeline tests.",
                        "evidence": [
                            {
                                "path": evidence_path,
                                "sha256": file_digest(evidence_path),
                                "scope": "video",
                                "source_start": item.source_start,
                                "source_end": item.source_end,
                            }
                        ],
                    }
                )
                for item in proposals
            )
            if proposals:
                service.refinement_approval = RefinementApproval(
                    approval_id=approval_id
                    or "synthetic-offline-approval-"
                    + "-".join(selection_digest(item) for item in proposals),
                    job_id=proposals[0].job_id,
                    proposal_digests=tuple(
                        selection_digest(item) for item in proposals
                    ),
                    maximum_cost_usd=Decimal(100),
                    maximum_requests=len(proposals),
                )
            return evaluate_selection(proposals, synthetic_reviews)

        monkeypatch.setattr(workflow, "evaluate_selection", evaluate)
        if allow_test_planning:
            # Existing render mechanics remain testable; this bypass is not an
            # acceptance claim and is absent from production/default-gate tests.
            monkeypatch.setattr(
                service, "_require_final_selection", lambda analyzed: None
            )

    return install
