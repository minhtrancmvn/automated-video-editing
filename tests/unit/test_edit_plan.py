import json
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from video_editor.models.edit_plan import (
    EditPlan,
    load_plan,
    timeline_duration,
    write_plan,
)

SCHEMA_PATH = Path(__file__).parents[2] / "schemas" / "edit-plan-v1.json"


def source(
    source_id: str = "a", *, duration: object = 30, has_audio: bool = True
) -> dict:
    return {
        "id": source_id,
        "path": f"/media/{source_id}.mp4",
        "identity": f"identity-{source_id}",
        "duration": duration,
        "has_audio": has_audio,
    }


def clip(
    source_id: str = "a",
    start: object = 0,
    end: object = 10,
    *,
    timeline_start: object = 0,
    speed: object = 1,
) -> dict:
    return {
        "source_id": source_id,
        "source_start": start,
        "source_end": end,
        "timeline_start": timeline_start,
        "speed": speed,
        "framing": {"mode": "center_crop"},
        "selection_reason": "phase1_sample",
    }


def valid_plan_data() -> dict:
    return {
        "schema_version": 1,
        "planner_version": "test-v1",
        "sources": [source()],
        "clips": [clip()],
        "transitions": [],
        "output": {
            "kind": "short",
            "width": 1920,
            "height": 1080,
            "frame_rate": "30",
            "codec": "libx264",
        },
        "provenance": {"planner": "test"},
    }


def test_speed_and_transition_control_duration() -> None:
    data = valid_plan_data()
    data["sources"] = [source(duration=30)]
    data["clips"] = [
        clip("a", 0, 10, timeline_start=0, speed=2),
        clip("a", 10, 20, timeline_start=4, speed=1),
    ]
    data["transitions"] = [
        {"from_clip": 0, "to_clip": 1, "kind": "dissolve", "duration": 1}
    ]
    plan = EditPlan.model_validate(data)
    assert timeline_duration(plan) == Decimal(14)


def test_long_form_must_be_strictly_below_one_hour() -> None:
    data = valid_plan_data()
    data["output"]["kind"] = "long"
    data["sources"] = [source(duration=3600)]
    data["clips"] = [clip("a", 0, 3600)]
    with pytest.raises(ValidationError, match="strictly shorter"):
        EditPlan.model_validate(data)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda d: d["clips"][0].update(source_id="missing"), "unknown source"),
        (lambda d: d["sources"][0].update(identity=""), "string_too_short"),
        (lambda d: d["clips"][0].update(source_start=-1), "greater than or equal"),
        (lambda d: d["clips"][0].update(source_end=31), "exceeds duration"),
        (lambda d: d["clips"][0].update(source_end=0), "greater than"),
        (lambda d: d["clips"][0].update(speed=0), "greater than 0"),
    ],
)
def test_invalid_sources_intervals_and_speed_are_rejected(
    mutator, message: str
) -> None:
    data = valid_plan_data()
    mutator(data)
    with pytest.raises(ValidationError, match=message):
        EditPlan.model_validate(data)


def test_gap_and_overlap_without_transition_are_rejected() -> None:
    data = valid_plan_data()
    data["sources"] = [source(duration=30)]
    data["clips"] = [clip(), clip("a", 10, 20, timeline_start=11)]
    with pytest.raises(ValidationError, match="gap or overlap"):
        EditPlan.model_validate(data)


def test_impossible_transition_is_rejected() -> None:
    data = valid_plan_data()
    data["sources"] = [source(duration=30)]
    data["clips"] = [clip(), clip("a", 10, 20, timeline_start=9)]
    data["transitions"] = [{"from_clip": 0, "to_clip": 1, "kind": "cut", "duration": 1}]
    with pytest.raises(ValidationError, match="cut transition"):
        EditPlan.model_validate(data)


def test_unsupported_primitives_and_missing_dimensions_are_rejected() -> None:
    data = valid_plan_data()
    data["clips"][0]["framing"] = {"mode": "smart_subject"}
    with pytest.raises(ValidationError, match="literal"):
        EditPlan.model_validate(data)

    data = valid_plan_data()
    del data["output"]["width"]
    with pytest.raises(ValidationError, match="width"):
        EditPlan.model_validate(data)


@pytest.mark.parametrize("field", ["command", "executable", "shell"])
def test_plan_data_forbids_shell_command_fields(field: str) -> None:
    data = valid_plan_data()
    data["provenance"][field] = "ffmpeg -i input.mp4 output.mp4"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EditPlan.model_validate(data)


def test_nested_provenance_shell_command_fields_are_rejected() -> None:
    data = valid_plan_data()
    data["provenance"]["metadata"] = {"details": {"command": "ffmpeg"}}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EditPlan.model_validate(data)


