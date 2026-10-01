"""Tests for deterministic long and multi-short highlight planning."""

from __future__ import annotations

import random
from decimal import Decimal
from pathlib import Path

from video_editor.analysis.models import CropKeyframe, CropTrack
from video_editor.analysis.ranking import (
    Dimension,
    Penalty,
    RankedCandidate,
    Rejection,
    ScoreBreakdown,
)
from video_editor.config import HighlightSettings
from video_editor.media.discovery import SourceCandidate
from video_editor.media.sequencing import ChronologyGroup, SequencedSource
from video_editor.models.edit_plan import AnchorMoment, PlanSourceV2, timeline_duration
from video_editor.planning.highlight_plan import (
    ALLOWED_TRANSITIONS,
    HighlightPlanSet,
    TransitionEvidence,
    create_highlight_plans,
)

D = Decimal


def _candidate(
    candidate_id: str,
    source_id: str,
    start: str,
    end: str,
    *,
    category: str = "action",
    event_id: str | None = None,
    location_id: str | None = "trail",
    total: str = "0.80",
    completeness: str = "0.90",
    vertical: str = "0.90",
    dedup_group: str | None = None,
    eligible_short: bool = True,
    selected: bool = True,
) -> RankedCandidate:
    positive: dict[Dimension, Decimal] = {
        "action": D("0.1"),
        "scenic": D("0.1"),
        "human": D("0.1"),
        "story": D("0.1"),
        "technical": D("0.1"),
        "novelty": D("0.1"),
        "completeness": D(completeness),
        "long_story": D("0.1"),
        "short": D("0.1"),
        "vertical": D(vertical),
    }
    penalties: dict[Penalty, Decimal] = {
        "blur_exposure": D("0"),
        "shake_obstruction": D("0"),
        "incomplete": D("0"),
        "weak_boundary": D("0"),
        "repetition": D("0"),
        "overlap": D("0"),
    }
    raw: dict[str, Decimal] = {str(name): value for name, value in positive.items()}
    raw.update({str(name): value for name, value in penalties.items()})
    return RankedCandidate(
        candidate_id=candidate_id,
        source_id=source_id,
        source_start=D(start),
        source_end=D(end),
        category=category,
        event_id=event_id,
        location_id=location_id,
        confidence=D("0.90"),
        completeness=D(completeness),
        evidence_ids=(f"analysis:{candidate_id}", f"mapping:{candidate_id}"),
        score=ScoreBreakdown(
            version="ranking-v1",
            raw=raw,
            positive=positive,
            penalties=penalties,
            total=D(total),
        ),
        dedup_group_id=dedup_group or f"dedup-{candidate_id}",
        eligible_long=True,
        eligible_short=eligible_short,
        selected=selected,
        rejections=() if selected else (Rejection(reason_code="below_threshold"),),
    )


def _inputs(
    source_ids: tuple[str, ...],
) -> tuple[tuple[ChronologyGroup, ...], tuple[PlanSourceV2, ...]]:
    groups: list[ChronologyGroup] = []
    sources: list[PlanSourceV2] = []
    for index, source_id in enumerate(source_ids):
        candidate = SourceCandidate(
            path=Path(f"/media/{source_id}.mp4"),
            size_bytes=100,
            discovery_index=index,
            fingerprint=source_id,
            identity_version="bounded-v1",
        )
        groups.append(
            ChronologyGroup(
                group_id=f"chapter-{index + 1}",
                members=(
                    SequencedSource(
                        source=candidate,
                        parsed=None,
                        creation_time=None,
                        order_evidence="discovery_order",
                        confidence="low",
                    ),
                ),
            )
        )
        sources.append(
            PlanSourceV2(
                id=source_id,
                path=candidate.path,
                identity=f"sha256:{source_id}",
                duration=D("3600"),
                has_audio=True,
            )
        )
    return tuple(groups), tuple(sources)


def _track(candidate: RankedCandidate, *, eligible: bool = True) -> CropTrack:
    duration = candidate.source_end - candidate.source_start
    return CropTrack(
        track_id=f"track-{candidate.candidate_id}",
        clip_id=candidate.candidate_id,
        source_id=candidate.source_id,
        source_identity=f"sha256:{candidate.source_id}",
        source_width=3840,
        source_height=2160,
        output_width=1080,
        output_height=1920,
        crop_width=1215,
        crop_height=2160,
        observations=(),
        keyframes=(
            CropKeyframe(
                time=D("0"),
                center_x=D("0.5"),
                center_y=D("0.5"),
                subject_box_id=None,
                fallback="static",
            ),
            CropKeyframe(
                time=duration,
                center_x=D("0.5"),
                center_y=D("0.5"),
                subject_box_id=None,
                fallback="static",
            ),
        ),
        short_eligible=eligible,
    )


