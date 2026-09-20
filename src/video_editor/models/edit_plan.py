"""Versioned edit-plan models and semantic validation."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from video_editor.errors import ErrorCategory, VideoEditorError

_D0 = Decimal(0)
_MAX_LONG = Decimal(3600)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, str) and "e" in value.lower():
        raise ValueError("exponent notation is not supported")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("must be a decimal number") from exc
    if not result.is_finite():
        raise ValueError("must be finite")
    return result


class _PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: Any, handler: Any
    ) -> dict[str, Any]:
        schema = handler(core_schema)

        decimal = r"(?:\d+(?:\.\d*)?|\.\d+)"
        positive = rf"^\+?(?=[0-9.]*[1-9]){decimal}$"
        nonnegative = rf"^\+?{decimal}$"
        field_patterns = {
            "PlanSource": {"duration": positive},
            "TimelineClip": {
                "source_start": nonnegative,
                "source_end": positive,
                "timeline_start": nonnegative,
                "speed": positive,
                "confidence": r"^(?:\+?(?:0+(?:\.\d+)?|\.\d+|1(?:\.0*)?))$",
            },
            "Transition": {"duration": nonnegative},
            "OutputSpec": {"frame_rate": positive},
        }
        patterns = field_patterns.get(cls.__name__, {})

        def tighten(value: Any, field_name: str = "") -> None:
            if isinstance(value, dict):
                if (
                    field_name in patterns
                    and value.get("type") == "string"
                    and "pattern" in value
                ):
                    value["pattern"] = patterns[field_name]
                properties = value.get("properties", {})
                for name, child in properties.items():
                    tighten(child, name)
                for child in value.values():
                    if child is not properties:
                        tighten(child, field_name)
            elif isinstance(value, list):
                for child in value:
                    tighten(child, field_name)

        tighten(schema)
        return cast(dict[str, Any], schema)


class PlanSource(_PlanModel):
    """Source identity and media facts referenced by an edit plan."""

    id: str = Field(min_length=1)
    path: Path
    identity: str = Field(min_length=1)
    duration: Decimal = Field(gt=0)
    has_audio: bool = True

    @field_validator("duration", mode="before")
    @classmethod
    def validate_duration(cls, value: Any) -> Decimal:
        return _decimal(value)


class Framing(_PlanModel):
    """Output framing primitive supported by Phase 1."""

    mode: Literal["center_crop", "fit_background"]
    background: str | None = None

    @model_validator(mode="after")
    def validate_background(self) -> Framing:
        if self.mode == "fit_background" and self.background is None:
            raise ValueError("fit_background requires background")
        if self.mode == "center_crop" and self.background is not None:
            raise ValueError("center_crop does not accept background")
        return self


class TimelineClip(_PlanModel):
    """One source interval placed consecutively on timeline."""

    source_id: str = Field(min_length=1)
    source_start: Decimal = Field(ge=0)
    source_end: Decimal = Field(gt=0)
    timeline_start: Decimal = Field(ge=0)
    speed: Decimal = Field(gt=0)
    framing: Framing
    selection_reason: str = Field(min_length=1)
    confidence: Decimal | None = Field(default=None, ge=0, le=1)

    @field_validator(
        "source_start",
        "source_end",
        "timeline_start",
        "speed",
        "confidence",
        mode="before",
    )
    @classmethod
    def validate_decimal_fields(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        return _decimal(value)

    @model_validator(mode="after")
    def validate_interval(self) -> TimelineClip:
        if self.source_end <= self.source_start:
            raise ValueError("source_end must be greater than source_start")
        return self


class Transition(_PlanModel):
    """Transition between adjacent clips."""

    from_clip: int = Field(ge=0)
    to_clip: int = Field(ge=0)
    kind: Literal["cut", "dissolve"]
    duration: Decimal = Field(ge=0)

    @field_validator("duration", mode="before")
    @classmethod
    def validate_duration(cls, value: Any) -> Decimal:
        return _decimal(value)


class OutputSpec(_PlanModel):
    """Requested output properties."""

    kind: Literal["short", "long"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_rate: Decimal = Field(gt=0)
    codec: Literal["libx264"]
    audio: Literal["source", "silence", "none"] = "source"
    color: str = "passthrough"

    @field_validator("frame_rate", mode="before")
    @classmethod
    def validate_frame_rate(cls, value: Any) -> Decimal:
        return _decimal(value)

    @field_validator("color")
    @classmethod
    def validate_color(cls, value: str) -> str:
        if value != "passthrough" and not value.startswith("override:"):
            raise ValueError("color must be passthrough or explicit override")
        return value


class Provenance(_PlanModel):
    """Data-only provenance metadata; command execution fields are forbidden."""

    planner: str = Field(min_length=1)


class EditPlan(_PlanModel):
    """Version 1 edit plan containing data, never shell commands."""

    schema_version: Literal[1]
    planner_version: str = Field(min_length=1)
    sources: list[PlanSource] = Field(min_length=1)
    clips: list[TimelineClip] = Field(min_length=1)
    transitions: list[Transition] = Field(default_factory=list)
    output: OutputSpec
    provenance: Provenance

    @model_validator(mode="after")
    def validate_semantics(self) -> EditPlan:
        sources = {source.id: source for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("source IDs must be unique")
        for clip in self.clips:
            source = sources.get(clip.source_id)
            if source is None:
                raise ValueError(f"unknown source: {clip.source_id}")
            if clip.source_end > source.duration:
                raise ValueError(f"source interval exceeds duration: {clip.source_id}")

        if self.clips[0].timeline_start != _D0:
            raise ValueError("first clip must start at timeline zero")

        for index, clip in enumerate(self.clips[1:], start=1):
            previous = self.clips[index - 1]
            previous_end = previous.timeline_start + _clip_duration(previous)
            incoming = _transition_for(self.transitions, index - 1, index)
            expected = previous_end - (incoming.duration if incoming else _D0)
            if clip.timeline_start != expected:
                if incoming is None:
                    raise ValueError(
                        "timeline contains gap or overlap without transition"
                    )
                raise ValueError("transition does not create exact clip overlap")

        seen_pairs: set[tuple[int, int]] = set()
        for transition in self.transitions:
            pair = (transition.from_clip, transition.to_clip)
            if pair in seen_pairs:
                raise ValueError("duplicate transition between clips")
            seen_pairs.add(pair)
            if transition.from_clip >= len(self.clips) or transition.to_clip >= len(
                self.clips
            ):
                raise ValueError("transition references missing clip")
            if transition.to_clip != transition.from_clip + 1:
                raise ValueError("transitions must join adjacent clips")
            if transition.kind == "cut" and transition.duration != _D0:
                raise ValueError("cut transition duration must be zero")
            if transition.duration > min(
                _clip_duration(self.clips[transition.from_clip]),
                _clip_duration(self.clips[transition.to_clip]),
            ):
                raise ValueError("transition duration exceeds clip interval")

        duration = timeline_duration(self)
        if duration <= _D0:
            raise ValueError("timeline duration must be positive")

        if self.output.kind == "long" and duration >= _MAX_LONG:
            raise ValueError(
                "long-form output must be strictly shorter than 60 minutes"
            )
        referenced_source_ids = {clip.source_id for clip in self.clips}
        if self.output.audio == "source" and not any(
            source.has_audio
            for source in self.sources
            if source.id in referenced_source_ids
        ):
            raise ValueError("source audio requested but plan has no audio")
        return self


def _source_interval(clip: TimelineClip) -> Decimal:
    return clip.source_end - clip.source_start


def _clip_duration(clip: TimelineClip) -> Decimal:
    return _source_interval(clip) / clip.speed


def _transition_for(
    transitions: list[Transition], from_clip: int, to_clip: int
) -> Transition | None:
    return next(
        (
            item
            for item in transitions
            if item.from_clip == from_clip and item.to_clip == to_clip
        ),
        None,
    )


def timeline_duration(plan: EditPlan) -> Decimal:
    """Compute duration using exact Decimal source arithmetic and overlaps."""

    total = sum((_clip_duration(clip) for clip in plan.clips), _D0)
    return total - sum((transition.duration for transition in plan.transitions), _D0)


def load_plan(path: Path) -> EditPlan:
    """Load and validate JSON plan from disk."""

    try:
        return EditPlan.model_validate_json(path.read_text())
    except OSError as exc:
        raise VideoEditorError(
            ErrorCategory.PLAN, f"cannot load edit plan {path}: {exc}"
        ) from exc
    except (ValidationError, ValueError, TypeError) as exc:
        raise VideoEditorError(
            ErrorCategory.PLAN, f"invalid edit plan {path}: {exc}"
        ) from exc


def write_plan(plan: EditPlan, path: Path) -> None:
    """Write canonical JSON edit plan."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
    except OSError as exc:
        raise VideoEditorError(
            ErrorCategory.PLAN, f"cannot write edit plan {path}: {exc}"
        ) from exc
