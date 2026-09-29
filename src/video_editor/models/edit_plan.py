"""Versioned edit-plan models and semantic validation."""

from __future__ import annotations

import json
from collections.abc import Sequence
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
_MAX_LONG_V2 = Decimal(1800)
_MAX_SHORT_V2 = Decimal(180)
_MAX_CROSS_SHORT_OVERLAP = Decimal("0.10")
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "cut": {"continuous_action", "matched_motion", "same_event"},
    "dissolve": {"same_event", "same_place_time_shift"},
    "fade": {"chapter_boundary", "story_open_close"},
    "fade_black": {"chapter_boundary", "time_jump", "location_change"},
}


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
            "CropKeyframe": {
                "time": nonnegative,
                "center_x": r"^(?:\+?(?:0+(?:\.\d+)?|\.\d+|1(?:\.0*)?))$",
                "center_y": r"^(?:\+?(?:0+(?:\.\d+)?|\.\d+|1(?:\.0*)?))$",
            },
            "ScoreBreakdown": {
                "positive": nonnegative,
                "penalties": nonnegative,
            },
            "TimelineClipV2": {
                "source_start": nonnegative,
                "source_end": positive,
                "timeline_start": nonnegative,
                "speed": positive,
                "overall_score": nonnegative,
                "vertical_suitability": (r"^(?:\+?(?:0+(?:\.\d+)?|\.\d+|1(?:\.0*)?))$"),
                "confidence": (r"^(?:\+?(?:0+(?:\.\d+)?|\.\d+|1(?:\.0*)?))$"),
            },
            "TransitionV2": {
                "duration": nonnegative,
                "confidence": (r"^(?:\+?(?:0+(?:\.\d+)?|\.\d+|1(?:\.0*)?))$"),
            },
            "OutputSpecV2": {"frame_rate": positive},
            "AnchorMoment": {
                "source_start": nonnegative,
                "source_end": positive,
            },
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
        if cls.__name__ == "FramingV2":
            schema["allOf"] = [
                {
                    "if": {"properties": {"mode": {"const": "tracked_crop"}}},
                    "then": {
                        "properties": {"keyframes": {"minItems": 1}},
                        "required": [
                            "track_id",
                            "track_clip_id",
                            "track_source_identity",
                        ],
                    },
                }
            ]
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


class CropKeyframe(_PlanModel):
    """One exact clip-local crop-center sample."""

    time: Decimal = Field(ge=0)
    center_x: Decimal = Field(ge=0, le=1)
    center_y: Decimal = Field(ge=0, le=1)
    subject_box_id: str | None = None
    fallback: Literal["tracked", "hold", "ease_center", "static"]

    @field_validator("time", "center_x", "center_y", mode="before")
    @classmethod
    def validate_decimal_fields(cls, value: Any) -> Decimal:
        """Parse crop values with exact finite decimal rules."""
        return _decimal(value)


class FramingV2(_PlanModel):
    """Version 2 output framing, including identity-bound tracked crops."""

    mode: Literal["center_crop", "fit_background", "tracked_crop"]
    background: str | None = None
    track_id: str | None = None
    track_clip_id: str | None = None
    track_source_identity: str | None = None
    keyframes: list[CropKeyframe] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_mode_fields(self) -> FramingV2:
        """Require only fields used by selected framing mode."""
        track_identity = (
            self.track_id,
            self.track_clip_id,
            self.track_source_identity,
        )
        if self.mode == "fit_background":
            if self.background is None:
                raise ValueError("fit_background requires background")
            if any(value is not None for value in track_identity) or self.keyframes:
                raise ValueError("fit_background does not accept tracked crop data")
        elif self.mode == "center_crop":
            if self.background is not None:
                raise ValueError("center_crop does not accept background")
            if any(value is not None for value in track_identity) or self.keyframes:
                raise ValueError("center_crop does not accept tracked crop data")
        else:
            if self.background is not None:
                raise ValueError("tracked_crop does not accept background")
            if (
                not self.track_id
                or not self.track_clip_id
                or not self.track_source_identity
            ):
                raise ValueError("tracked_crop requires track and path identity")
            if not self.keyframes:
                raise ValueError("tracked_crop requires keyframes")
            if any(
                current.time <= previous.time
                for previous, current in zip(self.keyframes, self.keyframes[1:])
            ):
                raise ValueError(
                    "tracked crop keyframe times must be strictly increasing"
                )
        return self