def _plan(
    candidates: list[RankedCandidate],
    source_ids: tuple[str, ...],
    *,
    settings: HighlightSettings | None = None,
    transitions: tuple[TransitionEvidence, ...] = (),
    anchor: AnchorMoment | None = None,
    tracks: tuple[CropTrack, ...] | None = None,
) -> HighlightPlanSet:
    chronology, sources = _inputs(source_ids)
    return create_highlight_plans(
        candidates,
        chronology,
        tracks if tracks is not None else tuple(_track(item) for item in candidates),
        sources,
        transitions,
        settings or HighlightSettings(),
        anchor=anchor,
    )


def test_long_plan_preserves_chronology_and_stops_before_weak_filler() -> None:
    candidates = [
        _candidate("late", "s2", "20", "40", category="story", event_id="late"),
        _candidate("weak", "s3", "0", "20", total="0.44", event_id="weak"),
        _candidate("early", "s1", "10", "30", category="human", event_id="early"),
    ]

    plans = _plan(candidates, ("s1", "s2", "s3"))

    assert [clip.clip_id for clip in plans.long.clips] == ["early", "late"]
    assert timeline_duration(plans.long) <= D("1800")
    assert plans.long_evidence.chapter_progression == ("chapter-1", "chapter-2")


def test_long_plan_balances_categories_and_keeps_complete_event_boundaries() -> None:
    candidates = [
        _candidate("action", "s1", "10", "20", category="action"),
        _candidate("scenic", "s1", "30", "40", category="scenic"),
        _candidate("human", "s1", "50", "60", category="human"),
        _candidate("story", "s1", "70", "80", category="story"),
        _candidate("incomplete", "s1", "90", "100", completeness="0.59"),
    ]

    plans = _plan(candidates, ("s1",))

    assert plans.long_evidence.category_distribution == (
        ("action", 1),
        ("human", 1),
        ("scenic", 1),
        ("story", 1),
    )
    by_id = {clip.clip_id: clip for clip in plans.long.clips}
    assert (by_id["action"].source_start, by_id["action"].source_end) == (
        D("10"),
        D("20"),
    )
    assert "incomplete" not in by_id


def test_short_count_is_zero_when_no_coherent_quality_cluster_exists() -> None:
    candidates = [
        _candidate("one", "s1", "0", "10", event_id="solo"),
        _candidate("low", "s1", "20", "30", event_id="low", total="0.44"),
    ]

    plans = _plan(candidates, ("s1",))

    assert plans.shorts == ()


def test_multiple_shorts_have_stable_names_themes_and_no_forbidden_reuse() -> None:
    candidates = [
        _candidate("ride-1", "s1", "0", "20", event_id="ride"),
        _candidate("ride-2", "s1", "30", "50", event_id="ride"),
        _candidate("camp-1", "s2", "0", "20", category="human", event_id="camp"),
        _candidate("camp-2", "s2", "30", "50", category="story", event_id="camp"),
    ]

    plans = _plan(candidates, ("s1", "s2"))

    assert plans.output_filenames == ("long.mp4", "short-01.mp4", "short-02.mp4")
    assert plans.themes == ("event:ride", "event:camp")
    assert plans.validation.short_count == 2
    assert plans.validation.validated is True


def test_short_planner_uses_quality_first_duration_and_configured_cap() -> None:
    candidates = [
        _candidate(
            f"{event}-{index}",
            source,
            str(index * 70),
            str(index * 70 + 60),
            event_id=event,
            total=score,
        )
        for event, source, score in (
            ("a", "s1", "0.90"),
            ("b", "s2", "0.80"),
            ("c", "s3", "0.70"),
        )
        for index in range(3)
    ]

    default = _plan(candidates, ("s1", "s2", "s3"))
    capped = _plan(
        candidates, ("s1", "s2", "s3"), settings=HighlightSettings(max_short_count=2)
    )
    disabled = _plan(
        candidates, ("s1", "s2", "s3"), settings=HighlightSettings(max_short_count=0)
    )

    assert len(default.shorts) == 3
    assert [timeline_duration(plan) for plan in default.shorts] == [D("180")] * 3
    assert capped.themes == ("event:a", "event:b")
    assert disabled.shorts == ()


