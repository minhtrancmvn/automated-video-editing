from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from video_editor.media.discovery import SourceCandidate
from video_editor.media.probe import AudioStream, MediaProbe, VideoStream
from video_editor.media.sequencing import (
    ChronologyGroup,
    ParsedSequence,
    SequencedSource,
)
from video_editor.planning.sample_plan import create_sample_plans


def _member(path: Path, index: int, *, duration: float = 12.0) -> SequencedSource:
    candidate = SourceCandidate(path, 100, index, f"fingerprint-{index}", "bounded-v1")
    return SequencedSource(
        candidate,
        ParsedSequence("GOPR", index + 1, 1),
        datetime(2026, 1, 1, tzinfo=UTC),
        "creation_time",
        "high",
    )


def _probe(path: Path, duration: float, *, audio: bool = True) -> MediaProbe:
    return MediaProbe(
        path=path,
        duration=duration,
        video=VideoStream(width=1920, height=1080, codec_name="h264"),
        audio=AudioStream(codec_name="aac") if audio else None,
    )


def _inputs(
    tmp_path: Path,
) -> tuple[list[ChronologyGroup], dict[object, MediaProbe], dict[object, Path]]:
    first = tmp_path / "GOPR0001.MP4"
    second = tmp_path / "GOPR0002.MP4"
    groups = [
        ChronologyGroup("one", (_member(first, 0),)),
        ChronologyGroup("two", (_member(second, 1),)),
    ]
    probes: dict[object, MediaProbe] = {
        str(first): _probe(first, 12),
        str(second): _probe(second, 3),
    }
    paths: dict[object, Path] = {str(first): first, str(second): second}
    return groups, probes, paths


def test_sample_plans_have_required_outputs(tmp_path: Path) -> None:
    ordered_sources, probes, paths = _inputs(tmp_path)
    horizontal, vertical = create_sample_plans(ordered_sources, probes, paths)

    assert (horizontal.output.width, horizontal.output.height) == (1920, 1080)
    assert (vertical.output.width, vertical.output.height) == (1080, 1920)
    assert horizontal.clips[0].framing.mode == "fit_background"
    assert vertical.clips[0].framing.mode == "fit_background"
    assert all(
        clip.selection_reason == "phase1_sample" and clip.confidence is None
        for clip in horizontal.clips
    )
    assert horizontal.provenance.planner == "phase1-sample-v1"


def test_plans_follow_chronology_and_have_no_gaps(tmp_path: Path) -> None:
    groups, probes, paths = _inputs(tmp_path)
    horizontal, _ = create_sample_plans(groups, probes, paths)

    assert [clip.source_id for clip in horizontal.clips] == [
        "fingerprint-0",
        "fingerprint-1",
    ]
    assert [clip.source_start for clip in horizontal.clips] == [
        Decimal(0),
        Decimal(0),
    ]
    assert [clip.timeline_start for clip in horizontal.clips] == [
        Decimal(0),
        Decimal(8),
    ]
    assert [clip.source_end for clip in horizontal.clips] == [
        Decimal(8),
        Decimal(3),
    ]


def test_plans_cap_each_interval_without_padding_and_preserve_audio(
    tmp_path: Path,
) -> None:
    groups, probes, paths = _inputs(tmp_path)
    probes[str(tmp_path / "GOPR0002.MP4")] = _probe(
        tmp_path / "GOPR0002.MP4", 3, audio=False
    )
    horizontal, vertical = create_sample_plans(groups, probes, paths)

    assert horizontal.output.audio == "source"
    assert vertical.output.audio == "source"
    assert horizontal.sources[-1].has_audio is False
    assert all(clip.source_end <= Decimal(8) for clip in horizontal.clips)
    assert sum(
        clip.source_end - clip.source_start for clip in horizontal.clips
    ) == Decimal(11)


def test_sources_without_video_or_duration_are_skipped_not_padded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bad.mp4"
    groups = [ChronologyGroup("bad", (_member(path, 0),))]
    probes: dict[object, MediaProbe] = {
        str(path): MediaProbe(path=path, video=None, duration=None)
    }

    try:
        create_sample_plans(
            groups,
            probes,
            {str(path): path},
        )
    except ValueError as exc:
        assert "no usable" in str(exc)
    else:
        raise AssertionError("planner must not create padded material")


def test_sample_seconds_is_clamped_to_eight_per_source(tmp_path: Path) -> None:
    groups, probes, paths = _inputs(tmp_path)
    horizontal, vertical = create_sample_plans(
        groups,
        probes,
        paths,
        sample_seconds=Decimal(999),
    )

    assert [clip.source_end for clip in horizontal.clips] == [Decimal(8), Decimal(3)]
    assert [clip.source_end for clip in vertical.clips] == [Decimal(8), Decimal(3)]


@pytest.mark.parametrize(
    "sample_seconds",
    ["invalid", Decimal("NaN"), Decimal("Infinity"), Decimal(0), Decimal(-1)],
)
def test_invalid_sample_seconds_raises_stable_value_error(
    tmp_path: Path,
    sample_seconds: Decimal | str,
) -> None:
    groups, probes, paths = _inputs(tmp_path)

    with pytest.raises(
        ValueError, match="sample_seconds must be a finite positive number"
    ):
        create_sample_plans(groups, probes, paths, sample_seconds=sample_seconds)
