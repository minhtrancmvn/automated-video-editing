"""Conservative GoPro filename parsing and source chronology ordering."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise

from video_editor.media.discovery import SourceCandidate


@dataclass(frozen=True)
class ParsedSequence:
    """Numeric sequence evidence extracted from one GoPro filename."""

    pattern: str
    file_number: int
    chapter: int
    session: str | None = None


@dataclass(frozen=True)
class SequencedSource:
    """A source together with parsing and chronology evidence."""

    source: SourceCandidate
    parsed: ParsedSequence | None
    creation_time: datetime | None
    order_evidence: str
    confidence: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ChronologyGroup:
    """Sources that belong to one recording sequence."""

    group_id: str
    members: tuple[SequencedSource, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)


# Keep these expressions intentionally narrow. In particular, do not use ``\\d``
# or ``\\w``: filename text is untrusted and GoPro's observed format is ASCII.
_CLASSIC_NAME = re.compile(
    r"(?P<pattern>GOPR)(?P<file>[0-9]{4})[.](?P<extension>MP4)",
    re.IGNORECASE | re.ASCII,
)
_CHAPTER_NAME = re.compile(
    r"(?P<pattern>G[A-Z])(?P<chapter>[0-9]{2})(?P<file>[0-9]{4})"
    r"[.](?P<extension>MP4)",
    re.IGNORECASE | re.ASCII,
)


def parse_gopro_name(name: str) -> ParsedSequence | None:
    """Parse one complete, observed GoPro MP4 filename.

    Classic ``GOPR####`` files are first chapters. Chapter files use
    ``G[A-Z]CCFFFF`` where ``CC`` is chapter and ``FFFF`` is file number.
    Prefix letters are retained as evidence only; they do not affect ordering.
    """

    match = _CLASSIC_NAME.fullmatch(name)
    if match is not None:
        return ParsedSequence(
            pattern=match.group("pattern"),
            file_number=int(match.group("file")),
            chapter=1,
        )

    match = _CHAPTER_NAME.fullmatch(name)
    if match is None:
        return None
    return ParsedSequence(
        pattern=match.group("pattern"),
        file_number=int(match.group("file")),
        chapter=int(match.group("chapter")),
    )


def _creation_time(
    source: SourceCandidate, creation_times: Mapping[str, datetime | None]
) -> datetime | None:
    """Read metadata by stable path spellings used by callers."""

    path = source.path
    for key in (str(path), path.as_posix(), path.name):
        if key in creation_times:
            return creation_times[key]
    return None


def _time_key(value: datetime) -> datetime:
    """Make naive and aware metadata comparable without changing returned data."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _first_index(group: ChronologyGroup) -> int:
    return min(member.source.discovery_index for member in group.members)


def _group_time(group: ChronologyGroup) -> datetime | None:
    times = [member.creation_time for member in group.members]
    present = [value for value in times if value is not None]
    if not present:
        return None
    return min(present, key=_time_key)


def _group_file_number(group: ChronologyGroup) -> int | None:
    parsed = [member.parsed for member in group.members]
    numbers = [item.file_number for item in parsed if item is not None]
    return min(numbers) if numbers else None


def _with_source_warning(
    source: SequencedSource, warning: str
) -> SequencedSource:
    if warning in source.warnings:
        return source
    return SequencedSource(
        source=source.source,
        parsed=source.parsed,
        creation_time=source.creation_time,
        order_evidence=source.order_evidence,
        confidence=source.confidence,
        warnings=(*source.warnings, warning),
    )


def _add_group_warning(
    group: ChronologyGroup, warning: str
) -> ChronologyGroup:
    members = tuple(_with_source_warning(member, warning) for member in group.members)
    warnings = group.warnings if warning in group.warnings else (*group.warnings, warning)
    return ChronologyGroup(group.group_id, members, warnings)


def _metadata_conflict(groups: Sequence[ChronologyGroup]) -> bool:
    known = [(group, _group_time(group), _group_file_number(group)) for group in groups]
    known = [item for item in known if item[1] is not None and item[2] is not None]
    for left_index, (_, left_time, left_number) in enumerate(known):
        assert left_time is not None and left_number is not None
        for _, right_time, right_number in known[left_index + 1 :]:
            assert right_time is not None and right_number is not None
            time_order = _time_key(left_time) < _time_key(right_time)
            number_order = left_number < right_number
            if time_order != number_order and left_time != right_time:
                return True
    return False


