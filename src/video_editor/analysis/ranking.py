"""Auditable deterministic candidate ranking, deduplication, and diversity."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal, Self

from pydantic import Field, model_validator

from video_editor.analysis.models import (
    AnalysisModel,
    FiniteDecimal,
    NonEmptyString,
    RankingCandidateEvidence,
)

Dimension = Literal[
    "action",
    "scenic",
    "human",
    "story",
    "technical",
    "novelty",
    "completeness",
    "long_story",
    "short",
    "vertical",
]
Penalty = Literal[
    "blur_exposure",
    "shake_obstruction",
    "incomplete",
    "weak_boundary",
    "repetition",
    "overlap",
]

POSITIVE_WEIGHTS: dict[Dimension, Decimal] = {
    "action": Decimal("0.12"),
    "scenic": Decimal("0.08"),
    "human": Decimal("0.10"),
    "story": Decimal("0.12"),
    "technical": Decimal("0.12"),
    "novelty": Decimal("0.08"),
    "completeness": Decimal("0.12"),
    "long_story": Decimal("0.10"),
    "short": Decimal("0.08"),
    "vertical": Decimal("0.08"),
}
PENALTY_WEIGHTS: dict[Penalty, Decimal] = {
    "blur_exposure": Decimal("0.10"),
    "shake_obstruction": Decimal("0.10"),
    "incomplete": Decimal("0.10"),
    "weak_boundary": Decimal("0.05"),
    "repetition": Decimal("0.075"),
    "overlap": Decimal("0.075"),
}


class RankingSettings(AnalysisModel):
    """Versioned weights, thresholds, and bounded diversity shares."""

    version: Literal["ranking-v1"] = "ranking-v1"
    positive_weights: dict[Dimension, FiniteDecimal] = Field(
        default_factory=lambda: dict(POSITIVE_WEIGHTS)
    )
    penalty_weights: dict[Penalty, FiniteDecimal] = Field(
        default_factory=lambda: dict(PENALTY_WEIGHTS)
    )
    positive_weight_total: FiniteDecimal = Decimal("1.00")
    penalty_weight_total: FiniteDecimal = Decimal("0.50")
    temporal_overlap_threshold: FiniteDecimal = Decimal("0.50")
    visual_similarity_threshold: FiniteDecimal = Decimal("0.90")
    semantic_similarity_threshold: FiniteDecimal = Decimal("0.90")
    selection_threshold: FiniteDecimal = Decimal("0.45")
    max_selected: int = Field(default=100, ge=1)
    max_category_share: FiniteDecimal = Decimal("0.50")
    max_source_share: FiniteDecimal = Decimal("0.50")
    max_event_share: FiniteDecimal = Decimal("0.50")
    max_location_share: FiniteDecimal = Decimal("0.50")

    @model_validator(mode="after")
    def validate_settings(self) -> Self:
        """Require exact weight dimensions, totals, and normalized thresholds."""
        if self.positive_weights.keys() != POSITIVE_WEIGHTS.keys():
            raise ValueError("positive weight dimensions must match ranking-v1")
        if self.penalty_weights.keys() != PENALTY_WEIGHTS.keys():
            raise ValueError("penalty weight dimensions must match ranking-v1")
        if sum(self.positive_weights.values(), start=Decimal(0)) != (
            self.positive_weight_total
        ):
            raise ValueError("positive weights must equal positive_weight_total")
        if sum(self.penalty_weights.values(), start=Decimal(0)) != (
            self.penalty_weight_total
        ):
            raise ValueError("penalty weights must equal penalty_weight_total")
        normalized = (
            self.temporal_overlap_threshold,
            self.visual_similarity_threshold,
            self.semantic_similarity_threshold,
            self.max_category_share,
            self.max_source_share,
            self.max_event_share,
            self.max_location_share,
        )
        if any(value < 0 or value > 1 for value in normalized):
            raise ValueError("thresholds and shares must be between zero and one")
        return self


class ScoreBreakdown(AnalysisModel):
    """Weighted score components plus unmodified supplied input values."""

    version: NonEmptyString
    raw: dict[str, FiniteDecimal]
    positive: dict[Dimension, FiniteDecimal]
    penalties: dict[Penalty, FiniteDecimal]
    total: FiniteDecimal


class Rejection(AnalysisModel):
    """Machine-readable rejection with optional selected winner reference."""

    reason_code: NonEmptyString
    winner_candidate_id: NonEmptyString | None = None


class RankedCandidate(AnalysisModel):
    """Auditable ranking result for one candidate."""

    candidate_id: NonEmptyString
    source_id: NonEmptyString
    source_start: FiniteDecimal
    source_end: FiniteDecimal
    category: NonEmptyString
    event_id: NonEmptyString | None
    location_id: NonEmptyString | None
    confidence: FiniteDecimal
    completeness: FiniteDecimal
    evidence_ids: tuple[NonEmptyString, ...]
    score: ScoreBreakdown
    dedup_group_id: NonEmptyString
    eligible_long: bool
    eligible_short: bool
    selected: bool
    rejections: tuple[Rejection, ...]

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """Return stable machine-readable rejection codes."""
        return tuple(rejection.reason_code for rejection in self.rejections)


def _clamp(value: Decimal) -> Decimal:
    return min(Decimal(1), max(Decimal(0), value))


def _score(
    candidate: RankingCandidateEvidence,
    settings: RankingSettings,
) -> ScoreBreakdown:
    raw: dict[str, Decimal] = {
        name: getattr(candidate, name)
        for name in (*settings.positive_weights, *settings.penalty_weights)
    }
    positive = {
        name: _clamp(raw[name]) * weight
        for name, weight in settings.positive_weights.items()
    }
    penalties = {
        name: _clamp(raw[name]) * weight
        for name, weight in settings.penalty_weights.items()
    }
    total = sum(positive.values(), start=Decimal(0)) - sum(
        penalties.values(), start=Decimal(0)
    )
    return ScoreBreakdown(
        version=settings.version,
        raw=raw,
        positive=positive,
        penalties=penalties,
        total=total,
    )


def _overlap_ratio(
    left: RankingCandidateEvidence,
    right: RankingCandidateEvidence,
) -> Decimal:
    if left.source_id != right.source_id:
        return Decimal(0)
    overlap = min(left.source_end, right.source_end) - max(
        left.source_start, right.source_start
    )
    if overlap <= 0:
        return Decimal(0)
    shorter = min(
        left.source_end - left.source_start,
        right.source_end - right.source_start,
    )
    return overlap / shorter


def _supplied_similarity(
    left: RankingCandidateEvidence,
    right: RankingCandidateEvidence,
    field: Literal["visual", "semantic"],
) -> Decimal | None:
    supplied = [
        getattr(similarity, field)
        for candidate, other_id in (
            (left, right.candidate_id),
            (right, left.candidate_id),
        )
        for similarity in candidate.similarities
        if similarity.other_candidate_id == other_id
        and getattr(similarity, field) is not None
    ]
    return max(supplied) if supplied else None


def _duplicates(
    left: RankingCandidateEvidence,
    right: RankingCandidateEvidence,
    settings: RankingSettings,
) -> bool:
    if left.exact_event_id is not None and left.exact_event_id == right.exact_event_id:
        return True
    if _overlap_ratio(left, right) >= settings.temporal_overlap_threshold:
        return True
    visual = _supplied_similarity(left, right, "visual")
    if visual is not None and _clamp(visual) >= settings.visual_similarity_threshold:
        return True
    semantic = _supplied_similarity(left, right, "semantic")
    return (
        semantic is not None
        and _clamp(semantic) >= settings.semantic_similarity_threshold
    )


def _dedup_groups(
    candidates: Sequence[RankingCandidateEvidence],
    settings: RankingSettings,
) -> dict[str, str]:
    parents = {
        candidate.candidate_id: candidate.candidate_id for candidate in candidates
    }

    def find(candidate_id: str) -> str:
        while parents[candidate_id] != candidate_id:
            parents[candidate_id] = parents[parents[candidate_id]]
            candidate_id = parents[candidate_id]
        return candidate_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if _duplicates(left, right, settings):
                union(left.candidate_id, right.candidate_id)

    members: dict[str, list[str]] = {}
    for candidate_id in parents:
        members.setdefault(find(candidate_id), []).append(candidate_id)
    group_ids = {
        root: "dedup-"
        + hashlib.sha256("\0".join(sorted(ids)).encode()).hexdigest()[:16]
        for root, ids in members.items()
    }
    return {candidate_id: group_ids[find(candidate_id)] for candidate_id in parents}


def _rank_key(
    candidate: RankingCandidateEvidence,
    score: ScoreBreakdown,
) -> tuple[Decimal, Decimal, Decimal, Decimal, str]:
    return (
        score.total,
        _clamp(candidate.confidence),
        _clamp(candidate.completeness),
        -candidate.source_start,
        candidate.candidate_id,
    )


def _quota_reason(
    candidate: RankingCandidateEvidence,
    selected: Sequence[RankingCandidateEvidence],
    settings: RankingSettings,
    target_count: int,
) -> tuple[str, str] | None:
    checks: tuple[tuple[str, str, Decimal], ...] = (
        ("category", "category_quota", settings.max_category_share),
        ("source_id", "source_quota", settings.max_source_share),
        ("event_id", "event_quota", settings.max_event_share),
        ("location_id", "location_quota", settings.max_location_share),
    )
    projected_total = len(selected) + 1
    for field, reason, share in checks:
        value = getattr(candidate, field)
        if value is None:
            continue
        allowed = max(1, int(share * target_count))
        count = sum(getattr(item, field) == value for item in selected)
        if count >= allowed and count + 1 > share * projected_total:
            winner = next(
                item.candidate_id for item in selected if getattr(item, field) == value
            )
            return reason, winner
    return None


def rank_candidates(
    candidates: Sequence[RankingCandidateEvidence],
    settings: RankingSettings,
) -> tuple[RankedCandidate, ...]:
    """Score, deduplicate, and diversity-select normalized evidence."""
    if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
        raise ValueError("candidate IDs must be unique")
    scores = {
        candidate.candidate_id: _score(candidate, settings) for candidate in candidates
    }
    groups = _dedup_groups(candidates, settings)
    ordered = sorted(
        candidates,
        key=lambda candidate: _rank_key(candidate, scores[candidate.candidate_id]),
        reverse=True,
    )
    target_count = min(settings.max_selected, len(ordered))
    selected: list[RankingCandidateEvidence] = []
    group_winners: dict[str, str] = {}
    rejections: dict[str, list[Rejection]] = {}

    for candidate in ordered:
        candidate_rejections = rejections.setdefault(candidate.candidate_id, [])
        for code in candidate.technical_failure_codes:
            candidate_rejections.append(Rejection(reason_code=code))
        if candidate_rejections:
            continue
        if scores[candidate.candidate_id].total < settings.selection_threshold:
            candidate_rejections.append(Rejection(reason_code="below_threshold"))
            continue
        group_id = groups[candidate.candidate_id]
        if group_id in group_winners:
            candidate_rejections.append(
                Rejection(
                    reason_code="duplicate",
                    winner_candidate_id=group_winners[group_id],
                )
            )
            continue
        if len(selected) >= settings.max_selected:
            candidate_rejections.append(Rejection(reason_code="selection_limit"))
            continue
        quota = _quota_reason(candidate, selected, settings, target_count)
        if quota is not None:
            reason, winner = quota
            candidate_rejections.append(
                Rejection(reason_code=reason, winner_candidate_id=winner)
            )
            continue
        selected.append(candidate)
        group_winners[group_id] = candidate.candidate_id

    selected_ids = {candidate.candidate_id for candidate in selected}
    return tuple(
        RankedCandidate(
            candidate_id=candidate.candidate_id,
            source_id=candidate.source_id,
            source_start=candidate.source_start,
            source_end=candidate.source_end,
            category=candidate.category,
            event_id=candidate.event_id,
            location_id=candidate.location_id,
            confidence=_clamp(candidate.confidence),
            completeness=_clamp(candidate.completeness),
            evidence_ids=candidate.evidence_ids,
            score=scores[candidate.candidate_id],
            dedup_group_id=groups[candidate.candidate_id],
            eligible_long=not candidate.technical_failure_codes,
            eligible_short=not candidate.technical_failure_codes,
            selected=candidate.candidate_id in selected_ids,
            rejections=tuple(rejections[candidate.candidate_id]),
        )
        for candidate in ordered
    )
