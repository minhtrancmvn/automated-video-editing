import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.models.edit_plan import (
    AnchorMoment,
    EditPlanV2,
    PlanSetPolicy,
    load_plan,
    load_plan_document,
    timeline_duration,
    validate_plan_set,
    write_plan,
)

SCHEMA_PATH = Path(__file__).parents[2] / "schemas" / "edit-plan-v2.json"


def score_breakdown() -> dict[str, Any]:
    return {
        "positive": {
            "action": "0.8",
            "scenic": "0.2",
            "human": "0.5",
            "story": "0.7",
            "technical": "0.9",
            "novelty": "0.6",
            "completeness": "0.8",
            "long_story": "0.7",
            "short": "0.8",
            "vertical": "0.9",
        },
        "penalties": {
            "blur_exposure": "0",
            "shake_obstruction": "0",
            "incomplete": "0",
            "weak_boundary": "0",
            "repetition": "0",
            "overlap": "0",
        },
    }


def source(
    source_id: str = "source-a",
    *,
    identity: str = "sha256:source-a",
    duration: object = 2000,
    has_audio: bool = True,
) -> dict[str, Any]:
    return {
        "id": source_id,
        "path": f"/media/{source_id}.mp4",
        "identity": identity,
        "duration": duration,
        "has_audio": has_audio,
    }


def clip(
    *,
    clip_id: str = "clip-a",
    source_id: str = "source-a",
    source_identity: str = "sha256:source-a",
    start: object = 0,
    end: object = 10,
    timeline_start: object = 0,
    dedup_group: str = "group-a",
    framing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "clip_id": clip_id,
        "source_id": source_id,
        "source_start": start,
        "source_end": end,
        "timeline_start": timeline_start,
        "speed": "1",
        "framing": framing or {"mode": "center_crop"},
        "selection_reason": "ranked highlight",
        "overall_score": "0.8",
        "score_breakdown": score_breakdown(),
        "analysis_reference": f"analysis:{clip_id}",
        "source_proxy_mapping_reference": f"mapping:{clip_id}",
        "dedup_group": dedup_group,
        "chapter_id": "chapter-1",
        "event_id": "event-1",
        "vertical_suitability": "0.9",
        "confidence": "0.95",
        "planner_version": "highlight-plan-v1",
        "analysis_version": "analysis-v1",
        "source_identity": source_identity,
    }


def valid_plan(
    *,
    kind: str = "short",
    filename: str = "short-01.mp4",
    plan_id: str = "plan-short-01",
    start: object = 0,
    end: object = 10,
    dedup_group: str = "group-a",
    source_id: str = "source-a",
    source_identity: str = "sha256:source-a",
) -> dict[str, Any]:
    width, height = (1080, 1920) if kind == "short" else (1920, 1080)
    return {
        "schema_version": 2,
        "planner_version": "highlight-plan-v1",
        "analysis_version": "analysis-v1",
        "sources": [source(source_id, identity=source_identity, duration=2000)],
        "clips": [
            clip(
                source_id=source_id,
                source_identity=source_identity,
                start=start,
                end=end,
                dedup_group=dedup_group,
            )
        ],
        "transitions": [],
        "output": {
            "plan_id": plan_id,
            "filename": filename,
            "kind": kind,
            "width": width,
            "height": height,
            "frame_rate": "30",
            "codec": "libx264",
            "audio": "source",
            "theme_summary": "strong action moments" if kind == "short" else None,
        },
        "provenance": {"planner": "highlight-planner"},
    }


def plan(**kwargs: Any) -> EditPlanV2:
    return EditPlanV2.model_validate(valid_plan(**kwargs))