class PositiveScores(_PlanModel):
    """Positive highlight dimensions retained for auditability."""

    action: Decimal = Field(ge=0)
    scenic: Decimal = Field(ge=0)
    human: Decimal = Field(ge=0)
    story: Decimal = Field(ge=0)
    technical: Decimal = Field(ge=0)
    novelty: Decimal = Field(ge=0)
    completeness: Decimal = Field(ge=0)
    long_story: Decimal = Field(ge=0)
    short: Decimal = Field(ge=0)
    vertical: Decimal = Field(ge=0)

    @field_validator("*", mode="before")
    @classmethod
    def validate_scores(cls, value: Any) -> Decimal:
        """Parse every positive score as an exact finite decimal."""
        return _decimal(value)


class PenaltyScores(_PlanModel):
    """Highlight penalties retained separately from positive scores."""

    blur_exposure: Decimal = Field(ge=0)
    shake_obstruction: Decimal = Field(ge=0)
    incomplete: Decimal = Field(ge=0)
    weak_boundary: Decimal = Field(ge=0)
    repetition: Decimal = Field(ge=0)
    overlap: Decimal = Field(ge=0)

    @field_validator("*", mode="before")
    @classmethod
    def validate_scores(cls, value: Any) -> Decimal:
        """Parse every penalty score as an exact finite decimal."""
        return _decimal(value)


class ScoreBreakdown(_PlanModel):
    """Typed positive and penalty score dimensions."""

    positive: PositiveScores
    penalties: PenaltyScores


class TimelineClipV2(_PlanModel):
    """Version 2 source interval with score, evidence, and identity metadata."""

    clip_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_identity: str = Field(min_length=1)
    source_start: Decimal = Field(ge=0)
    source_end: Decimal = Field(gt=0)
    timeline_start: Decimal = Field(ge=0)
    speed: Decimal = Field(gt=0)
    framing: FramingV2
    selection_reason: str = Field(min_length=1)
    overall_score: Decimal = Field(ge=0)
    score_breakdown: ScoreBreakdown
    analysis_reference: str = Field(min_length=1)
    source_proxy_mapping_reference: str = Field(min_length=1)
    dedup_group: str = Field(min_length=1)
    chapter_id: str | None = Field(default=None, min_length=1)
    event_id: str | None = Field(default=None, min_length=1)
    vertical_suitability: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    planner_version: str = Field(min_length=1)
    analysis_version: str = Field(min_length=1)

    @field_validator(
        "source_start",
        "source_end",
        "timeline_start",
        "speed",
        "overall_score",
        "vertical_suitability",
        "confidence",
        mode="before",
    )
    @classmethod
    def validate_decimal_fields(cls, value: Any) -> Decimal:
        """Parse clip values with exact finite decimal rules."""
        return _decimal(value)

    @model_validator(mode="after")
    def validate_interval_and_track(self) -> TimelineClipV2:
        """Validate interval ordering and tracked-crop ownership."""
        if self.source_end <= self.source_start:
            raise ValueError("source_end must be greater than source_start")
        if self.framing.mode == "tracked_crop":
            if self.framing.track_clip_id != self.clip_id:
                raise ValueError("tracked crop clip identity does not match clip")
            if self.framing.track_source_identity != self.source_identity:
                raise ValueError("tracked crop source identity does not match clip")
            clip_duration = _clip_duration(self)
            if self.framing.keyframes[-1].time > clip_duration:
                raise ValueError("tracked crop keyframe exceeds clip duration")
        return self


class TransitionV2(_PlanModel):
    """Semantic transition between adjacent version 2 clips."""

    from_clip: int = Field(ge=0)
    to_clip: int = Field(ge=0)
    kind: Literal["cut", "dissolve", "fade", "fade_black"]
    duration: Decimal = Field(ge=0)
    relation: Literal[
        "continuous_action",
        "matched_motion",
        "same_event",
        "same_place_time_shift",
        "chapter_boundary",
        "time_jump",
        "location_change",
        "story_open_close",
    ]
    reason: str = Field(min_length=1)
    confidence: Decimal = Field(ge=0, le=1)
    audio_policy: Literal["cut", "crossfade", "fade_out_in"]

    @field_validator("duration", "confidence", mode="before")
    @classmethod
    def validate_decimal_fields(cls, value: Any) -> Decimal:
        """Parse transition values with exact finite decimal rules."""
        return _decimal(value)

    @model_validator(mode="after")
    def validate_relation(self) -> TransitionV2:
        """Reject transition types unsupported by semantic relation."""
        if self.relation not in _ALLOWED_TRANSITIONS[self.kind]:
            raise ValueError(
                f"transition {self.kind} is not allowed for relation {self.relation}"
            )
        return self


