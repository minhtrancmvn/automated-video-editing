"""Evidence-linked selection mechanics; semantic observations are evaluator inputs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field

from video_editor.analysis.models import (
    AnalysisModel,
    BroadCandidate,
    FiniteDecimal,
    NonEmptyString,
)

SELECTION_VERSION: Final = "evidence-selection-v1"
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def file_digest(path: Path) -> str:
    """Hash local evidence bytes, not an evaluator's verification flag."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def selection_digest(value: AnalysisModel) -> str:
    """Pin exact immutable proposal or review contents, including raw intervals."""
    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class SelectionProposal(AnalysisModel):
    """One unmodified broad proposal and its persisted request/media lineage."""

    job_id: NonEmptyString
    chunk_id: NonEmptyString
    broad_cache_key: Digest
    broad_result_id: NonEmptyString
    broad_request_id: NonEmptyString
    source_identity: NonEmptyString
    source_fingerprint: NonEmptyString
    proxy_digest: NonEmptyString
    mapping_version: NonEmptyString
    chunk_source_start: FiniteDecimal
    chunk_source_end: FiniteDecimal
    chunk_proxy_start: FiniteDecimal
    chunk_proxy_end: FiniteDecimal
    broad: BroadCandidate

    def _source_time(self, value: Decimal) -> Decimal:
        values = (self.chunk_source_start, self.chunk_proxy_start, value)
        with localcontext() as context:
            context.prec = max(
                context.prec,
                max(item.adjusted() for item in values)
                - min(int(item.as_tuple().exponent) for item in values)
                + 3,
            )
            return self.chunk_source_start + value - self.chunk_proxy_start

    @property
    def source_start(self) -> Decimal:
        """Translate without timestamp guessing or decimal precision loss."""
        return self._source_time(self.broad.start)

    @property
    def source_end(self) -> Decimal:
        """Translate without mutating stored provider timestamps."""
        return self._source_time(self.broad.end)


class SelectionEvidence(AnalysisModel):
    """Hashed local review input and evaluator-declared source-time coverage."""

    path: Path
    sha256: Digest
    scope: Literal["sparse_frames", "video"]
    source_start: FiniteDecimal = Field(ge=0)
    source_end: FiniteDecimal = Field(gt=0)


class SelectionReview(AnalysisModel):
    """Trusted evaluator sidecar; never parse this from a broad response."""

    proposal_digest: Digest
    evaluator: NonEmptyString
    evaluator_version: NonEmptyString
    observations: tuple[NonEmptyString, ...] = Field(min_length=1)
    factual_support: Literal["supported", "contradicted", "unknown"]
    editorial: Literal["primary", "reject", "alternative"]
    reason: NonEmptyString
    alternative_to: tuple[Digest, ...] = ()
    evidence: tuple[SelectionEvidence, ...] = Field(min_length=1)


class SelectionDecision(AnalysisModel):
    """Auditable outcome for every original proposal, including exclusions."""

    proposal: SelectionProposal
    proposal_digest: Digest
    review_digest: Digest | None
    review: SelectionReview | None
    state: Literal[
        "awaiting_evidence",
        "blocked",
        "rejected",
        "reserve_alternative",
        "provisional_shortlist",
        "eligible_for_refinement",
    ]
    reason: NonEmptyString


class SelectionReport(AnalysisModel):
    """Selection is neither final-edit acceptance nor paid authorization."""

    version: Literal["evidence-selection-v1"] = SELECTION_VERSION
    decisions: tuple[SelectionDecision, ...]
    render_ready: Literal[False] = False
    portrait_ready: Literal[False] = False
    paid_dispatch_authorized: Literal[False] = False

    @property
    def eligible(self) -> tuple[SelectionProposal, ...]:
        """Expose only video-supported primary proposals for later admission."""
        return tuple(
            item.proposal
            for item in self.decisions
            if item.state == "eligible_for_refinement"
        )


def _interval_valid(item: SelectionProposal) -> bool:
    return (
        0
        <= item.chunk_proxy_start
        <= item.broad.start
        < item.broad.end
        <= item.chunk_proxy_end
        and 0
        <= item.chunk_source_start
        <= item.source_start
        < item.source_end
        <= item.chunk_source_end
        and item._source_time(item.chunk_proxy_end) == item.chunk_source_end
    )