def test_v2_output_identity_dimensions_filenames_and_duration_caps() -> None:
    long_plan = plan(kind="long", filename="long.mp4", plan_id="plan-long", end=1800)
    assert timeline_duration(long_plan) == Decimal(1800)
    assert long_plan.output.plan_id == "plan-long"

    with pytest.raises(ValidationError, match="180 seconds"):
        plan(end=181)
    with pytest.raises(ValidationError, match="1,800 seconds"):
        plan(kind="long", filename="long.mp4", plan_id="plan-long", end=1801)

    invalid_dimensions = valid_plan()
    invalid_dimensions["output"].update(width=1920, height=1080)
    with pytest.raises(ValidationError, match="1080x1920"):
        EditPlanV2.model_validate(invalid_dimensions)

    invalid_filename = valid_plan(filename="short.mp4")
    with pytest.raises(ValidationError, match="short-NN.mp4"):
        EditPlanV2.model_validate(invalid_filename)


@pytest.mark.parametrize(
    ("kind", "relation"),
    [
        ("cut", "continuous_action"),
        ("cut", "matched_motion"),
        ("cut", "same_event"),
        ("dissolve", "same_event"),
        ("dissolve", "same_place_time_shift"),
        ("fade", "chapter_boundary"),
        ("fade", "story_open_close"),
        ("fade_black", "chapter_boundary"),
        ("fade_black", "time_jump"),
        ("fade_black", "location_change"),
    ],
)
def test_transition_relation_matrix_accepts_approved_pairs(
    kind: str, relation: str
) -> None:
    data = valid_plan()
    data["sources"][0]["duration"] = 30
    data["clips"] = [
        clip(end=10),
        clip(
            clip_id="clip-b",
            start=10,
            end=20,
            timeline_start="9.5" if kind != "cut" else 10,
            dedup_group="group-b",
        ),
    ]
    data["transitions"] = [
        {
            "from_clip": 0,
            "to_clip": 1,
            "kind": kind,
            "duration": "0.5" if kind != "cut" else "0",
            "relation": relation,
            "reason": "semantic boundary",
            "confidence": "0.9",
            "audio_policy": "cut" if kind == "cut" else "crossfade",
        }
    ]
    EditPlanV2.model_validate(data)


@pytest.mark.parametrize(
    ("kind", "relation"),
    [
        ("cut", "location_change"),
        ("dissolve", "time_jump"),
        ("fade", "same_event"),
        ("fade_black", "continuous_action"),
    ],
)
def test_transition_relation_matrix_rejects_unapproved_pairs(
    kind: str, relation: str
) -> None:
    data = valid_plan()
    data["transitions"] = [
        {
            "from_clip": 0,
            "to_clip": 1,
            "kind": kind,
            "duration": "0",
            "relation": relation,
            "reason": "variety",
            "confidence": "0.5",
            "audio_policy": "cut",
        }
    ]
    with pytest.raises(ValidationError, match="not allowed"):
        EditPlanV2.model_validate(data)


def test_v2_requires_score_evidence_and_version_references() -> None:
    data = valid_plan()
    expected = {
        "overall_score",
        "score_breakdown",
        "analysis_reference",
        "source_proxy_mapping_reference",
        "dedup_group",
        "vertical_suitability",
        "confidence",
        "planner_version",
        "analysis_version",
    }
    assert expected <= set(data["clips"][0])

    for field in expected:
        invalid = deepcopy(data)
        del invalid["clips"][0][field]
        with pytest.raises(ValidationError, match=field):
            EditPlanV2.model_validate(invalid)


