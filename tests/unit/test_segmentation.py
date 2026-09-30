from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

from video_editor.analysis.models import LocalSegmentation
from video_editor.analysis.segmentation import (
    SegmentationSettings,
    canonical_segmentation_json,
    segment_media,
)
from video_editor.media.proxies import ProxyMapping

_fixture_spec = importlib.util.spec_from_file_location(
    "video_editor_test_fixtures", Path(__file__).parents[1] / "fixtures.py"
)
assert _fixture_spec is not None and _fixture_spec.loader is not None
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)
create_segmentation_fixture = _fixture_module.create_segmentation_fixture

D = Decimal


def _mapping() -> ProxyMapping:
    return ProxyMapping(
        source_id="source-1",
        source_start=D("100"),
        source_end=D("104"),
        proxy_start=D("0"),
        proxy_end=D("4"),
        source_identity="bounded-v1:controlled-fixture",
        settings_hash="proxy-settings-hash",
        tool_version="fixture-v1",
    )


def _average_score(
    result: LocalSegmentation, field: str, start: str, end: str
) -> float:
    records = getattr(result, field)
    lower = D(start)
    upper = D(end)
    scores = [
        record.score for record in records if lower <= record.proxy_range.start < upper
    ]
    assert scores
    return sum(scores) / len(scores)


@pytest.fixture
def segmented_fixture(tmp_path: Path) -> tuple[Path, Path, LocalSegmentation]:
    proxy, wav = create_segmentation_fixture(tmp_path)
    result = segment_media(proxy, wav, _mapping(), SegmentationSettings())
    return proxy, wav, result


def test_segmentation_maps_proxy_ranges_to_source_time_exactly(
    segmented_fixture: tuple[Path, Path, LocalSegmentation],
) -> None:
    _, _, result = segmented_fixture

    assert result.scenes[0].source_range.start == D("100")
    assert result.scenes[-1].source_range.end == D("104")
    assert any(
        abs(scene.proxy_range.start - D("2")) <= D("0.15")
        for scene in result.scenes[1:]
    )


def test_segmentation_preserves_nonzero_proxy_time_origin(tmp_path: Path) -> None:
    proxy, wav = create_segmentation_fixture(tmp_path)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("100"),
        source_end=D("104"),
        proxy_start=D("10"),
        proxy_end=D("14"),
        source_identity="bounded-v1:controlled-fixture",
        settings_hash="proxy-settings-hash",
        tool_version="fixture-v1",
    )

    result = segment_media(proxy, wav, mapping, SegmentationSettings())

    assert result.scenes[0].proxy_range.start == D("10")
    assert result.scenes[0].source_range.start == D("100")
    assert result.scenes[-1].proxy_range.end == D("14")
    assert result.scenes[-1].source_range.end == D("104")


def test_segmentation_detects_silence_speech_presence_and_transient(
    segmented_fixture: tuple[Path, Path, LocalSegmentation],
) -> None:
    _, _, result = segmented_fixture

    assert result.silence_ranges[0].proxy_range.start == D("0")
    assert abs(result.silence_ranges[0].proxy_range.end - D("1")) <= D("0.1")
    assert abs(result.speech_presence_ranges[0].proxy_range.start - D("1")) <= D("0.1")
    assert result.speech_presence_ranges[-1].proxy_range.end == D("4")
    assert any(
        abs(transient.proxy_range.start - D("2.75")) <= D("0.15")
        for transient in result.audio_transients
    )


def test_segmentation_orders_motion_and_quality_evidence(
    segmented_fixture: tuple[Path, Path, LocalSegmentation],
) -> None:
    _, _, result = segmented_fixture

    assert _average_score(result, "motion", "1", "2") > _average_score(
        result, "motion", "0", "1"
    )
    assert _average_score(result, "blur", "2", "2.5") > _average_score(
        result, "blur", "3.5", "4"
    )
    assert _average_score(result, "exposure", "2.5", "3") > _average_score(
        result, "exposure", "3.5", "4"
    )
    assert _average_score(result, "obstruction", "3", "3.5") > _average_score(
        result, "obstruction", "3.5", "4"
    )
    for field in (
        "motion",
        "motion_continuity",
        "blur",
        "shake",
        "exposure",
        "obstruction",
    ):
        assert all(0.0 <= evidence.score <= 1.0 for evidence in getattr(result, field))


def test_speech_presence_never_contains_transcript_text(
    segmented_fixture: tuple[Path, Path, LocalSegmentation],
) -> None:
    _, _, result = segmented_fixture

    payload = result.model_dump(mode="json")
    assert "transcript" not in json.dumps(payload).lower()
    assert "tone" not in json.dumps(payload).lower()


def test_segmentation_json_is_canonical_and_byte_stable(tmp_path: Path) -> None:
    proxy, wav = create_segmentation_fixture(tmp_path)
    settings = SegmentationSettings()

    first = segment_media(proxy, wav, _mapping(), settings)
    second = segment_media(proxy, wav, _mapping(), settings)
    first_json = canonical_segmentation_json(first)
    second_json = canonical_segmentation_json(second)
    payload = json.loads(first_json)

    assert first_json == second_json
    assert first_json.endswith(b"\n")
    assert payload["implementation_version"] == "local-segmentation-v1"
    assert payload["settings_hash"]
    assert payload["source_identity"] == "bounded-v1:controlled-fixture"
    evidence = [
        *payload["scenes"],
        *payload["audio_energy"],
        *payload["motion"],
        *payload["blur"],
    ]
    evidence_ids = [record["evidence_id"] for record in evidence]
    assert evidence_ids
    assert len(evidence_ids) == len(set(evidence_ids))
    assert all(evidence_id.startswith("local-") for evidence_id in evidence_ids)


def test_segmentation_accepts_missing_audio(tmp_path: Path) -> None:
    proxy, _ = create_segmentation_fixture(tmp_path)

    result = segment_media(proxy, None, _mapping(), SegmentationSettings())

    assert result.audio_energy == ()
    assert result.silence_ranges == ()
    assert result.speech_presence_ranges == ()
    assert result.audio_transients == ()