def _review_state(item: SelectionProposal, review: SelectionReview) -> tuple[str, str]:
    for evidence in review.evidence:
        if (
            not item.source_start
            <= evidence.source_start
            < evidence.source_end
            <= item.source_end
        ):
            return "blocked", "evidence_interval_invalid"
        try:
            if (
                not evidence.path.is_file()
                or file_digest(evidence.path) != evidence.sha256
            ):
                return "blocked", "evidence_digest_mismatch"
        except OSError:
            return "blocked", "evidence_unavailable"
    if review.factual_support == "contradicted":
        return "rejected", "description_contradicted"
    if review.editorial == "reject":
        return "rejected", "editorial_rejection"
    if review.factual_support == "unknown":
        return "awaiting_evidence", "factual_support_unknown"
    if review.editorial == "alternative":
        return "reserve_alternative", "explicit_editorial_alternative"
    if review.alternative_to:
        return "blocked", "unexpected_alternative_link"
    videos = sorted(
        (e.source_start, e.source_end) for e in review.evidence if e.scope == "video"
    )
    if not videos:
        return "provisional_shortlist", "sparse_evidence_only"
    covered = item.source_start
    for start, end in videos:
        if start > covered:
            break
        covered = max(covered, end)
    if covered < item.source_end:
        return "awaiting_evidence", "video_coverage_incomplete"
    return "eligible_for_refinement", "video_supported_primary"


def evaluate_selection(
    proposals: Sequence[SelectionProposal], reviews: Sequence[SelectionReview]
) -> SelectionReport:
    """Check lineage and evidence bytes without claiming to interpret video."""
    results = []
    digests = [selection_digest(item) for item in proposals]
    keys = [(item.job_id, item.chunk_id, item.broad.candidate_id) for item in proposals]
    for item, digest, key in zip(proposals, digests, keys, strict=True):
        matches = [review for review in reviews if review.proposal_digest == digest]
        state, reason = "awaiting_evidence", "review_missing_or_stale"
        if keys.count(key) > 1:
            state, reason = "blocked", "duplicate_proposal_identity"
        elif not _interval_valid(item):
            state, reason = "blocked", "proposal_interval_invalid"
        elif len(matches) > 1:
            state, reason = "blocked", "duplicate_review"
        elif matches:
            state, reason = _review_state(item, matches[0])
        results.append(
            SelectionDecision.model_validate(
                {
                    "proposal": item,
                    "proposal_digest": digest,
                    "review_digest": selection_digest(matches[0])
                    if len(matches) == 1
                    else None,
                    "review": matches[0] if len(matches) == 1 else None,
                    "state": state,
                    "reason": reason,
                }
            )
        )
    primaries = {
        row.proposal_digest
        for row in results
        if row.state in {"eligible_for_refinement", "provisional_shortlist"}
    }
    for index, row in enumerate(results):
        if row.state != "reserve_alternative":
            continue
        review = next(r for r in reviews if r.proposal_digest == row.proposal_digest)
        if not review.alternative_to or any(
            target not in primaries for target in review.alternative_to
        ):
            results[index] = row.model_copy(
                update={"state": "blocked", "reason": "alternative_primary_unavailable"}
            )
    return SelectionReport(decisions=tuple(results))


class RefinementApproval(AnalysisModel):
    """Separate caller-supplied paid scope; never inferred from review or budget."""

    approval_id: NonEmptyString
    job_id: NonEmptyString
    proposal_digests: tuple[Digest, ...] = Field(min_length=1)
    maximum_cost_usd: FiniteDecimal = Field(gt=0)
    maximum_requests: int = Field(gt=0, strict=True)


def refinement_authorized(
    job_id: str,
    proposals: Sequence[SelectionProposal],
    maximum_costs: Sequence[Decimal],
    approval: RefinementApproval | None,
) -> bool:
    """Fail closed before reservations, even when candidate results are cached."""
    digests = tuple(selection_digest(item) for item in proposals)
    return bool(
        approval is not None
        and approval.job_id == job_id
        and all(item.job_id == job_id for item in proposals)
        and digests
        and len(set(digests)) == len(digests)
        and len(digests) == len(maximum_costs) <= approval.maximum_requests
        and set(digests) == set(approval.proposal_digests)
        and all(cost.is_finite() and cost > 0 for cost in maximum_costs)
        and sum((Fraction(cost) for cost in maximum_costs), Fraction(0))
        <= Fraction(approval.maximum_cost_usd)
    )