def test_tracked_crop_requires_matching_clip_and_source_identity() -> None:
    tracked = {
        "mode": "tracked_crop",
        "track_id": "track-1",
        "track_clip_id": "clip-a",
        "track_source_identity": "sha256:source-a",
        "keyframes": [
            {
                "time": "0",
                "center_x": "0.5",
                "center_y": "0.5",
                "subject_box_id": "subject-1",
                "fallback": "tracked",
            },
            {
                "time": "10",
                "center_x": "0.6",
                "center_y": "0.5",
                "subject_box_id": None,
                "fallback": "ease_center",
            },
        ],
    }
    data = valid_plan()
    data["clips"][0]["framing"] = tracked
    EditPlanV2.model_validate(data)

    for field, value in [
        ("track_clip_id", "other-clip"),
        ("track_source_identity", "sha256:other-source"),
    ]:
        invalid = deepcopy(data)
        invalid["clips"][0]["framing"][field] = value
        with pytest.raises(ValidationError, match="tracked crop"):
            EditPlanV2.model_validate(invalid)

    nonmonotonic = deepcopy(data)
    nonmonotonic["clips"][0]["framing"]["keyframes"][1]["time"] = "0"
    with pytest.raises(ValidationError, match="strictly increasing"):
        EditPlanV2.model_validate(nonmonotonic)

    stale_track_identity = valid_plan()
    stale_track_identity["clips"][0]["framing"]["track_clip_id"] = "clip-a"
    with pytest.raises(ValidationError, match="tracked crop data"):
        EditPlanV2.model_validate(stale_track_identity)


def test_plan_set_rejects_duplicate_plan_ids_filenames_and_excess_counts() -> None:
    first = plan()
    duplicate_filename = plan(
        filename="short-01.mp4",
        plan_id="plan-short-02",
        start=20,
        end=30,
        dedup_group="group-b",
    )
    with pytest.raises(ValueError, match="filename"):
        validate_plan_set([first, duplicate_filename], PlanSetPolicy(max_shorts=5))

    duplicate_id = plan(
        filename="short-02.mp4",
        plan_id="plan-short-01",
        start=20,
        end=30,
        dedup_group="group-b",
    )
    with pytest.raises(ValueError, match="plan ID"):
        validate_plan_set([first, duplicate_id], PlanSetPolicy(max_shorts=5))

    second_long = plan(
        kind="long",
        filename="long.mp4",
        plan_id="plan-long-2",
        start=20,
        end=30,
        dedup_group="group-b",
    )
    first_long = plan(kind="long", filename="long.mp4", plan_id="plan-long")
    with pytest.raises(ValueError, match="one long"):
        validate_plan_set([first_long, second_long], PlanSetPolicy(max_shorts=5))

    with pytest.raises(ValueError, match="short count"):
        validate_plan_set([first], PlanSetPolicy(max_shorts=0))


def test_cross_short_overlap_accepts_exactly_ten_percent_and_rejects_more() -> None:
    first = plan(start=0, end=10)
    boundary = plan(
        filename="short-02.mp4",
        plan_id="plan-short-02",
        start=9,
        end=19,
        dedup_group="group-b",
    )
    validate_plan_set([first, boundary], PlanSetPolicy(max_shorts=5))

    over = plan(
        filename="short-02.mp4",
        plan_id="plan-short-02",
        start="8.9",
        end="18.9",
        dedup_group="group-b",
    )
    with pytest.raises(ValueError, match="overlap"):
        validate_plan_set([first, over], PlanSetPolicy(max_shorts=5))


def test_cross_short_overlap_uses_source_identity_not_local_source_id() -> None:
    first = plan(source_id="shared-id", source_identity="sha256:first")
    same_local_id_different_source = plan(
        filename="short-02.mp4",
        plan_id="plan-short-02",
        source_id="shared-id",
        source_identity="sha256:second",
        dedup_group="group-b",
    )
    validate_plan_set(
        [first, same_local_id_different_source], PlanSetPolicy(max_shorts=5)
    )

    different_local_id_same_source = plan(
        filename="short-02.mp4",
        plan_id="plan-short-02",
        source_id="other-id",
        source_identity="sha256:first",
        dedup_group="group-b",
    )
    with pytest.raises(ValueError, match="overlap"):
        validate_plan_set(
            [first, different_local_id_same_source], PlanSetPolicy(max_shorts=5)
        )


