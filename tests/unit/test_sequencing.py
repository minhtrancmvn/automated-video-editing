from datetime import UTC, datetime
from pathlib import Path

import pytest

from video_editor.media.discovery import SourceCandidate
from video_editor.media.sequencing import parse_gopro_name, sequence_sources


@pytest.mark.parametrize(
    ("name", "file_number", "chapter"),
    [
        ("GOPR0123.MP4", 123, 1),
        ("GP020123.MP4", 123, 2),
        ("GH010456.MP4", 456, 1),
        ("GX030456.MP4", 456, 3),
    ],
)
def test_parse_observed_gopro_shapes(name: str, file_number: int, chapter: int) -> None:
    parsed = parse_gopro_name(name)
    assert parsed is not None
    assert (parsed.file_number, parsed.chapter) == (file_number, chapter)


@pytest.mark.parametrize(
    "name",
    [
        "GOPR0123.MP4;rm -rf /",
        "prefix-GOPR0123.MP4",
        "GOPR0123.MP4.bak",
        "GPé20123.MP4",
        "GP020123.MP4\n",
    ],
)
def test_parse_rejects_untrusted_or_partial_names(name: str) -> None:
    assert parse_gopro_name(name) is None


def _candidates(names: list[str]) -> list[SourceCandidate]:
    return [
        SourceCandidate(
            path=Path(name),
            size_bytes=1,
            discovery_index=index,
            fingerprint=f"fingerprint-{index}",
            identity_version="bounded-v1",
        )
        for index, name in enumerate(names)
    ]


def test_chapters_group_by_file_number_and_order_numerically() -> None:
    groups = sequence_sources(
        _candidates(["GP030123.MP4", "GOPR0123.MP4", "GP020123.MP4"]), {}
    )
    assert [
        [member.source.path.name for member in group.members] for group in groups
    ] == [["GOPR0123.MP4", "GP020123.MP4", "GP030123.MP4"]]


def test_repeated_chapter_one_starts_new_session_group() -> None:
    sources = _candidates(["GOPR0001.MP4", "GP020001.MP4", "GOPR0001.MP4"])
    groups = sequence_sources(sources, {})

    assert [group.group_id for group in groups] == ["1", "1-session-1"]
    assert all(len(group.members) for group in groups)
    assert any("session boundary" in warning for warning in groups[1].warnings)


def test_interleaved_repeated_sessions_preserve_discovery_chunks_with_warning() -> None:
    sources = _candidates(
        [
            "session-a/GOPR0001.MP4",
            "session-b/GOPR0001.MP4",
            "session-a/GP020001.MP4",
            "session-b/GP020001.MP4",
        ]
    )
    groups = sequence_sources(sources, {})

    assert [
        [member.source.path.as_posix() for member in group.members] for group in groups
    ] == [
        ["session-a/GOPR0001.MP4", "session-a/GP020001.MP4"],
        ["session-b/GOPR0001.MP4", "session-b/GP020001.MP4"],
    ]
    assert all(
        "session ambiguity" not in warning
        for group in groups
        for warning in group.warnings
    )


def test_ambiguous_repeated_sessions_preserve_chunks_and_warn() -> None:
    sources = _candidates(
        ["GOPR0001.MP4", "GOPR0001.MP4", "GP020001.MP4", "GP020001.MP4"]
    )
    groups = sequence_sources(sources, {})

    assert [
        [member.source.path.name for member in group.members] for group in groups
    ] == [["GOPR0001.MP4", "GP020001.MP4"], ["GOPR0001.MP4", "GP020001.MP4"]]
    assert all(
        any("session ambiguity" in warning for warning in group.warnings)
        for group in groups
    )


def test_duplicate_chapter_emits_warning() -> None:
    groups = sequence_sources(_candidates(["GP020123.MP4", "GH020123.MP4"]), {})
    assert any("duplicate chapter" in warning for warning in groups[0].warnings)
    assert any(
        "duplicate chapter" in warning for warning in groups[0].members[0].warnings
    )


def test_numbering_reset_emits_warning() -> None:
    groups = sequence_sources(_candidates(["GOPR0123.MP4", "GOPR0001.MP4"]), {})
    assert any(
        "numbering reset" in warning for group in groups for warning in group.warnings
    )


def test_creation_metadata_orders_groups_before_numeric_fallback() -> None:
    sources = _candidates(["GOPR0200.MP4", "GOPR0100.MP4", "GOPR0300.MP4"])
    times = {
        "GOPR0200.MP4": datetime(2026, 1, 1, tzinfo=UTC),
        "GOPR0100.MP4": datetime(2025, 1, 1, tzinfo=UTC),
    }
    groups = sequence_sources(sources, times)
    assert [group.members[0].source.path.name for group in groups] == [
        "GOPR0100.MP4",
        "GOPR0200.MP4",
        "GOPR0300.MP4",
    ]
    assert groups[0].members[0].order_evidence == "creation_time"


def test_filename_metadata_conflict_emits_warning() -> None:
    sources = _candidates(["GOPR0100.MP4", "GOPR0200.MP4"])
    times = {
        "GOPR0100.MP4": datetime(2026, 1, 2, tzinfo=UTC),
        "GOPR0200.MP4": datetime(2026, 1, 1, tzinfo=UTC),
    }
    groups = sequence_sources(sources, times)
    assert any(
        "filename/metadata conflict" in warning
        for group in groups
        for warning in group.warnings
    )


def test_non_gopro_falls_back_to_discovery_order_with_low_confidence() -> None:
    groups = sequence_sources(_candidates(["phone.mp4", "camera.mov"]), {})
    assert [group.members[0].source.path.name for group in groups] == [
        "phone.mp4",
        "camera.mov",
    ]
    assert all(
        member.confidence == "low" for group in groups for member in group.members
    )
    assert all(
        member.order_evidence == "discovery_order"
        for group in groups
        for member in group.members
    )
