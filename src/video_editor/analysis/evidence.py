"""Fixed, versioned glue from analysis evidence to ranking and transition inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from video_editor.analysis.models import (
    AnalysisChunkData,
    BroadCandidate,
    CandidateRefinementResponse,
    LocalSegmentation,
    NormalizedSubjectBox,
    RankingCandidateEvidence,
    ScoredEvidence,
    SubjectObservation,
)
from video_editor.analysis.ranking import RankedCandidate
from video_editor.planning.highlight_plan import (
    TransitionEvidence,
    TransitionKind,
    TransitionRelation,
)

EVIDENCE_MAP_VERSION = "evidence-map-v1"
TRANSITION_MAP_VERSION = "transition-map-v1"
TIME_JUMP_SECONDS = Decimal(30)
_ZERO = Decimal(0)
_ONE = Decimal(1)

# relation -> kind, chosen inside the planner's approved ALLOWED_TRANSITIONS matrix.
_RELATION_KIND: dict[TransitionRelation, TransitionKind] = {
    "continuous_action": "cut",
    "same_event": "cut",
    "same_place_time_shift": "dissolve",
    "chapter_boundary": "fade",
    "story_open_close": "fade",
    "time_jump": "fade_black",
    "location_change": "fade_black",
}


def _d(value: float) -> Decimal:
    return Decimal(str(value))


def _overlap_max(
    items: Sequence[ScoredEvidence], start: Decimal, end: Decimal
) -> Decimal:
    return max(
        (
            _d(item.score)
            for item in items
            if item.source_range.start < end and item.source_range.end > start
        ),
        default=_ZERO,
    )


def map_ranking_evidence(
    broad: BroadCandidate,
    refined: CandidateRefinementResponse,
    chunk: AnalysisChunkData,
    segmentation: LocalSegmentation,
) -> RankingCandidateEvidence:
    """Map one refined candidate to ranking evidence using table evidence-map-v1.

    Table (all inputs normalized 0..1):
      action, completeness  <- refinement.action_completeness
      scenic, short         <- refinement.visual_composition
      human, story          <- refinement.semantic_importance
      novelty               <- refinement.novelty
      long_story            <- refinement.adjacent_scene_compatibility
      vertical              <- visual_composition if subject boxes exist, else 0
      technical             <- 1 - max(blur, exposure, shake, obstruction)
      blur_exposure         <- max overlapping segmentation blur/exposure
      shake_obstruction     <- max overlapping segmentation shake/obstruction
      incomplete            <- 1 - action_completeness
      weak_boundary         <- 1 - best overlapping min(entry, exit) suitability
      repetition            <- refinement.duplicate_similarity
      overlap               <- 0 (temporal overlap is handled by ranking dedup)
      confidence            <- min(broad.confidence, refinement.confidence)
    """
    offset = chunk.source_start - chunk.proxy_start
    start = refined.start + offset
    end = refined.end + offset
    blur_exposure = max(
        _overlap_max(segmentation.blur, start, end),
        _overlap_max(segmentation.exposure, start, end),
    )
    shake_obstruction = max(
        _overlap_max(segmentation.shake, start, end),
        _overlap_max(segmentation.obstruction, start, end),
    )
    boundary = max(
        (
            _d(min(item.entry_score, item.exit_score))
            for item in segmentation.boundary_suitability
            if item.source_range.start < end and item.source_range.end > start
        ),
        default=_ONE,
    )
    segment_ids = tuple(
        item.evidence_id
        for item in (*segmentation.motion, *segmentation.scenes)
        if item.source_range.start < end and item.source_range.end > start
    )
    composition = _d(refined.visual_composition)
    completeness = _d(refined.action_completeness)
    semantic = _d(refined.semantic_importance)
    return RankingCandidateEvidence(
        candidate_id=refined.candidate_id,
        source_id=chunk.source_id,
        source_start=start,
        source_end=end,
        category=broad.category,
        confidence=min(_d(broad.confidence), _d(refined.confidence)),
        action=completeness,
        scenic=composition,
        human=semantic,
        story=semantic,
        technical=_ONE - max(blur_exposure, shake_obstruction),
        novelty=_d(refined.novelty),
        completeness=completeness,
        long_story=_d(refined.adjacent_scene_compatibility),
        short=composition,
        vertical=composition if refined.subject_boxes else _ZERO,
        blur_exposure=blur_exposure,
        shake_obstruction=shake_obstruction,
        incomplete=_ONE - completeness,
        weak_boundary=_ONE - boundary,
        repetition=_d(refined.duplicate_similarity),
        overlap=_ZERO,
        evidence_ids=(
            f"analysis:{refined.candidate_id}",
            f"mapping:{refined.chunk_id}",
            *segment_ids,
        ),
    )


def subject_seeds(
    refined: CandidateRefinementResponse, chunk: AnalysisChunkData
) -> tuple[SubjectObservation, ...]:
    """Convert refinement subject boxes into source-time tracking seeds only."""
    offset = chunk.source_start - chunk.proxy_start
    return tuple(
        SubjectObservation(
            observation_id=f"{refined.candidate_id}-seed-{index}",
            source_id=chunk.source_id,
            source_identity=chunk.source_identity,
            source_time=box.time + offset,
            box=NormalizedSubjectBox(
                x_min=_d(box.x),
                y_min=_d(box.y),
                x_max=_d(box.x) + _d(box.w),
                y_max=_d(box.y) + _d(box.h),
            ),
            priority=box.priority,
            origin="gemini_seed",
        )
        for index, box in enumerate(refined.subject_boxes)
    )


def _relation(
    left: RankedCandidate,
    right: RankedCandidate,
    *,
    first_id: str,
    last_id: str,
    chapters: Mapping[str, str],
) -> TransitionRelation:
    if left.candidate_id == first_id or right.candidate_id == last_id:
        return "story_open_close"
    if chapters[left.source_id] != chapters[right.source_id]:
        return "chapter_boundary"
    if (
        left.location_id is not None
        and right.location_id is not None
        and left.location_id != right.location_id
    ):
        return "location_change"
    same_source = left.source_id == right.source_id
    gap = right.source_start - left.source_end if same_source else None
    if gap is None or gap > TIME_JUMP_SECONDS:
        return "time_jump"
    if left.event_id is not None and left.event_id == right.event_id:
        return "continuous_action" if gap == 0 else "same_event"
    return "same_place_time_shift"


def derive_transition_evidence(
    ordered: Sequence[RankedCandidate], chapters: Mapping[str, str]
) -> tuple[TransitionEvidence, ...]:
    """Derive relation/kind for every chronological pair of selected candidates."""
    if len(ordered) < 2:
        return ()
    first_id = ordered[0].candidate_id
    last_id = ordered[-1].candidate_id
    evidence: list[TransitionEvidence] = []
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            relation = _relation(
                left, right, first_id=first_id, last_id=last_id, chapters=chapters
            )
            evidence.append(
                TransitionEvidence(
                    from_candidate_id=left.candidate_id,
                    to_candidate_id=right.candidate_id,
                    kind=_RELATION_KIND[relation],
                    relation=relation,
                    reason=f"{TRANSITION_MAP_VERSION}:{relation}",
                    confidence=min(
                        _ONE, max(_ZERO, min(left.confidence, right.confidence))
                    ),
                )
            )
    return tuple(evidence)