def test_cross_short_semantic_groups_require_bounded_recorded_anchor() -> None:
    first = plan()
    second = plan(
        filename="short-02.mp4",
        plan_id="plan-short-02",
        start=20,
        end=30,
        dedup_group="group-a",
    )
    with pytest.raises(ValueError, match="semantic group"):
        validate_plan_set([first, second], PlanSetPolicy(max_shorts=5))

    same_group_different_source = plan(
        filename="short-02.mp4",
        plan_id="plan-short-02",
        source_identity="sha256:other-source",
        dedup_group="group-a",
    )
    with pytest.raises(ValueError, match="semantic group"):
        validate_plan_set(
            [first, same_group_different_source], PlanSetPolicy(max_shorts=5)
        )

    anchor = AnchorMoment(
        source_identity="sha256:source-a",
        source_start="0",
        source_end="10",
        dedup_group="group-a",
        rationale="opening context connects both reels",
    )
    unrelated_anchor = AnchorMoment(
        source_identity="sha256:source-a",
        source_start="40",
        source_end="50",
        dedup_group="group-z",
        rationale="different recorded anchor",
    )
    with pytest.raises(ValueError, match="semantic group"):
        validate_plan_set(
            [first, second], PlanSetPolicy(max_shorts=5, anchor=unrelated_anchor)
        )

    policy = PlanSetPolicy(max_shorts=5, anchor=anchor)
    second_anchor = plan(
        filename="short-02.mp4",
        plan_id="plan-short-02",
        dedup_group="group-a",
    )
    validate_plan_set([first, second_anchor], policy)

    third = plan(
        filename="short-03.mp4",
        plan_id="plan-short-03",
        dedup_group="group-a",
    )
    with pytest.raises(ValueError, match="at most two shorts"):
        validate_plan_set([first, second_anchor, third], policy)

    with pytest.raises(ValidationError, match="rationale"):
        AnchorMoment(
            source_identity="sha256:source-a",
            source_start="0",
            source_end="10",
            dedup_group="group-a",
            rationale="",
        )


def test_v2_load_write_union_dispatch_and_schema_parity(tmp_path: Path) -> None:
    v2 = plan()
    path = tmp_path / "edit-plan-short-01.json"
    write_plan(v2, path)
    loaded = load_plan_document(path)
    assert type(loaded) is EditPlanV2
    assert loaded == v2

    with pytest.raises(VideoEditorError) as error:
        load_plan(path)
    assert error.value.category is ErrorCategory.PLAN
    assert str(error.value) == (
        "edit plan schema version 2 is not supported by Phase 1 workflow"
    )

    schema = json.loads(SCHEMA_PATH.read_text())
    assert schema == EditPlanV2.model_json_schema()
    Draft202012Validator.check_schema(schema)
    assert not list(Draft202012Validator(schema).iter_errors(valid_plan()))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("clips", 0, "overall_score"), "NaN"),
        (("clips", 0, "confidence"), "1e-1"),
        (("clips", 0, "vertical_suitability"), "1.1"),
        (("output", "frame_rate"), "1e1"),
        (("clips", 0, "score_breakdown", "positive", "action"), "1e-1"),
        (("clips", 0, "score_breakdown", "penalties", "overlap"), "NaN"),
        (("clips", 0, "framing", "keyframes"), []),
    ],
)
def test_v2_schema_and_model_reject_same_invalid_decimal_values(
    path: tuple[str | int, ...], value: object
) -> None:
    data = valid_plan()
    if path[-1] == "keyframes":
        data["clips"][0]["framing"] = {
            "mode": "tracked_crop",
            "track_id": "track-1",
            "track_clip_id": "clip-a",
            "track_source_identity": "sha256:source-a",
            "keyframes": value,
        }
    else:
        target: Any = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    schema_errors = list(
        Draft202012Validator(json.loads(SCHEMA_PATH.read_text())).iter_errors(data)
    )
    assert schema_errors
    with pytest.raises(ValidationError):
        EditPlanV2.model_validate(data)