class OutputSpecV2(_PlanModel):
    """Collision-proof version 2 output identity and media properties."""

    plan_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    kind: Literal["short", "long"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_rate: Decimal = Field(gt=0)
    codec: Literal["libx264"]
    audio: Literal["source", "silence", "none"] = "source"
    theme_summary: str | None = Field(default=None, min_length=1)

    @field_validator("frame_rate", mode="before")
    @classmethod
    def validate_frame_rate(cls, value: Any) -> Decimal:
        """Parse frame rate with exact finite decimal rules."""
        return _decimal(value)

    @model_validator(mode="after")
    def validate_identity_and_dimensions(self) -> OutputSpecV2:
        """Bind each output kind to required dimensions and filename."""
        if self.kind == "long":
            if (self.width, self.height) != (1920, 1080):
                raise ValueError("long output dimensions must be 1920x1080")
            if self.filename != "long.mp4":
                raise ValueError("long output filename must be long.mp4")
        else:
            if (self.width, self.height) != (1080, 1920):
                raise ValueError("short output dimensions must be 1080x1920")
            prefix, separator, suffix = self.filename.partition("-")
            number, dot, extension = suffix.partition(".")
            if (
                prefix != "short"
                or separator != "-"
                or len(number) != 2
                or not number.isdigit()
                or number == "00"
                or dot != "."
                or extension != "mp4"
            ):
                raise ValueError("short output filename must be short-NN.mp4")
        return self


class EditPlanV2(_PlanModel):
    """Version 2 edit plan with evidence and explicit output identity."""

    schema_version: Literal[2]
    planner_version: str = Field(min_length=1)
    analysis_version: str = Field(min_length=1)
    sources: list[PlanSource] = Field(min_length=1)
    clips: list[TimelineClipV2] = Field(min_length=1)
    transitions: list[TransitionV2] = Field(default_factory=list)
    output: OutputSpecV2
    provenance: Provenance

    @model_validator(mode="after")
    def validate_semantics(self) -> EditPlanV2:
        """Validate source, timeline, transition, duration, and audio semantics."""
        sources = {source.id: source for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("source IDs must be unique")
        clip_ids = {clip.clip_id for clip in self.clips}
        if len(clip_ids) != len(self.clips):
            raise ValueError("clip IDs must be unique")
        for clip in self.clips:
            source = sources.get(clip.source_id)
            if source is None:
                raise ValueError(f"unknown source: {clip.source_id}")
            if clip.source_identity != source.identity:
                raise ValueError(f"source identity does not match: {clip.source_id}")
            if clip.source_end > source.duration:
                raise ValueError(f"source interval exceeds duration: {clip.source_id}")
            if clip.planner_version != self.planner_version:
                raise ValueError("clip planner version does not match plan")
            if clip.analysis_version != self.analysis_version:
                raise ValueError("clip analysis version does not match plan")

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
        if self.output.kind == "long" and duration > _MAX_LONG_V2:
            raise ValueError("long-form output may not exceed 1,800 seconds")
        if self.output.kind == "short" and duration > _MAX_SHORT_V2:
            raise ValueError("short output may not exceed 180 seconds")

        referenced_source_ids = {clip.source_id for clip in self.clips}
        if self.output.audio == "source" and not any(
            source.has_audio
            for source in self.sources
            if source.id in referenced_source_ids
        ):
            raise ValueError("source audio requested but plan has no audio")
        return self


class AnchorMoment(_PlanModel):
    """Recorded exception allowing one moment in at most two shorts."""

    source_identity: str = Field(min_length=1)
    source_start: Decimal = Field(ge=0)
    source_end: Decimal = Field(gt=0)
    dedup_group: str = Field(min_length=1)
    rationale: str = Field(min_length=1)

    @field_validator("source_start", "source_end", mode="before")
    @classmethod
    def validate_decimal_fields(cls, value: Any) -> Decimal:
        """Parse anchor interval with exact finite decimal rules."""
        return _decimal(value)

    @model_validator(mode="after")
    def validate_interval(self) -> AnchorMoment:
        """Reject empty or reversed anchor intervals."""
        if self.source_end <= self.source_start:
            raise ValueError("anchor source_end must be greater than source_start")
        return self


class PlanSetPolicy(_PlanModel):
    """Cross-output limits for one edit plan set."""

    max_shorts: int = Field(default=5, ge=0)
    anchor: AnchorMoment | None = None


EditPlanDocument = EditPlan | EditPlanV2


def _source_interval(clip: TimelineClip | TimelineClipV2) -> Decimal:
    return clip.source_end - clip.source_start


def _clip_duration(clip: TimelineClip | TimelineClipV2) -> Decimal:
    return _source_interval(clip) / clip.speed


def _transition_for(
    transitions: list[Transition] | list[TransitionV2],
    from_clip: int,
    to_clip: int,
) -> Transition | TransitionV2 | None:
    return next(
        (
            item
            for item in transitions
            if item.from_clip == from_clip and item.to_clip == to_clip
        ),
        None,
    )


def timeline_duration(plan: EditPlanDocument) -> Decimal:
    """Compute duration using exact Decimal source arithmetic and overlaps."""

    total = sum((_clip_duration(clip) for clip in plan.clips), _D0)
    return total - sum((transition.duration for transition in plan.transitions), _D0)


def _clip_source_identity(plan: EditPlanV2, clip: TimelineClipV2) -> str:
    return next(
        source.identity for source in plan.sources if source.id == clip.source_id
    )


def _temporal_overlap(first: TimelineClipV2, second: TimelineClipV2) -> Decimal:
    return max(
        _D0,
        min(first.source_end, second.source_end)
        - max(first.source_start, second.source_start),
    )


def _matches_anchor(clip: TimelineClipV2, anchor: AnchorMoment) -> bool:
    return (
        clip.source_identity == anchor.source_identity
        and clip.source_start == anchor.source_start
        and clip.source_end == anchor.source_end
        and clip.dedup_group == anchor.dedup_group
    )


def validate_plan_set(plans: Sequence[EditPlanV2], policy: PlanSetPolicy) -> None:
    """Validate unique output identity and duplicate policy across version 2 plans."""
    long_count = sum(plan.output.kind == "long" for plan in plans)
    if long_count > 1:
        raise ValueError("plan set allows at most one long output")
    plan_ids = [plan.output.plan_id for plan in plans]
    if len(set(plan_ids)) != len(plan_ids):
        raise ValueError("plan IDs must be unique")
    filenames = [plan.output.filename for plan in plans]
    if len(set(filenames)) != len(filenames):
        raise ValueError("output filenames must be unique")

    shorts = [plan for plan in plans if plan.output.kind == "short"]
    if len(shorts) > policy.max_shorts:
        raise ValueError("plan set exceeds short count limit")

    anchor_plan_ids: set[str] = set()
    for index, first in enumerate(shorts):
        for second in shorts[index + 1 :]:
            for first_clip in first.clips:
                for second_clip in second.clips:
                    semantic_forbidden = (
                        first_clip.dedup_group == second_clip.dedup_group
                    )
                    same_source = _clip_source_identity(
                        first, first_clip
                    ) == _clip_source_identity(second, second_clip)
                    overlap = (
                        _temporal_overlap(first_clip, second_clip)
                        if same_source
                        else _D0
                    )
                    shorter = min(
                        _source_interval(first_clip), _source_interval(second_clip)
                    )
                    overlap_forbidden = overlap / shorter > _MAX_CROSS_SHORT_OVERLAP
                    if not overlap_forbidden and not semantic_forbidden:
                        continue
                    if (
                        policy.anchor is not None
                        and _matches_anchor(first_clip, policy.anchor)
                        and _matches_anchor(second_clip, policy.anchor)
                    ):
                        anchor_plan_ids.update(
                            {first.output.plan_id, second.output.plan_id}
                        )
                        continue
                    if overlap_forbidden:
                        raise ValueError(
                            "short plans exceed cross-short temporal overlap limit"
                        )
                    raise ValueError("short plans reuse a semantic group")

    if len(anchor_plan_ids) > 2:
        raise ValueError("anchor moment may appear in at most two shorts")


def load_plan(path: Path) -> EditPlanDocument:
    """Load and validate version 1 or version 2 JSON plan from disk."""

    try:
        text = path.read_text()
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise TypeError("edit plan must be a JSON object")
        schema_version = payload.get("schema_version")
        if schema_version == 1:
            return EditPlan.model_validate_json(text)
        if schema_version == 2:
            return EditPlanV2.model_validate_json(text)
        raise ValueError(f"unsupported schema_version: {schema_version!r}")
    except OSError as exc:
        raise VideoEditorError(
            ErrorCategory.PLAN, f"cannot load edit plan {path}: {exc}"
        ) from exc
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
        raise VideoEditorError(
            ErrorCategory.PLAN, f"invalid edit plan {path}: {exc}"
        ) from exc


def write_plan(plan: EditPlanDocument, path: Path) -> None:
    """Write canonical version 1 or version 2 JSON edit plan."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
    except OSError as exc:
        raise VideoEditorError(
            ErrorCategory.PLAN, f"cannot write edit plan {path}: {exc}"
        ) from exc