def test_missing_audio_plan_is_rejected_when_referenced_source_has_no_audio() -> None:
    data = valid_plan_data()
    data["sources"] = [source(has_audio=False)]
    with pytest.raises(ValidationError, match="no audio"):
        EditPlan.model_validate(data)


def test_unreferenced_source_without_audio_does_not_reject_plan() -> None:
    data = valid_plan_data()
    data["sources"] = [source(has_audio=True), source("unused", has_audio=False)]
    plan = EditPlan.model_validate(data)
    assert plan.sources[-1].has_audio is False


def test_phase_one_rejects_unsupported_output_codec() -> None:
    data = valid_plan_data()
    data["output"]["codec"] = "hevc"
    with pytest.raises(ValidationError, match="libx264"):
        EditPlan.model_validate(data)


def test_schema_version_and_checked_in_schema() -> None:
    assert json.loads(SCHEMA_PATH.read_text()) == EditPlan.model_json_schema()
    Draft202012Validator.check_schema(json.loads(SCHEMA_PATH.read_text()))
    data = valid_plan_data()
    data["schema_version"] = 2
    with pytest.raises(ValidationError):
        EditPlan.model_validate(data)


def test_transition_zero_duration_string_has_schema_model_parity() -> None:
    data = valid_plan_data()
    data["clips"].append(clip("a", 10, 20, timeline_start=10))
    data["transitions"] = [
        {"from_clip": 0, "to_clip": 1, "kind": "cut", "duration": "0"}
    ]

    schema_errors = list(
        Draft202012Validator(json.loads(SCHEMA_PATH.read_text())).iter_errors(data)
    )

    assert not schema_errors
    plan = EditPlan.model_validate(data)
    assert plan.transitions[0].duration == 0


@pytest.mark.parametrize(
    ("location", "value"),
    [
        (("sources", 0, "duration"), "-1"),
        (("clips", 0, "speed"), "-1"),
        (
            ("transitions",),
            [{"from_clip": 0, "to_clip": 1, "kind": "cut", "duration": "-1"}],
        ),
        (("clips", 0, "confidence"), "-0.1"),
    ],
)
def test_checked_in_json_schema_rejects_negative_decimal_strings(
    location: tuple[object, ...], value: object
) -> None:
    data = valid_plan_data()
    if location == ("transitions",):
        data["transitions"] = value
    else:
        target: object = data
        for key in location[:-1]:
            target = target[key]  # type: ignore[index]
        target[location[-1]] = value  # type: ignore[index]
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))
    assert list(validator.iter_errors(data))
    with pytest.raises(ValidationError):
        EditPlan.model_validate(data)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", Decimal(0)), ("0.5", Decimal("0.5")), ("1", Decimal(1))],
)
def test_confidence_decimal_strings_have_schema_model_parity(
    value: str, expected: Decimal
) -> None:
    data = valid_plan_data()
    data["clips"][0]["confidence"] = value

    schema_errors = list(
        Draft202012Validator(json.loads(SCHEMA_PATH.read_text())).iter_errors(data)
    )

    assert not schema_errors
    plan = EditPlan.model_validate(data)
    assert plan.clips[0].confidence == expected


@pytest.mark.parametrize(
    ("location", "value"),
    [
        (("sources", 0, "duration"), "0"),
        (("clips", 0, "source_end"), "0"),
        (("clips", 0, "speed"), "0"),
        (("output", "frame_rate"), "0"),
        (("clips", 0, "confidence"), "1.1"),
        (("clips", 0, "confidence"), "-0.1"),
        (("clips", 0, "confidence"), "1e-1"),
        (("sources", 0, "duration"), "1e1"),
        (("clips", 0, "timeline_start"), ""),
        (("clips", 0, "timeline_start"), "+"),
        (("clips", 0, "timeline_start"), "."),
        (("clips", 0, "timeline_start"), "+."),
    ],
)
def test_schema_and_model_reject_same_numeric_values(
    location: tuple[str | int, ...], value: str
) -> None:
    data = valid_plan_data()
    target: object = data
    for key in location[:-1]:
        target = target[key]  # type: ignore[index]
    target[location[-1]] = value  # type: ignore[index]

    schema_result = list(
        Draft202012Validator(json.loads(SCHEMA_PATH.read_text())).iter_errors(data)
    )
    model_result = True
    try:
        EditPlan.model_validate(data)
    except ValidationError:
        model_result = False
    assert bool(schema_result) is (not model_result)


def test_load_and_write_plan_round_trip(tmp_path: Path) -> None:
    plan = EditPlan.model_validate(valid_plan_data())
    path = tmp_path / "plan.json"
    write_plan(plan, path)
    assert load_plan(path) == plan