def test_short_planner_respects_overlap_boundary_and_semantic_group_exclusion() -> None:
    exact = [
        _candidate("a1", "s1", "0", "10", event_id="a", dedup_group="g1"),
        _candidate("a2", "s2", "0", "10", event_id="a", dedup_group="g2"),
        _candidate("b1", "s1", "9", "19", event_id="b", dedup_group="g3"),
        _candidate("b2", "s3", "0", "10", event_id="b", dedup_group="g4"),
    ]
    over = [
        exact[0],
        exact[1],
        exact[2].model_copy(update={"source_start": D("8.9"), "source_end": D("18.9")}),
        exact[3],
    ]
    reused = [
        exact[0],
        exact[1],
        exact[2].model_copy(update={"dedup_group_id": "g1"}),
        exact[3],
    ]

    assert len(_plan(exact, ("s1", "s2", "s3")).shorts) == 2
    assert len(_plan(over, ("s1", "s2", "s3")).shorts) == 1
    assert len(_plan(reused, ("s1", "s2", "s3")).shorts) == 1


def test_recorded_anchor_may_appear_in_exactly_two_shorts_with_rationale() -> None:
    candidates = [
        _candidate("anchor", "s1", "0", "10", dedup_group="anchor"),
        *[
            _candidate(
                f"support-{event}-{index}",
                source,
                str(20 + index * 20),
                str(30 + index * 20),
                event_id=event,
            )
            for event, source in (("a", "s1"), ("b", "s2"), ("c", "s3"))
            for index in range(2)
        ],
    ]
    anchor = AnchorMoment(
        source_identity="sha256:s1",
        source_start=D("0"),
        source_end=D("10"),
        dedup_group="anchor",
        rationale="shared opening establishes the route",
    )

    plans = _plan(candidates, ("s1", "s2", "s3"), anchor=anchor)

    assert len(plans.shorts) == 3
    assert (
        sum(clip.clip_id == "anchor" for plan in plans.shorts for clip in plan.clips)
        == 2
    )
    assert plans.validation.anchor_rationale == anchor.rationale


def test_vertical_ineligibility_prevents_short_but_not_long_selection() -> None:
    candidates = [
        _candidate("a", "s1", "0", "10", event_id="event"),
        _candidate("b", "s1", "20", "30", event_id="event"),
    ]
    tracks = (_track(candidates[0]), _track(candidates[1], eligible=False))

    plans = _plan(candidates, ("s1",), tracks=tracks)

    assert plans.shorts == ()
    assert [clip.clip_id for clip in plans.long.clips] == ["a", "b"]


def test_semantic_transition_uses_approved_matrix_and_bounded_audio_policy() -> None:
    candidates = [
        _candidate("a", "s1", "0", "10", event_id="event"),
        _candidate("b", "s1", "20", "30", event_id="event"),
    ]
    evidence = TransitionEvidence(
        from_candidate_id="a",
        to_candidate_id="b",
        kind="dissolve",
        relation="same_event",
        reason="same event with a time shift",
        confidence=D("0.9"),
    )

    plans = _plan(candidates, ("s1",), transitions=(evidence,))
    transition = plans.long.transitions[0]

    assert transition.relation in ALLOWED_TRANSITIONS[transition.kind]
    assert transition.duration == D("0.5")
    assert transition.audio_policy == "crossfade"
    assert transition.duration < min(
        plans.long.clips[0].source_end - plans.long.clips[0].source_start,
        plans.long.clips[1].source_end - plans.long.clips[1].source_start,
    )


def test_important_action_boundary_is_not_hidden_by_noncut_transition() -> None:
    candidates = [
        _candidate("a", "s1", "0", "10", event_id="event"),
        _candidate("b", "s1", "20", "30", event_id="event"),
    ]
    evidence = TransitionEvidence(
        from_candidate_id="a",
        to_candidate_id="b",
        kind="dissolve",
        relation="same_event",
        reason="action remains important at boundary",
        confidence=D("0.9"),
        important_action_boundary=True,
    )

    plans = _plan(candidates, ("s1",), transitions=(evidence,))

    assert plans.long.transitions == []


def test_planning_is_deterministic_for_shuffled_inputs() -> None:
    candidates = [
        _candidate("a1", "s1", "0", "10", event_id="a"),
        _candidate("a2", "s1", "20", "30", event_id="a"),
        _candidate("b1", "s2", "0", "10", event_id="b"),
        _candidate("b2", "s2", "20", "30", event_id="b"),
    ]
    shuffled = candidates.copy()
    random.Random(7).shuffle(shuffled)

    first = _plan(candidates, ("s1", "s2"))
    second = _plan(shuffled, ("s1", "s2"))

    assert first == second
