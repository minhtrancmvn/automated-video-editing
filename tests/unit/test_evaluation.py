"""Offline evaluation formulas and fail-closed release gates."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from video_editor.evaluation import (
    EvaluationDataset,
    EvaluationError,
    EvaluationPredictions,
    EvaluationThresholds,
    check_release_gate,
    evaluate_predictions,
)

FIXTURES = Path(__file__).parents[1] / "evaluation"


def _dataset() -> EvaluationDataset:
    return EvaluationDataset.model_validate_json(
        (FIXTURES / "synthetic-labels.json").read_text()
    )


def _predictions() -> EvaluationPredictions:
    return EvaluationPredictions.model_validate(
        {
            "selected_ids": ["a", "b", "d", "w4"],
            "duplicate_pairs": [["a", "d"]],
            "ordered_ids": ["a", "b", "d", "w4"],
            "crop_samples": [{"interval_id": "a", "retained": 19, "sampled": 20}],
            "boundaries": [
                {
                    "from_id": "a",
                    "to_id": "b",
                    "kind": "cut",
                    "relation": "same_event",
                    "unrelated": False,
                },
                {
                    "from_id": "b",
                    "to_id": "d",
                    "kind": "cut",
                    "relation": "location_change",
                    "unrelated": True,
                },
                {
                    "from_id": "d",
                    "to_id": "w4",
                    "kind": "fade",
                    "relation": "chapter_boundary",
                    "unrelated": False,
                },
                {
                    "from_id": "w4",
                    "to_id": "a",
                    "kind": "dissolve",
                    "relation": "same_event",
                    "unrelated": False,
                },
                {
                    "from_id": "a",
                    "to_id": "d",
                    "kind": "fade_black",
                    "relation": "time_jump",
                    "unrelated": False,
                },
            ],
            "outputs": [
                {
                    "kind": "long",
                    "duration_seconds": "8",
                    "selected_ids": ["a", "b", "d", "w4"],
                },
                {"kind": "short", "duration_seconds": "3", "selected_ids": ["a", "b"]},
                {"kind": "short", "duration_seconds": "3", "selected_ids": ["d", "w4"]},
            ],
            "human_preference_wins": 1,
            "human_preference_trials": 2,
            "source_hours": "0.01",
            "spent_usd": "0.001",
            "source_safe": True,
            "schema_valid": True,
        }
    )


def _thresholds() -> EvaluationThresholds:
    return EvaluationThresholds.model_validate_json(
        (FIXTURES / "thresholds-v1.json").read_text()
    )


def test_fixed_metric_formulas() -> None:
    metrics = evaluate_predictions(_dataset(), _predictions())

    assert metrics.must_include_recall == Decimal(2) / 3
    assert metrics.weak_rejection == Decimal(3) / 4
    assert metrics.duplicate_rate == Decimal(1) / 6
    assert metrics.abrupt_cut_rate == Decimal(1) / 5
    assert metrics.crop_retention == Decimal("0.95")
    assert metrics.chronology_correct is True
    assert metrics.short_diversity == Decimal(1)
    assert metrics.human_preference == Decimal("0.5")
    assert metrics.duration_compliant is True
    assert metrics.budget_compliant is True


def test_release_gate_rejects_small_or_leaky_dataset() -> None:
    dataset = _dataset()
    with pytest.raises(EvaluationError, match="100 candidate intervals"):
        check_release_gate(dataset, _predictions(), _thresholds())

    payload = dataset.model_dump(mode="json")
    payload["rows"][0]["partition"] = "holdout"
    with pytest.raises(ValidationError, match="partition"):
        EvaluationDataset.model_validate(payload)


def test_release_gate_rejects_missing_labels_and_fewer_than_three_batches() -> None:
    payload = _dataset().model_dump(mode="json")
    del payload["rows"][0]["must_include"]
    with pytest.raises(ValidationError):
        EvaluationDataset.model_validate(payload)

    payload = _dataset().model_dump(mode="json")
    payload["rows"] = [
        {
            **row,
            "interval_id": f"{row['interval_id']}-{index}",
            "duplicate_of": f"a-{index}" if row["duplicate"] else None,
        }
        for index in range(15)
        for row in payload["rows"]
    ]
    with pytest.raises(EvaluationError, match="three batches"):
        check_release_gate(
            EvaluationDataset.model_validate(payload), _predictions(), _thresholds()
        )


def test_release_gate_requires_frozen_matching_thresholds_before_holdout() -> None:
    dataset = _dataset()
    payload = dataset.model_dump(mode="json")
    payload["release_eligible"] = True
    payload["rows"] = [
        {
            **row,
            "interval_id": f"{row['interval_id']}-{index}",
            "duplicate_of": None,
            "duplicate": False,
            "batch_id": f"batch-{index // 36}",
            "partition": ("train", "calibration", "holdout")[index // 36],
        }
        for index in range(108)
        for row in [payload["rows"][index % len(payload["rows"])]]
    ]
    eligible = EvaluationDataset.model_validate(payload)
    thresholds = _thresholds()
    with pytest.raises(EvaluationError, match="threshold.*version|version.*threshold"):
        check_release_gate(
            eligible, _predictions(), thresholds.model_copy(update={"model": "other"})
        )
    with pytest.raises(EvaluationError, match="frozen"):
        check_release_gate(
            eligible,
            _predictions(),
            thresholds.model_copy(update={"release_eligible": True, "frozen_at": None}),
        )
    with pytest.raises(EvaluationError, match="before holdout"):
        check_release_gate(
            eligible,
            _predictions(),
            thresholds.model_copy(
                update={
                    "release_eligible": True,
                    "frozen_at": datetime(2026, 10, 3, tzinfo=UTC),
                    "frozen_commit": "a" * 40,
                }
            ),
        )


def test_invalid_prediction_and_hard_gates_fail_closed() -> None:
    dataset = _dataset()
    predictions = _predictions()
    with pytest.raises(EvaluationError, match="unknown interval"):
        evaluate_predictions(
            dataset, predictions.model_copy(update={"selected_ids": ("missing",)})
        )
    for update, reason in (
        ({"spent_usd": Decimal("0.02")}, "budget"),
        ({"source_safe": False}, "source"),
        ({"schema_valid": False}, "schema"),
    ):
        metrics = evaluate_predictions(dataset, predictions.model_copy(update=update))
        assert reason in metrics.hard_gate_failures
    too_long = predictions.model_dump(mode="json")
    too_long["outputs"][1]["duration_seconds"] = "181"
    assert (
        "duration"
        in evaluate_predictions(
            dataset, EvaluationPredictions.model_validate(too_long)
        ).hard_gate_failures
    )


def test_metrics_do_not_mark_missing_denominators_perfect() -> None:
    dataset = _dataset()
    predictions = _predictions().model_copy(
        update={
            "crop_samples": (),
            "human_preference_trials": 0,
            "human_preference_wins": 0,
        }
    )
    metrics = evaluate_predictions(dataset, predictions)

    assert metrics.crop_retention is None
    assert metrics.human_preference is None
    assert "crop_evidence" in metrics.hard_gate_failures
    assert "human_preference_evidence" in metrics.hard_gate_failures


def test_duplicate_interval_id_and_invalid_subject_box_rejected() -> None:
    payload = _dataset().model_dump(mode="json")
    payload["rows"][1]["interval_id"] = payload["rows"][0]["interval_id"]
    with pytest.raises(ValidationError, match="unique"):
        EvaluationDataset.model_validate(payload)
    payload = _dataset().model_dump(mode="json")
    payload["rows"][0]["subject_boxes"] = [
        {"x": "0.9", "y": "0", "w": "0.2", "h": "0.2"}
    ]
    with pytest.raises(ValidationError):
        EvaluationDataset.model_validate(payload)


def test_release_gate_rejects_self_reported_evidence_even_with_sufficient_rows() -> (
    None
):
    payload = _dataset().model_dump(mode="json")
    payload["release_eligible"] = True
    payload["rows"] = [
        {
            **row,
            "interval_id": f"{row['interval_id']}-{index}",
            "duplicate_of": None,
            "duplicate": False,
            "batch_id": f"batch-{index // 36}",
            "partition": ("train", "calibration", "holdout")[index // 36],
        }
        for index in range(108)
        for row in [payload["rows"][index % len(payload["rows"])]]
    ]
    dataset = EvaluationDataset.model_validate(payload)
    thresholds = _thresholds().model_copy(
        update={
            "release_eligible": True,
            "frozen_at": datetime(2026, 9, 27, tzinfo=UTC),
            "frozen_commit": "a" * 40,
            "must_include_recall_min": Decimal(0),
            "weak_rejection_min": Decimal(0),
            "duplicate_rate_max": Decimal(1),
            "crop_retention_min": Decimal(0),
            "abrupt_cut_rate_max": Decimal(1),
            "short_diversity_min": Decimal(0),
            "human_preference_min": Decimal(0),
        }
    )

    predicted = _predictions().model_dump(mode="json")
    predicted["selected_ids"] = ["a-0", "b-1", "d-3", "w4-7"]
    predicted["ordered_ids"] = predicted["selected_ids"]
    predicted["duplicate_pairs"] = [["a-0", "d-3"]]
    predicted["crop_samples"][0]["interval_id"] = "a-0"
    for boundary in predicted["boundaries"]:
        boundary["from_id"] += (
            "-" + {"a": "0", "b": "1", "d": "3", "w4": "7"}[boundary["from_id"]]
        )
        boundary["to_id"] += (
            "-" + {"a": "0", "b": "1", "d": "3", "w4": "7"}[boundary["to_id"]]
        )
    for output in predicted["outputs"]:
        output["selected_ids"] = [
            {"a": "a-0", "b": "b-1", "d": "d-3", "w4": "w4-7"}[item]
            for item in output["selected_ids"]
        ]
    with pytest.raises(EvaluationError, match="verified|provenance|artifact"):
        check_release_gate(
            dataset, EvaluationPredictions.model_validate(predicted), thresholds
        )


def test_unreported_duplicate_and_unrelated_cut_are_not_perfect() -> None:
    predictions = _predictions().model_copy(update={"duplicate_pairs": ()})
    with pytest.raises(EvaluationError, match="duplicate pairs.*labels"):
        evaluate_predictions(_dataset(), predictions)

    payload = _predictions().model_dump(mode="json")
    payload["boundaries"][1]["unrelated"] = False
    with pytest.raises(EvaluationError, match="unrelated cuts.*labels"):
        evaluate_predictions(_dataset(), EvaluationPredictions.model_validate(payload))


def test_published_schema_matches_strict_dataset_model() -> None:
    schema = json.loads((FIXTURES / "schema.json").read_text())
    assert schema == EvaluationDataset.model_json_schema()
    label = schema["$defs"]["EvaluationLabel"]
    assert label["additionalProperties"] is False
    assert label["properties"]["subject_boxes"]["items"]["$ref"] == "#/$defs/SubjectBox"
    assert (
        schema["properties"]["predictions"]["anyOf"][0]["$ref"]
        == "#/$defs/EvaluationPredictions"
    )


def test_synthetic_cli_reports_metrics_but_blocks_release() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "video_editor.evaluation",
            "--labels",
            str(FIXTURES / "synthetic-labels.json"),
            "--thresholds",
            str(FIXTURES / "thresholds-v1.json"),
            "--expect-blocked-release",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["release_status"] == "blocked"
    assert report["metrics_status"] == "unverified_input"
    assert report["metrics"]["must_include_recall"] == str(Decimal(2) / 3)
    assert "100 candidate intervals" in report["block_reason"]
