"""Offline labeled evaluation; synthetic evidence never opens release gate."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from video_editor.analysis.models import FiniteDecimal, NonEmptyString


class EvaluationError(ValueError):
    """Release evidence is missing, inconsistent, or below its gate."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SubjectBox(_StrictModel):
    x: FiniteDecimal = Field(ge=0, le=1)
    y: FiniteDecimal = Field(ge=0, le=1)
    w: FiniteDecimal = Field(gt=0, le=1)
    h: FiniteDecimal = Field(gt=0, le=1)

    @model_validator(mode="after")
    def within_frame(self) -> Self:
        if self.x + self.w > 1 or self.y + self.h > 1:
            raise ValueError("subject box exceeds frame")
        return self


class EvaluationLabel(_StrictModel):
    interval_id: NonEmptyString
    batch_id: NonEmptyString
    source_id: NonEmptyString
    start: FiniteDecimal = Field(ge=0)
    end: FiniteDecimal = Field(gt=0)
    partition: Literal["train", "calibration", "holdout"]
    must_include: bool
    acceptable: bool
    weak: bool
    duplicate: bool
    duplicate_of: NonEmptyString | None
    chapter: NonEmptyString
    order: int = Field(ge=0)
    subject_boxes: tuple[SubjectBox, ...]
    acceptable_transitions: tuple[Literal["cut", "dissolve", "fade", "fade_black"], ...]

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        if self.end <= self.start:
            raise ValueError("label interval must be positive")
        if self.must_include and (self.weak or not self.acceptable):
            raise ValueError("must-include label must be acceptable and not weak")
        if self.duplicate != (self.duplicate_of is not None):
            raise ValueError("duplicate label requires duplicate_of reference")
        return self


class CropSample(_StrictModel):
    interval_id: NonEmptyString
    retained: int = Field(ge=0)
    sampled: int = Field(gt=0)

    @model_validator(mode="after")
    def valid_counts(self) -> Self:
        if self.retained > self.sampled:
            raise ValueError("crop retention exceeds sampled frames")
        return self


class BoundaryPrediction(_StrictModel):
    from_id: NonEmptyString
    to_id: NonEmptyString
    kind: Literal["cut", "dissolve", "fade", "fade_black"]
    relation: NonEmptyString
    unrelated: bool


class OutputPrediction(_StrictModel):
    kind: Literal["long", "short"]
    duration_seconds: FiniteDecimal = Field(gt=0)
    selected_ids: tuple[NonEmptyString, ...]


class EvaluationPredictions(_StrictModel):
    selected_ids: tuple[NonEmptyString, ...]
    duplicate_pairs: tuple[tuple[NonEmptyString, NonEmptyString], ...]
    ordered_ids: tuple[NonEmptyString, ...]
    crop_samples: tuple[CropSample, ...]
    boundaries: tuple[BoundaryPrediction, ...]
    outputs: tuple[OutputPrediction, ...]
    human_preference_wins: int = Field(ge=0)
    human_preference_trials: int = Field(ge=0)
    source_hours: FiniteDecimal = Field(gt=0)
    spent_usd: FiniteDecimal = Field(ge=0)
    source_safe: bool
    schema_valid: bool

    @model_validator(mode="after")
    def valid_predictions(self) -> Self:
        if len(set(self.selected_ids)) != len(self.selected_ids):
            raise ValueError("selected interval IDs must be unique")
        if self.human_preference_wins > self.human_preference_trials:
            raise ValueError("human preference wins exceed trials")
        if any(len(pair) != 2 or pair[0] == pair[1] for pair in self.duplicate_pairs):
            raise ValueError("duplicate pairs must contain two distinct IDs")
        return self


class EvaluationDataset(_StrictModel):
    schema_version: Literal["evaluation-v1"]
    dataset_version: NonEmptyString
    model: NonEmptyString
    prompt_version: NonEmptyString
    config_version: NonEmptyString
    release_eligible: bool
    holdout_started_at: datetime | None
    rows: tuple[EvaluationLabel, ...]
    predictions: EvaluationPredictions | None = None

    @model_validator(mode="after")
    def isolated_partitions(self) -> Self:
        ids = [row.interval_id for row in self.rows]
        if len(ids) != len(set(ids)):
            raise ValueError("interval IDs must be unique")
        batch_partitions: dict[str, str] = {}
        by_id = {row.interval_id: row for row in self.rows}
        for row in self.rows:
            previous = batch_partitions.setdefault(row.batch_id, row.partition)
            if previous != row.partition:
                raise ValueError("batch partition must be isolated")
            if row.duplicate_of is not None:
                target = by_id.get(row.duplicate_of)
                if (
                    target is None
                    or target.batch_id != row.batch_id
                    or target.interval_id == row.interval_id
                ):
                    raise ValueError(
                        "duplicate_of must reference another labeled interval in same batch"
                    )
        return self


