"""Deterministic bounded sample edit-plan generation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path

from video_editor.media.probe import MediaProbe
from video_editor.media.sequencing import ChronologyGroup, SequencedSource
from video_editor.models.edit_plan import (
    EditPlan,
    Framing,
    OutputSpec,
    PlanSource,
    Provenance,
    TimelineClip,
)

_SAMPLE_SECONDS = Decimal(8)
_PLANNER_VERSION = "phase1-sample-v1"
_FRAME_RATE = Decimal(30)
_CODEC = "libx264"

type ProbeMapping = Mapping[object, MediaProbe]
type PathMapping = Mapping[object, Path]


def _lookup[T](values: Mapping[object, T], path: Path, source_id: str) -> T | None:
    """Resolve mappings accepting path spellings, basename, or source ID."""

    for key in (path, str(path), path.as_posix(), path.name, source_id):
        if key in values:
            return values[key]
    return None


def _duration(probe: MediaProbe) -> Decimal | None:
    if probe.duration is not None:
        value = Decimal(str(probe.duration))
    elif probe.video is not None and probe.video.duration is not None:
        value = Decimal(str(probe.video.duration))
    else:
        return None
    return value if value > 0 else None


def _resolved_path(member: SequencedSource, paths: PathMapping) -> Path:
    path = _lookup(paths, member.source.path, member.source.fingerprint)
    return path if path is not None else member.source.path


def _resolved_probe(member: SequencedSource, probes: ProbeMapping) -> MediaProbe | None:
    return _lookup(probes, member.source.path, member.source.fingerprint)


def _ordered_members(groups: Sequence[ChronologyGroup]) -> list[SequencedSource]:
    return [member for group in groups for member in group.members]


def _make_plan(
    members: Sequence[SequencedSource],
    probes: ProbeMapping,
    paths: PathMapping,
    *,
    width: int,
    height: int,
    framing: Framing,
    sample_seconds: Decimal,
    max_sources: int | None,
) -> EditPlan:
    sources: list[PlanSource] = []
    clips: list[TimelineClip] = []
    timeline_start = Decimal(0)
    any_audio = False

    for member in members:
        if max_sources is not None and len(clips) >= max_sources:
            break
        source_id = member.source.fingerprint
        probe = _resolved_probe(member, probes)
        if probe is None or probe.video is None:
            continue
        duration = _duration(probe)
        if duration is None:
            continue
        path = _resolved_path(member, paths)
        has_audio = probe.audio is not None
        any_audio = any_audio or has_audio
        sources.append(
            PlanSource(
                id=source_id,
                path=path,
                identity=f"{member.source.identity_version}:{source_id}",
                duration=duration,
                has_audio=has_audio,
            )
        )
        end = min(sample_seconds, duration)
        clips.append(
            TimelineClip(
                source_id=source_id,
                source_start=Decimal(0),
                source_end=end,
                timeline_start=timeline_start,
                speed=Decimal(1),
                framing=framing,
                selection_reason="phase1_sample",
                confidence=None,
            )
        )
        timeline_start += end

    if not clips:
        raise ValueError("no usable video sources available for sample plan")

    return EditPlan(
        schema_version=1,
        planner_version=_PLANNER_VERSION,
        sources=sources,
        clips=clips,
        output=OutputSpec(
            kind="short",
            width=width,
            height=height,
            frame_rate=_FRAME_RATE,
            codec=_CODEC,
            audio="source" if any_audio else "silence",
        ),
        provenance=Provenance(planner=_PLANNER_VERSION),
    )


def create_sample_plans(
    ordered_sources: Sequence[ChronologyGroup],
    probes: ProbeMapping,
    paths: PathMapping,
    *,
    sample_seconds: Decimal | float | str = _SAMPLE_SECONDS,
    max_sources: int | None = None,
) -> tuple[EditPlan, EditPlan]:
    """Create horizontal and vertical plans from ordered, inspected sources.

    Each usable source contributes its first ``sample_seconds`` seconds, capped
    by its inspected duration. Invalid or absent material is skipped; planner
    never invents padding. Clean cuts keep timeline intervals contiguous.
    """

    try:
        interval = Decimal(str(sample_seconds))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("sample_seconds must be a finite positive number") from exc
    if not interval.is_finite() or interval <= 0:
        raise ValueError("sample_seconds must be a finite positive number")
    interval = min(interval, _SAMPLE_SECONDS)
    if max_sources is not None and max_sources <= 0:
        raise ValueError("max_sources must be greater than zero")

    members = _ordered_members(ordered_sources)
    horizontal = _make_plan(
        members,
        probes,
        paths,
        width=1920,
        height=1080,
        framing=Framing(mode="fit_background", background="black"),
        sample_seconds=interval,
        max_sources=max_sources,
    )
    vertical = _make_plan(
        members,
        probes,
        paths,
        width=1080,
        height=1920,
        framing=Framing(mode="fit_background", background="black"),
        sample_seconds=interval,
        max_sources=max_sources,
    )
    return horizontal, vertical
