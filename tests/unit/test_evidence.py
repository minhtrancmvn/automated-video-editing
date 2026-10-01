"""Unit tests for candidate-v2 subject seeds, evidence-map-v1, and transitions."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from video_editor.analysis.evidence import (
    EVIDENCE_MAP_VERSION,
    derive_transition_evidence,
    map_ranking_evidence,
    subject_seeds,
)
from video_editor.analysis.models import (
    AnalysisBoundaryKind,
    AnalysisChunkData,
    BroadCandidate,
    CandidateRefinementResponse,
    LocalSegmentation,
)
from video_editor.analysis.ranking import RankingSettings, rank_candidates
from video_editor.planning.highlight_plan import ALLOWED_TRANSITIONS

D = Decimal


def _refined(**overrides: Any) -> CandidateRefinementResponse:
    data: dict[str, Any] = {
        "schema_version": "candidate-v2",
        "chunk_id": "chunk-1",
        "candidate_id": "c-1",
        "start": "1",
        "end": "3",
        "action_completeness": 0.8,
        "visual_composition": 0.7,
        "novelty": 0.6,
        "semantic_importance": 0.5,
        "duplicate_similarity": 0.1,
        "vertical_subject_priority": "center",
        "crop_intent": "follow",
        "adjacent_scene_compatibility": 0.4,
        "confidence": 0.9,
        "subject_boxes": [
            {"time": "1.5", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4, "priority": 0}
        ],
    }
    data.update(overrides)
    return CandidateRefinementResponse.model_validate(data)


def _chunk(source_id: str = "s-1", source_start: str = "10") -> AnalysisChunkData:
    return AnalysisChunkData(
        schema_version=1,
        job_id="job",
        source_id=source_id,
        source_identity=f"identity:{source_id}",
        mapping_version="m-1",
        source_start=D(source_start),
        source_end=D(source_start) + 5,
        proxy_start=D(0),
        proxy_end=D(5),
        boundary_kind=AnalysisBoundaryKind.SCENE,
        implementation_version="cloud-proxy-v1",
    )


def _segmentation() -> LocalSegmentation:
    empty: tuple[()] = ()
    return LocalSegmentation(
        schema_version=1,
        implementation_version="seg",
        settings_hash="h",
        source_id="s-1",
        source_identity="identity:s-1",
        proxy_settings_hash="p",
        proxy_tool_version="ffmpeg",
        scenes=empty,
        silence_ranges=empty,
        speech_presence_ranges=empty,
        audio_energy=empty,
        audio_transients=empty,
        motion=empty,
        motion_continuity=empty,
        blur=empty,
        shake=empty,
        exposure=empty,
        obstruction=empty,
        boundary_suitability=empty,
        candidate_windows=empty,
    )


def _broad(category: str = "action") -> BroadCandidate:
    return BroadCandidate.model_validate(
        {
            "candidate_id": "c-1",
            "start": "1",
            "end": "3",
            "category": category,
            "reason": "fixture",
            "confidence": 0.95,
        }
    )


def test_candidate_v1_schema_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _refined(schema_version="candidate-v1")


@pytest.mark.parametrize(
    "box",
    [
        {"time": "3.5", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "priority": 0},
        {"time": "2", "x": 0.9, "y": 0.1, "w": 0.2, "h": 0.2, "priority": 0},
        {"time": "2", "x": 0.1, "y": 0.1, "w": 0.0, "h": 0.2, "priority": 0},
    ],
)
def test_subject_boxes_must_be_inside_interval_and_frame(box: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _refined(subject_boxes=[box])


def test_subject_boxes_become_source_time_seeds_only() -> None:
    seeds = subject_seeds(_refined(), _chunk())
    assert len(seeds) == 1
    assert seeds[0].origin == "gemini_seed"
    assert seeds[0].source_time == D("11.5")
    assert seeds[0].box.x_max == D("0.4")
    assert seeds[0].source_identity == "identity:s-1"


def test_evidence_map_v1_is_fixed_and_source_mapped() -> None:
    evidence = map_ranking_evidence(_broad(), _refined(), _chunk(), _segmentation())
    assert EVIDENCE_MAP_VERSION == "evidence-map-v1"
    assert (evidence.source_start, evidence.source_end) == (D(11), D(13))
    assert evidence.action == evidence.completeness == D("0.8")
    assert evidence.scenic == evidence.short == evidence.vertical == D("0.7")
    assert evidence.human == evidence.story == D("0.5")
    assert evidence.incomplete == D("0.2")
    assert evidence.technical == D(1)
    assert evidence.confidence == D("0.9")
    assert evidence.evidence_ids[:2] == ("analysis:c-1", "mapping:chunk-1")
    no_boxes = map_ranking_evidence(
        _broad(), _refined(subject_boxes=[]), _chunk(), _segmentation()
    )
    assert no_boxes.vertical == D(0)


def _ranked(specs: list[tuple[str, str, str, str]]) -> list[Any]:
    evidence = []
    for candidate_id, source_id, start, end in specs:
        item = map_ranking_evidence(
            _broad(),
            _refined(candidate_id=candidate_id, start=start, end=end, subject_boxes=[]),
            _chunk(source_id, "0"),
            _segmentation(),
        )
        evidence.append(item)
    ranked = rank_candidates(evidence, RankingSettings(max_source_share=D(1)))
    order = {spec[0]: index for index, spec in enumerate(specs)}
    return sorted(ranked, key=lambda item: order[item.candidate_id])


def test_transition_relations_follow_fixed_precedence() -> None:
    ranked = _ranked(
        [
            ("a", "s-1", "0", "1"),
            ("b", "s-1", "1", "2"),
            ("c", "s-1", "2.5", "3"),
            ("d", "s-2", "0", "1"),
            ("e", "s-3", "0", "1"),
        ]
    )
    chapters = {"s-1": "ch-1", "s-2": "ch-1", "s-3": "ch-2"}
    evidence = {
        (item.from_candidate_id, item.to_candidate_id): item
        for item in derive_transition_evidence(ranked, chapters)
    }
    assert evidence[("a", "b")].relation == "story_open_close"
    assert evidence[("d", "e")].relation == "story_open_close"
    assert evidence[("b", "c")].relation == "same_place_time_shift"
    assert evidence[("c", "d")].relation == "time_jump"
    assert evidence[("b", "d")].relation == "time_jump"
    assert all(
        item.relation in ALLOWED_TRANSITIONS[item.kind] for item in evidence.values()
    )
    assert derive_transition_evidence(ranked, chapters) == tuple(evidence.values())


def test_transition_relations_cover_chapter_location_and_event() -> None:
    ranked = _ranked(
        [
            ("a", "s-1", "0", "1"),
            ("b", "s-1", "1", "2"),
            ("c", "s-1", "2", "3"),
            ("d", "s-1", "4", "5"),
            ("e", "s-2", "0", "1"),
            ("f", "s-2", "1", "2"),
        ]
    )
    ranked = [
        item.model_copy(update={"event_id": "ev"})
        if item.candidate_id in "bcd"
        else item
        for item in ranked
    ]
    ranked = [
        item.model_copy(update={"location_id": "loc-b"})
        if item.candidate_id == "f"
        else item.model_copy(update={"location_id": "loc-a"})
        for item in ranked
    ]
    chapters = {"s-1": "ch-1", "s-2": "ch-2"}
    evidence = {
        (item.from_candidate_id, item.to_candidate_id): item.relation
        for item in derive_transition_evidence(ranked, chapters)
    }
    assert evidence[("b", "c")] == "continuous_action"
    assert evidence[("c", "d")] == "same_event"
    assert evidence[("d", "e")] == "chapter_boundary"
    chapters["s-2"] = "ch-1"
    relations = {
        (item.from_candidate_id, item.to_candidate_id): item.relation
        for item in derive_transition_evidence(ranked, chapters)
    }
    assert relations[("b", "e")] == "time_jump"
    assert relations[("e", "f")] == "story_open_close"
    ranked[4] = ranked[4].model_copy(update={"location_id": "loc-b"})
    relations = {
        (item.from_candidate_id, item.to_candidate_id): item.relation
        for item in derive_transition_evidence(ranked, chapters)
    }
    assert relations[("d", "e")] == "location_change"


def test_gemini_maximum_request_cost_fails_closed_without_pricing() -> None:
    from video_editor.analysis.gemini import GeminiAdapter
    from video_editor.errors import VideoEditorError

    adapter = GeminiAdapter(client=object())
    with pytest.raises(VideoEditorError) as caught:
        adapter.maximum_request_cost("m-1", _chunk(), prompt_version="broad-v1")
    assert caught.value.code == "provider_pricing_unavailable"


def _scene(scene_id: str, setting: str, start: str = "0", end: str = "5") -> Any:
    from video_editor.analysis.models import BroadScene

    return BroadScene.model_validate(
        {
            "scene_id": scene_id,
            "start": start,
            "end": end,
            "summary": "fixture scene",
            "actions": ["walk"],
            "setting": setting,
            "scenic_interest": 0.5,
            "human_interaction": 0.5,
            "story_milestone": False,
            "technical_problems": [],
            "vertical_suitability": 0.5,
            "confidence": 0.9,
        }
    )


def test_evidence_map_takes_event_and_location_from_overlapping_scene() -> None:
    evidence = map_ranking_evidence(
        _broad(),
        _refined(),
        _chunk(),
        _segmentation(),
        scenes=(_scene("scene-a", " Beach "), _scene("scene-b", "Pier", "4", "5")),
    )
    assert evidence.event_id == "chunk-1:scene-a"
    assert evidence.exact_event_id == "chunk-1:scene-a@11-13"
    assert evidence.location_id == "beach"
    bare = map_ranking_evidence(_broad(), _refined(), _chunk(), _segmentation())
    assert (bare.event_id, bare.exact_event_id, bare.location_id) == (None, None, None)


def _scene_ranked(
    specs: list[tuple[str, str, str, str, str]], settings: RankingSettings
) -> list[Any]:
    evidence = [
        map_ranking_evidence(
            _broad(),
            _refined(candidate_id=candidate_id, start=start, end=end, subject_boxes=[]),
            _chunk("s-1", "0"),
            _segmentation(),
            scenes=(_scene(scene_id, setting, "0", "5"),),
        )
        for candidate_id, start, end, scene_id, setting in specs
    ]
    ranked = rank_candidates(evidence, settings)
    order = {spec[0]: index for index, spec in enumerate(specs)}
    return sorted(ranked, key=lambda item: order[item.candidate_id])


def test_scene_identity_makes_event_and_location_transitions_reachable() -> None:
    specs = [
        ("a", "0", "1", "s1", "beach"),
        ("b", "1", "2", "s1", "beach"),
        ("c", "2", "3", "s1", "beach"),
        ("d", "3.5", "4", "s1", "beach"),
        ("e", "4", "4.5", "s2", "pier"),
        ("f", "4.5", "5", "s2", "pier"),
    ]
    settings = RankingSettings(
        max_source_share=D(1),
        max_event_share=D(1),
        max_location_share=D(1),
        max_category_share=D(1),
    )
    evidence = [
        map_ranking_evidence(
            _broad(),
            _refined(candidate_id=cid, start=start, end=end, subject_boxes=[]),
            _chunk("s-1", "0"),
            _segmentation(),
            scenes=(_scene(scene, setting, start, end),),
        )
        for cid, start, end, scene, setting in specs
    ]
    ranked = sorted(
        rank_candidates(evidence, settings), key=lambda item: item.candidate_id
    )
    relations = {
        (item.from_candidate_id, item.to_candidate_id): item.relation
        for item in derive_transition_evidence(ranked, {"s-1": "ch-1"})
    }
    assert relations[("d", "e")] == "location_change"
    same_scene = sorted(
        _scene_ranked(specs, settings), key=lambda item: item.candidate_id
    )
    relations = {
        (item.from_candidate_id, item.to_candidate_id): item.relation
        for item in derive_transition_evidence(same_scene, {"s-1": "ch-1"})
    }
    assert relations[("b", "c")] == "continuous_action"
    assert relations[("c", "d")] == "same_event"


def test_scene_identity_activates_ranking_event_and_location_quotas() -> None:
    specs = [
        (f"c{index}", str(index), str(index + 1), "s1", "beach") for index in range(4)
    ]
    ranked = _scene_ranked(
        specs,
        RankingSettings(
            max_source_share=D(1),
            max_category_share=D(1),
            max_location_share=D(1),
            max_event_share=D("0.25"),
            max_selected=4,
        ),
    )
    assert any("event_quota" in item.reason_codes for item in ranked)
    ranked = _scene_ranked(
        specs,
        RankingSettings(
            max_source_share=D(1),
            max_category_share=D(1),
            max_event_share=D(1),
            max_location_share=D("0.25"),
            max_selected=4,
        ),
    )
    assert any("location_quota" in item.reason_codes for item in ranked)
