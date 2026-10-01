"""Deterministic chronological long and distinct multi-short planning."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Literal

from video_editor.analysis.models import CropTrack
from video_editor.analysis.ranking import RankedCandidate
from video_editor.config import HighlightSettings
from video_editor.media.sequencing import ChronologyGroup
from video_editor.models.edit_plan import (
    AnchorMoment,
    CropKeyframe,
    EditPlanV2,
    FramingV2,
    OutputSpecV2,
    PenaltyScores,
    PlanSetPolicy,
    PlanSourceV2,
    PositiveScores,
    Provenance,
    ScoreBreakdown,
    TimelineClipV2,
    TransitionV2,
    validate_plan_set,
)

PLANNER_VERSION = "highlight-plan-v1"
_MIN_QUALITY = Decimal("0.45")
_MIN_COMPLETENESS = Decimal("0.60")
_MIN_SHORT_QUALITY = Decimal("0.60")
_MIN_VERTICAL = Decimal("0.60")
_MIN_SHORT_MOMENTS = 2
_CORE_CATEGORIES = ("action", "scenic", "human", "story")

TransitionKind = Literal["cut", "dissolve", "fade", "fade_black"]
TransitionRelation = Literal[
    "continuous_action",
    "matched_motion",
    "same_event",
    "same_place_time_shift",
    "chapter_boundary",
    "time_jump",
    "location_change",
    "story_open_close",
]

ALLOWED_TRANSITIONS: dict[TransitionKind, frozenset[TransitionRelation]] = {
    "cut": frozenset({"continuous_action", "matched_motion", "same_event"}),
    "dissolve": frozenset({"same_event", "same_place_time_shift"}),
    "fade": frozenset({"chapter_boundary", "story_open_close"}),
    "fade_black": frozenset({"chapter_boundary", "time_jump", "location_change"}),
}
_TRANSITION_DURATION: dict[TransitionKind, Decimal] = {
    "cut": Decimal(0),
    "dissolve": Decimal("0.5"),
    "fade": Decimal("0.75"),
    "fade_black": Decimal(1),
}
_TRANSITION_AUDIO: dict[TransitionKind, Literal["cut", "crossfade", "fade_out_in"]] = {
    "cut": "cut",
    "dissolve": "crossfade",
    "fade": "fade_out_in",
    "fade_black": "fade_out_in",
}


@dataclass(frozen=True)
class TransitionEvidence:
    """Validated semantic compatibility for one directed candidate boundary."""

    from_candidate_id: str
    to_candidate_id: str
    kind: TransitionKind
    relation: TransitionRelation
    reason: str
    confidence: Decimal
    important_action_boundary: bool = False

    def __post_init__(self) -> None:
        """Reject incomplete or unsupported transition evidence."""
        if not self.from_candidate_id or not self.to_candidate_id:
            raise ValueError("transition candidate IDs must be non-empty")
        if self.from_candidate_id == self.to_candidate_id:
            raise ValueError("transition must join distinct candidates")
        if self.relation not in ALLOWED_TRANSITIONS[self.kind]:
            raise ValueError(
                f"transition {self.kind} is not allowed for relation {self.relation}"
            )
        if not self.reason.strip():
            raise ValueError("transition reason must be non-empty")
        if not self.confidence.is_finite() or not Decimal(0) <= self.confidence <= 1:
            raise ValueError("transition confidence must be between zero and one")


@dataclass(frozen=True)
class PlanEvidence:
    """Auditable category and chronology evidence for one planned output."""

    category_distribution: tuple[tuple[str, int], ...]
    chapter_progression: tuple[str, ...]


@dataclass(frozen=True)
class ShortPlanEvidence:
    """Auditable theme and category evidence for one short output."""

    filename: str
    theme: str
    category_distribution: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class PlanSetValidationEvidence:
    """Recorded proof that cross-output policy validation ran."""

    validated: bool
    short_count: int
    max_short_count: int
    overlap_ratio: Decimal
    anchor_rationale: str | None


@dataclass(frozen=True)
class HighlightPlanSet:
    """One chronological long plan plus zero or more distinct short plans."""

    long: EditPlanV2
    shorts: tuple[EditPlanV2, ...]
    long_evidence: PlanEvidence
    short_evidence: tuple[ShortPlanEvidence, ...]
    validation: PlanSetValidationEvidence

    @property
    def output_filenames(self) -> tuple[str, ...]:
        """Return stable output filenames in render order."""
        return (
            self.long.output.filename,
            *(plan.output.filename for plan in self.shorts),
        )

    @property
    def themes(self) -> tuple[str, ...]:
        """Return short themes in stable output order."""
        return tuple(item.theme for item in self.short_evidence)


@dataclass(frozen=True)
class _Cluster:
    theme: str
    candidates: tuple[RankedCandidate, ...]
    quality: Decimal
    first_chronology_key: tuple[int, int, Decimal, str]


def _chronology_keys(
    groups: Sequence[ChronologyGroup],
) -> tuple[dict[str, tuple[int, int]], dict[str, str]]:
    positions: dict[str, tuple[int, int]] = {}
    chapters: dict[str, str] = {}
    for group_index, group in enumerate(groups):
        for member_index, member in enumerate(group.members):
            source_id = member.source.fingerprint
            if source_id in positions:
                raise ValueError(
                    f"source appears in multiple chronology positions: {source_id}"
                )
            positions[source_id] = (group_index, member_index)
            chapters[source_id] = group.group_id
    return positions, chapters


def _chronology_key(
    candidate: RankedCandidate,
    positions: Mapping[str, tuple[int, int]],
) -> tuple[int, int, Decimal, str]:
    try:
        group_index, member_index = positions[candidate.source_id]
    except KeyError as exc:
        raise ValueError(
            f"candidate source is missing from chronology: {candidate.source_id}"
        ) from exc
    return group_index, member_index, candidate.source_start, candidate.candidate_id


def _duration(candidate: RankedCandidate) -> Decimal:
    return candidate.source_end - candidate.source_start


def _quality_key(candidate: RankedCandidate) -> tuple[Decimal, Decimal, Decimal, str]:
    return (
        candidate.score.total,
        candidate.confidence,
        candidate.completeness,
        candidate.candidate_id,
    )


def _bounded_quality_selection(
    candidates: Sequence[RankedCandidate], cap: Decimal
) -> tuple[RankedCandidate, ...]:
    selected: list[RankedCandidate] = []
    duration = Decimal(0)
    for candidate in sorted(candidates, key=_quality_key, reverse=True):
        candidate_duration = _duration(candidate)
        if duration + candidate_duration <= cap:
            selected.append(candidate)
            duration += candidate_duration
    return tuple(selected)


def _distribution(
    candidates: Sequence[RankedCandidate],
) -> tuple[tuple[str, int], ...]:
    return tuple(
        sorted(Counter(candidate.category for candidate in candidates).items())
    )


def _selected_sources(
    candidates: Sequence[RankedCandidate], sources: Mapping[str, PlanSourceV2]
) -> list[PlanSourceV2]:
    source_ids = tuple(dict.fromkeys(candidate.source_id for candidate in candidates))
    missing = set(source_ids) - sources.keys()
    if missing:
        raise ValueError(f"missing source records: {', '.join(sorted(missing))}")
    return [sources[source_id] for source_id in source_ids]


def _framing(
    candidate: RankedCandidate,
    source: PlanSourceV2,
    tracks: Mapping[str, CropTrack],
    *,
    short: bool,
) -> FramingV2:
    if not short:
        return FramingV2(mode="center_crop")
    track = tracks[candidate.candidate_id]
    if (
        track.source_id != candidate.source_id
        or track.source_identity != source.identity
    ):
        raise ValueError(f"crop track identity mismatch: {candidate.candidate_id}")
    return FramingV2(
        mode="tracked_crop",
        track_id=track.track_id,
        track_clip_id=candidate.candidate_id,
        track_source_identity=track.source_identity,
        keyframes=[
            CropKeyframe(
                time=keyframe.time,
                center_x=keyframe.center_x,
                center_y=keyframe.center_y,
                subject_box_id=keyframe.subject_box_id,
                fallback=keyframe.fallback,
            )
            for keyframe in track.keyframes
        ],
    )


def _transition_decisions(
    candidates: Sequence[RankedCandidate],
    evidence: Mapping[tuple[str, str], TransitionEvidence],
) -> tuple[TransitionV2, ...]:
    transitions: list[TransitionV2] = []
    for index, (left, right) in enumerate(pairwise(candidates)):
        item = evidence.get((left.candidate_id, right.candidate_id))
        if item is None or (item.important_action_boundary and item.kind != "cut"):
            continue
        duration = _TRANSITION_DURATION[item.kind]
        if duration:
            duration = min(duration, _duration(left) / 2, _duration(right) / 2)
        transitions.append(
            TransitionV2(
                from_clip=index,
                to_clip=index + 1,
                kind=item.kind,
                duration=duration,
                relation=item.relation,
                reason=item.reason,
                confidence=item.confidence,
                audio_policy=_TRANSITION_AUDIO[item.kind],
            )
        )
    return tuple(transitions)


def _clips(
    candidates: Sequence[RankedCandidate],
    sources: Mapping[str, PlanSourceV2],
    tracks: Mapping[str, CropTrack],
    transitions: Sequence[TransitionV2],
    chapters: Mapping[str, str],
    *,
    short: bool,
) -> list[TimelineClipV2]:
    incoming = {transition.to_clip: transition.duration for transition in transitions}
    clips: list[TimelineClipV2] = []
    timeline_start = Decimal(0)
    for index, candidate in enumerate(candidates):
        if index:
            timeline_start -= incoming.get(index, Decimal(0))
        evidence_ids = candidate.evidence_ids
        analysis_reference = evidence_ids[0] if evidence_ids else candidate.candidate_id
        mapping_reference = (
            evidence_ids[1] if len(evidence_ids) > 1 else analysis_reference
        )
        source = sources[candidate.source_id]
        clips.append(
            TimelineClipV2(
                clip_id=candidate.candidate_id,
                source_id=candidate.source_id,
                source_identity=source.identity,
                source_start=candidate.source_start,
                source_end=candidate.source_end,
                timeline_start=timeline_start,
                speed=Decimal(1),
                framing=_framing(candidate, source, tracks, short=short),
                selection_reason=(
                    "coherent quality-first short moment"
                    if short
                    else "strong complete chronological moment"
                ),
                overall_score=candidate.score.total,
                score_breakdown=ScoreBreakdown(
                    positive=PositiveScores.model_validate(candidate.score.positive),
                    penalties=PenaltyScores.model_validate(candidate.score.penalties),
                ),
                analysis_reference=analysis_reference,
                source_proxy_mapping_reference=mapping_reference,
                dedup_group=candidate.dedup_group_id,
                chapter_id=chapters[candidate.source_id],
                event_id=candidate.event_id,
                vertical_suitability=min(
                    Decimal(1), max(Decimal(0), candidate.score.raw["vertical"])
                ),
                confidence=candidate.confidence,
                planner_version=PLANNER_VERSION,
                analysis_version=candidate.score.version,
            )
        )
        timeline_start += _duration(candidate)
    return clips


def _build_plan(
    candidates: Sequence[RankedCandidate],
    sources: Mapping[str, PlanSourceV2],
    tracks: Mapping[str, CropTrack],
    transition_evidence: Mapping[tuple[str, str], TransitionEvidence],
    chapters: Mapping[str, str],
    *,
    kind: Literal["long", "short"],
    number: int = 0,
    theme: str | None = None,
) -> EditPlanV2:
    transitions = _transition_decisions(candidates, transition_evidence)
    filename = "long.mp4" if kind == "long" else f"short-{number:02d}.mp4"
    width, height = (1920, 1080) if kind == "long" else (1080, 1920)
    analysis_versions = {candidate.score.version for candidate in candidates}
    if len(analysis_versions) != 1:
        raise ValueError("all planned candidates must use one analysis version")
    return EditPlanV2(
        schema_version=2,
        planner_version=PLANNER_VERSION,
        analysis_version=next(iter(analysis_versions)),
        sources=_selected_sources(candidates, sources),
        clips=_clips(
            candidates,
            sources,
            tracks,
            transitions,
            chapters,
            short=kind == "short",
        ),
        transitions=list(transitions),
        output=OutputSpecV2(
            plan_id="plan-long" if kind == "long" else f"plan-short-{number:02d}",
            filename=filename,
            kind=kind,
            width=width,
            height=height,
            frame_rate=Decimal(30),
            codec="libx264",
            audio="source",
            theme_summary=theme,
        ),
        provenance=Provenance(planner="highlight-planner"),
    )


def _theme(candidate: RankedCandidate) -> str:
    if candidate.event_id is not None:
        return f"event:{candidate.event_id}"
    if candidate.location_id is not None:
        return f"location:{candidate.location_id}"
    return f"category:{candidate.category}"


def _short_eligible(
    candidate: RankedCandidate, tracks: Mapping[str, CropTrack]
) -> bool:
    track = tracks.get(candidate.candidate_id)
    return (
        candidate.selected
        and candidate.eligible_short
        and candidate.score.total >= _MIN_SHORT_QUALITY
        and candidate.completeness >= _MIN_COMPLETENESS
        and candidate.score.raw["vertical"] >= _MIN_VERTICAL
        and track is not None
        and track.short_eligible
    )


def _short_clusters(
    candidates: Sequence[RankedCandidate],
    positions: Mapping[str, tuple[int, int]],
    tracks: Mapping[str, CropTrack],
    cap: Decimal,
) -> tuple[_Cluster, ...]:
    grouped: dict[str, list[RankedCandidate]] = {}
    for candidate in candidates:
        if _short_eligible(candidate, tracks):
            grouped.setdefault(_theme(candidate), []).append(candidate)

    clusters: list[_Cluster] = []
    for theme, members in grouped.items():
        chosen = _bounded_quality_selection(members, cap)
        if len(chosen) < _MIN_SHORT_MOMENTS:
            continue
        quality = sum((item.score.total for item in chosen), Decimal(0)) / len(chosen)
        if quality < _MIN_SHORT_QUALITY:
            continue
        ordered = tuple(
            sorted(chosen, key=lambda item: _chronology_key(item, positions))
        )
        clusters.append(
            _Cluster(
                theme=theme,
                candidates=ordered,
                quality=quality,
                first_chronology_key=_chronology_key(ordered[0], positions),
            )
        )
    return tuple(
        sorted(
            clusters,
            key=lambda item: (-item.quality, item.first_chronology_key, item.theme),
        )
    )


def _long_candidates(
    candidates: Sequence[RankedCandidate],
    positions: Mapping[str, tuple[int, int]],
    cap: Decimal,
) -> tuple[RankedCandidate, ...]:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.selected
        and candidate.eligible_long
        and candidate.score.total >= _MIN_QUALITY
        and candidate.completeness >= _MIN_COMPLETENESS
    ]
    selected: list[RankedCandidate] = []
    selected_ids: set[str] = set()
    duration = Decimal(0)
    for category in _CORE_CATEGORIES:
        representatives = sorted(
            (candidate for candidate in eligible if candidate.category == category),
            key=_quality_key,
            reverse=True,
        )
        for candidate in representatives:
            candidate_duration = _duration(candidate)
            if duration + candidate_duration <= cap:
                selected.append(candidate)
                selected_ids.add(candidate.candidate_id)
                duration += candidate_duration
                break
    for candidate in sorted(eligible, key=_quality_key, reverse=True):
        if candidate.candidate_id in selected_ids:
            continue
        candidate_duration = _duration(candidate)
        if duration + candidate_duration <= cap:
            selected.append(candidate)
            selected_ids.add(candidate.candidate_id)
            duration += candidate_duration
    return tuple(sorted(selected, key=lambda item: _chronology_key(item, positions)))


def create_highlight_plans(
    candidates: Sequence[RankedCandidate],
    chronology: Sequence[ChronologyGroup],
    tracks: Sequence[CropTrack],
    sources: Sequence[PlanSourceV2],
    transition_evidence: Sequence[TransitionEvidence],
    settings: HighlightSettings,
    *,
    anchor: AnchorMoment | None = None,
) -> HighlightPlanSet:
    """Create one chronological long plan and quality-first distinct shorts."""
    positions, chapters = _chronology_keys(chronology)
    source_map = {source.id: source for source in sources}
    if len(source_map) != len(sources):
        raise ValueError("source record IDs must be unique")
    track_map = {track.clip_id: track for track in tracks}
    if len(track_map) != len(tracks):
        raise ValueError("crop track clip IDs must be unique")
    transition_map = {
        (item.from_candidate_id, item.to_candidate_id): item
        for item in transition_evidence
    }
    if len(transition_map) != len(transition_evidence):
        raise ValueError("transition evidence pairs must be unique")

    long_candidates = _long_candidates(
        candidates, positions, Decimal(settings.long_max_seconds)
    )
    if not long_candidates:
        raise ValueError("no strong complete candidates available for long plan")
    long_plan = _build_plan(
        long_candidates,
        source_map,
        track_map,
        transition_map,
        chapters,
        kind="long",
    )

    policy = PlanSetPolicy(max_shorts=settings.max_short_count, anchor=anchor)
    clusters = list(
        _short_clusters(
            candidates,
            positions,
            track_map,
            Decimal(settings.short_max_seconds),
        )
    )
    if anchor is not None:
        anchor_candidate = next(
            (
                candidate
                for candidate in candidates
                if source_map[candidate.source_id].identity == anchor.source_identity
                and candidate.source_start == anchor.source_start
                and candidate.source_end == anchor.source_end
                and candidate.dedup_group_id == anchor.dedup_group
                and _short_eligible(candidate, track_map)
            ),
            None,
        )
        if anchor_candidate is not None:
            anchored_clusters: list[_Cluster] = []
            anchored_count = 0
            for cluster in clusters:
                cluster_candidates = cluster.candidates
                shares_context = any(
                    (
                        anchor_candidate.event_id is not None
                        and anchor_candidate.event_id == candidate.event_id
                    )
                    or (
                        anchor_candidate.location_id is not None
                        and anchor_candidate.location_id == candidate.location_id
                    )
                    or anchor_candidate.category == candidate.category
                    for candidate in cluster.candidates
                )
                candidate_ids = {
                    candidate.candidate_id for candidate in cluster.candidates
                }
                can_add_anchor = (
                    anchored_count < 2
                    and anchor_candidate.candidate_id not in candidate_ids
                    and shares_context
                    and sum(
                        (_duration(candidate) for candidate in cluster.candidates),
                        Decimal(0),
                    )
                    + _duration(anchor_candidate)
                    <= Decimal(settings.short_max_seconds)
                )
                if can_add_anchor:
                    cluster_candidates = tuple(
                        sorted(
                            (*cluster.candidates, anchor_candidate),
                            key=lambda item: _chronology_key(item, positions),
                        )
                    )
                    anchored_count += 1
                anchored_clusters.append(
                    _Cluster(
                        theme=cluster.theme,
                        candidates=cluster_candidates,
                        quality=cluster.quality,
                        first_chronology_key=cluster.first_chronology_key,
                    )
                )
            clusters = anchored_clusters

    accepted: list[tuple[_Cluster, EditPlanV2]] = []
    for cluster in clusters:
        if len(accepted) >= settings.max_short_count:
            break
        number = len(accepted) + 1
        plan = _build_plan(
            cluster.candidates,
            source_map,
            track_map,
            transition_map,
            chapters,
            kind="short",
            number=number,
            theme=cluster.theme,
        )
        try:
            validate_plan_set(
                [long_plan, *(item[1] for item in accepted), plan], policy
            )
        except ValueError:
            continue
        accepted.append((cluster, plan))

    shorts = tuple(plan for _, plan in accepted)
    validate_plan_set([long_plan, *shorts], policy)
    progression = tuple(
        dict.fromkeys(chapters[candidate.source_id] for candidate in long_candidates)
    )
    short_evidence = tuple(
        ShortPlanEvidence(
            filename=plan.output.filename,
            theme=cluster.theme,
            category_distribution=_distribution(cluster.candidates),
        )
        for cluster, plan in accepted
    )
    return HighlightPlanSet(
        long=long_plan,
        shorts=shorts,
        long_evidence=PlanEvidence(
            category_distribution=_distribution(long_candidates),
            chapter_progression=progression,
        ),
        short_evidence=short_evidence,
        validation=PlanSetValidationEvidence(
            validated=True,
            short_count=len(shorts),
            max_short_count=settings.max_short_count,
            overlap_ratio=settings.cross_short_overlap_ratio,
            anchor_rationale=anchor.rationale if anchor is not None else None,
        ),
    )