class EvaluationThresholds(_StrictModel):
    schema_version: Literal["thresholds-v1"]
    model: NonEmptyString
    prompt_version: NonEmptyString
    config_version: NonEmptyString
    dataset_version: NonEmptyString
    must_include_recall_min: FiniteDecimal = Field(ge=0, le=1)
    weak_rejection_min: FiniteDecimal = Field(ge=0, le=1)
    duplicate_rate_max: FiniteDecimal = Field(ge=0, le=1)
    crop_retention_min: FiniteDecimal = Field(ge=0, le=1)
    abrupt_cut_rate_max: FiniteDecimal = Field(ge=0, le=1)
    short_diversity_min: FiniteDecimal = Field(ge=0, le=1)
    human_preference_min: FiniteDecimal = Field(ge=0, le=1)
    calibration_evidence_digest: NonEmptyString
    frozen_at: datetime | None
    frozen_commit: NonEmptyString | None
    release_eligible: bool


class EvaluationMetrics(_StrictModel):
    must_include_recall: FiniteDecimal | None
    weak_rejection: FiniteDecimal | None
    duplicate_rate: FiniteDecimal | None
    abrupt_cut_rate: FiniteDecimal | None
    crop_retention: FiniteDecimal | None
    short_diversity: FiniteDecimal | None
    human_preference: FiniteDecimal | None
    chronology_correct: bool
    duration_compliant: bool
    budget_compliant: bool
    hard_gate_failures: tuple[str, ...]


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    return Decimal(numerator) / Decimal(denominator) if denominator else None


def evaluate_predictions(
    dataset: EvaluationDataset, predictions: EvaluationPredictions
) -> EvaluationMetrics:
    """Compute exploratory formulas from supplied, unverified prediction rows."""
    labels = {row.interval_id: row for row in dataset.rows}
    selected = set(predictions.selected_ids)
    unknown = selected - labels.keys()
    if unknown:
        raise EvaluationError("unknown interval in selection")
    if set(predictions.ordered_ids) != selected or len(predictions.ordered_ids) != len(
        selected
    ):
        raise EvaluationError("ordered intervals must equal selected intervals")
    if any(set(pair) - selected for pair in predictions.duplicate_pairs):
        raise EvaluationError("duplicate pair references unselected interval")
    if any(sample.interval_id not in selected for sample in predictions.crop_samples):
        raise EvaluationError("crop sample references unselected interval")
    if any(
        boundary.from_id not in selected or boundary.to_id not in selected
        for boundary in predictions.boundaries
    ):
        raise EvaluationError("boundary references unselected interval")
    if any(set(output.selected_ids) - selected for output in predictions.outputs):
        raise EvaluationError("output references unselected interval")

    must = [row for row in dataset.rows if row.must_include]
    weak = [row for row in dataset.rows if row.weak]
    selected_pairs = len(selected) * (len(selected) - 1) // 2
    pair_ids = {tuple(sorted(pair)) for pair in predictions.duplicate_pairs}
    if len(pair_ids) != len(predictions.duplicate_pairs):
        raise EvaluationError("duplicate pairs must be unique")
    labeled_pairs = {
        tuple(sorted((row.interval_id, row.duplicate_of)))
        for row in dataset.rows
        if row.duplicate_of is not None
        and row.interval_id in selected
        and row.duplicate_of in selected
    }
    if pair_ids != labeled_pairs:
        raise EvaluationError("reported duplicate pairs do not match labels")
    for boundary in predictions.boundaries:
        acceptable = labels[boundary.to_id].acceptable_transitions
        if boundary.kind == "cut" and (boundary.unrelated != ("cut" not in acceptable)):
            raise EvaluationError("reported unrelated cuts do not match labels")
    order_keys = [
        (labels[item].order, labels[item].start) for item in predictions.ordered_ids
    ]
    retained = sum(sample.retained for sample in predictions.crop_samples)
    sampled = sum(sample.sampled for sample in predictions.crop_samples)
    short_sets = [
        set(output.selected_ids)
        for output in predictions.outputs
        if output.kind == "short"
    ]
    short_pairs = list(combinations(short_sets, 2))
    diverse = sum(not left.intersection(right) for left, right in short_pairs)
    duration_ok = all(
        output.duration_seconds
        <= (Decimal(1800) if output.kind == "long" else Decimal(180))
        for output in predictions.outputs
    ) and bool(predictions.outputs)
    # Release criterion is ACTUAL spend, kept at USD 1.00 per source hour; the
    # runtime reservation ceiling in config is higher by design.
    budget_ok = predictions.spent_usd <= predictions.source_hours * Decimal("1.00")
    failures = []
    if not duration_ok:
        failures.append("duration")
    if not budget_ok:
        failures.append("budget")
    if not predictions.source_safe:
        failures.append("source")
    if not predictions.schema_valid:
        failures.append("schema")
    if sampled == 0:
        failures.append("crop_evidence")
    if predictions.human_preference_trials == 0:
        failures.append("human_preference_evidence")
    if (
        not must
        or not weak
        or not selected_pairs
        or not predictions.boundaries
        or not short_pairs
    ):
        failures.append("metric_denominator")
    return EvaluationMetrics(
        must_include_recall=_ratio(
            sum(row.interval_id in selected for row in must), len(must)
        ),
        weak_rejection=_ratio(
            sum(row.interval_id not in selected for row in weak), len(weak)
        ),
        duplicate_rate=_ratio(len(pair_ids), selected_pairs),
        abrupt_cut_rate=_ratio(
            sum(
                boundary.kind == "cut" and boundary.unrelated
                for boundary in predictions.boundaries
            ),
            len(predictions.boundaries),
        ),
        crop_retention=_ratio(retained, sampled),
        short_diversity=_ratio(diverse, len(short_pairs)),
        human_preference=_ratio(
            predictions.human_preference_wins, predictions.human_preference_trials
        ),
        chronology_correct=order_keys == sorted(order_keys),
        duration_compliant=duration_ok,
        budget_compliant=budget_ok,
        hard_gate_failures=tuple(failures),
    )