def _sort_groups(groups: Sequence[ChronologyGroup]) -> list[ChronologyGroup]:
    """Apply metadata-first ordering, then numeric continuity, then discovery."""

    return sorted(
        groups,
        key=lambda group: (
            _group_time(group) is None,
            _time_key(group_time)
            if (group_time := _group_time(group)) is not None
            else datetime.max.replace(tzinfo=UTC),
            _group_file_number(group) is None,
            _group_file_number(group) if _group_file_number(group) is not None else 0,
            _first_index(group),
        ),
    )


def sequence_sources(
    sources: Sequence[SourceCandidate],
    creation_times: Mapping[str, datetime | None],
) -> list[ChronologyGroup]:
    """Group and order candidates using conservative chronology evidence.

    Creation metadata wins when available. Groups without metadata use numeric
    file continuity for GoPro names and discovery order for other files.
    """

    parsed_sources: list[SequencedSource] = []
    for source in sources:
        parsed = parse_gopro_name(source.path.name)
        creation_time = _creation_time(source, creation_times)
        if parsed is not None and creation_time is not None:
            evidence, confidence = "creation_time", "high"
        elif parsed is not None:
            evidence, confidence = "numeric_continuity", "medium"
        elif creation_time is not None:
            evidence, confidence = "creation_time", "medium"
        else:
            evidence, confidence = "discovery_order", "low"
        parsed_sources.append(
            SequencedSource(
                source=source,
                parsed=parsed,
                creation_time=creation_time,
                order_evidence=evidence,
                confidence=confidence,
            )
        )

    grouped: dict[str, list[SequencedSource]] = {}
    sessions: dict[int, list[str]] = {}
    for item in parsed_sources:
        if item.parsed is None:
            group_id = f"discovery-{item.source.discovery_index}"
        else:
            number = item.parsed.file_number
            number_sessions = sessions.setdefault(number, [str(number)])
            if item.parsed.chapter == 1 and any(
                member.parsed is not None and member.parsed.chapter == 1
                for member in grouped.get(number_sessions[-1], [])
            ):
                group_id = f"{number}-session-{len(number_sessions)}"
                number_sessions.append(group_id)
            elif item.parsed.chapter == 1:
                group_id = number_sessions[-1]
            else:
                compatible = [
                    candidate
                    for candidate in reversed(number_sessions)
                    if not any(
                        member.parsed is not None
                        and member.parsed.chapter == item.parsed.chapter
                        for member in grouped.get(candidate, [])
                    )
                ]
                group_id = compatible[0] if compatible else number_sessions[-1]
        grouped.setdefault(group_id, []).append(item)

    groups: list[ChronologyGroup] = []
    for group_id, members in grouped.items():
        if members[0].parsed is None:
            ordered = members
        else:
            ordered = sorted(
                members,
                key=lambda item: (
                    item.parsed.chapter if item.parsed is not None else 0,
                    item.source.discovery_index,
                ),
            )
        warnings: list[str] = []
        if any(item.parsed is not None for item in ordered):
            seen_chapters: set[int] = set()
            for item in ordered:
                assert item.parsed is not None
                if item.parsed.chapter in seen_chapters:
                    warnings.append(
                        f"duplicate chapter: file {item.parsed.file_number} "
                        f"chapter {item.parsed.chapter}"
                    )
                seen_chapters.add(item.parsed.chapter)
        if "-session-" in group_id:
            warnings.append("session boundary: repeated chapter 1/file number")
        group = ChronologyGroup(group_id, tuple(ordered), tuple(warnings))
        for warning in warnings:
            group = _add_group_warning(group, warning)
        groups.append(group)

    groups = _sort_groups(groups)

    parsed_group_numbers: list[int] = []
    for group in sorted(groups, key=_first_index):
        group_number = _group_file_number(group)
        if group_number is not None:
            parsed_group_numbers.append(group_number)
    if any(right < left for left, right in pairwise(parsed_group_numbers)):
        warning = "numbering reset: file numbers decrease in discovery order"
        groups = [
            _add_group_warning(group, warning)
            if _group_file_number(group) is not None
            else group
            for group in groups
        ]

    if _metadata_conflict(groups):
        warning = "filename/metadata conflict: creation time disagrees with file numbering"
        groups = [_add_group_warning(group, warning) for group in groups]

    for index, group in enumerate(groups):
        if _group_time(group) is None:
            if group.group_id.startswith("discovery-"):
                warning = "chronology uncertain: no GoPro sequence or creation metadata"
            else:
                warning = "chronology uncertain: no creation metadata; used numeric continuity"
            groups[index] = _add_group_warning(group, warning)

    return groups