def check_release_gate(
    dataset: EvaluationDataset,
    predictions: EvaluationPredictions,
    thresholds: EvaluationThresholds,
) -> EvaluationMetrics:
    """Block release until independent holdout and artifact attestation exists."""
    del predictions
    if len(dataset.rows) < 100:
        raise EvaluationError("release requires at least 100 candidate intervals")
    if len({row.batch_id for row in dataset.rows}) < 3:
        raise EvaluationError("release requires at least three batches")
    counts = Counter(row.partition for row in dataset.rows)
    if any(counts[partition] == 0 for partition in ("train", "calibration", "holdout")):
        raise EvaluationError(
            "release requires train, calibration, and holdout partitions"
        )
    versions = ("model", "prompt_version", "config_version", "dataset_version")
    if any(getattr(dataset, field) != getattr(thresholds, field) for field in versions):
        raise EvaluationError("threshold version mismatch with evaluation dataset")
    if not thresholds.release_eligible or not dataset.release_eligible:
        raise EvaluationError("synthetic or ineligible evidence cannot release")
    if thresholds.frozen_at is None or thresholds.frozen_commit is None:
        raise EvaluationError("thresholds must be frozen and committed")
    if (
        dataset.holdout_started_at is None
        or thresholds.frozen_at >= dataset.holdout_started_at
    ):
        raise EvaluationError("thresholds must be frozen before holdout")
    # This harness computes exploratory metrics only. It cannot attest the
    # billing ledger, original-source safety, committed thresholds, or holdout
    # provenance; no self-reported JSON value may open the release gate.
    raise EvaluationError("verified artifact and threshold provenance required")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline Phase 2 evaluation")
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--thresholds", type=Path, required=True)
    parser.add_argument("--expect-blocked-release", action="store_true")
    args = parser.parse_args(argv)
    try:
        dataset = EvaluationDataset.model_validate_json(args.labels.read_text())
        thresholds = EvaluationThresholds.model_validate_json(
            args.thresholds.read_text()
        )
        if dataset.predictions is None:
            raise EvaluationError("predictions are required")
        metrics = evaluate_predictions(dataset, dataset.predictions)
        try:
            check_release_gate(dataset, dataset.predictions, thresholds)
            status, reason = "passed", None
        except EvaluationError as exc:
            status, reason = "blocked", str(exc)
        print(
            json.dumps(
                {
                    "release_status": status,
                    "block_reason": reason,
                    "metrics_status": "unverified_input",
                    "metrics": metrics.model_dump(mode="json"),
                },
                sort_keys=True,
            )
        )
        return 0 if (status == "blocked") == args.expect_blocked_release else 1
    except (ValueError, OSError) as exc:
        print(
            json.dumps(
                {
                    "release_status": "blocked",
                    "block_reason": str(exc),
                    "metrics": None,
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
