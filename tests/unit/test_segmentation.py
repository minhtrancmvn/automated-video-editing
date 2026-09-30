from __future__ import annotations

import importlib.util
import json
from dataclasses import fields
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
create_luminance_cut_fixture = _fixture_module.create_luminance_cut_fixture
create_mono_wav = _fixture_module.create_mono_wav

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
    assert payload["implementation_version"] == "local-segmentation-v3"
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


def test_segmentation_anchors_unequal_mapping_despite_decoder_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy, wav = create_segmentation_fixture(tmp_path)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("100"),
        source_end=D("108"),
        proxy_start=D("10"),
        proxy_end=D("14"),
        source_identity="bounded-v1:controlled-fixture",
        settings_hash="proxy-settings-hash",
        tool_version="fixture-v1",
    )
    from video_editor.analysis import segmentation

    original_read_video = segmentation._read_video

    def drifted_read_video(
        proxy_path: Path, settings: SegmentationSettings
    ) -> tuple[list[tuple[Decimal, object]], Decimal]:
        samples, _ = original_read_video(proxy_path, settings)
        samples.append((D("4"), samples[-1][1]))
        return samples, D("4.02")

    monkeypatch.setattr(segmentation, "_read_video", drifted_read_video)

    result = segment_media(proxy, wav, mapping)

    assert result.scenes[0].proxy_range.start == D("10")
    assert result.scenes[0].source_range.start == D("100")
    assert result.scenes[-1].proxy_range.end == D("14")
    assert result.scenes[-1].source_range.end == D("108")
    assert result.motion[-1].proxy_range.end == D("14")
    assert result.motion[-1].source_range.end == D("108")


@pytest.mark.parametrize("seconds", [D("3.9"), D("4.1")])
def test_segmentation_rejects_material_audio_duration_mismatch(
    tmp_path: Path, seconds: Decimal
) -> None:
    proxy, _ = create_segmentation_fixture(tmp_path)
    sample_rate = 16_000
    wav = create_mono_wav(
        tmp_path / f"audio-{seconds}.wav",
        frame_count=int(seconds * sample_rate),
        sample_rate=sample_rate,
    )

    with pytest.raises(ValueError, match="audio duration does not match mapping"):
        segment_media(proxy, wav, _mapping())


def test_segmentation_rejects_one_sample_long_audio(tmp_path: Path) -> None:
    proxy, _ = create_segmentation_fixture(tmp_path)
    sample_rate = 16_000
    wav = create_mono_wav(
        tmp_path / "one-sample-long.wav", frame_count=4 * sample_rate + 1
    )

    with pytest.raises(ValueError, match="audio duration does not match mapping"):
        segment_media(proxy, wav, _mapping())


def test_segmentation_rejects_truncated_audio_payload(tmp_path: Path) -> None:
    proxy, _ = create_segmentation_fixture(tmp_path)
    sample_rate = 16_000
    wav = create_mono_wav(tmp_path / "truncated.wav", frame_count=4 * sample_rate)
    wav.write_bytes(wav.read_bytes()[:-2])

    with pytest.raises(ValueError, match="decoded sample count does not match header"):
        segment_media(proxy, wav, _mapping())


def test_segmentation_rejects_empty_audio(tmp_path: Path) -> None:
    proxy, _ = create_segmentation_fixture(tmp_path)
    wav = create_mono_wav(tmp_path / "empty.wav", frame_count=0)

    with pytest.raises(ValueError, match="audio contains no samples"):
        segment_media(proxy, wav, _mapping())


def test_segmentation_rejects_malformed_audio(tmp_path: Path) -> None:
    proxy, _ = create_segmentation_fixture(tmp_path)
    wav = tmp_path / "malformed.wav"
    wav.write_bytes(b"not-a-wave-file")

    with pytest.raises(ValueError, match="cannot decode analysis audio"):
        segment_media(proxy, wav, _mapping())


def test_segmentation_clips_final_partial_audio_window_to_mapping_end(
    tmp_path: Path,
) -> None:
    proxy, _ = create_segmentation_fixture(tmp_path)
    sample_rate = 16_000
    wav = create_mono_wav(tmp_path / "rounded.wav", frame_count=4 * sample_rate - 1)

    result = segment_media(proxy, wav, _mapping())

    assert result.audio_energy[-1].proxy_range.end == D("4")
    assert result.audio_energy[-1].source_range.end == D("104")


def test_segmentation_emits_independent_boundary_suitability(
    segmented_fixture: tuple[Path, Path, LocalSegmentation],
) -> None:
    _, _, result = segmented_fixture

    assert result.boundary_suitability
    assert result.boundary_suitability[0].proxy_range.start == D("0")
    assert result.boundary_suitability[-1].proxy_range.end == D("4")
    assert result.boundary_suitability[0].source_range.start == D("100")
    assert result.boundary_suitability[-1].source_range.end == D("104")
    assert all(0 <= item.entry_score <= 1 for item in result.boundary_suitability)
    assert all(0 <= item.exit_score <= 1 for item in result.boundary_suitability)


def test_segmentation_detects_luminance_only_hard_cut(tmp_path: Path) -> None:
    proxy = create_luminance_cut_fixture(tmp_path)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("100"),
        source_end=D("102"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity="bounded-v1:luminance-fixture",
        settings_hash="proxy-settings-hash",
        tool_version="fixture-v1",
    )

    result = segment_media(proxy, None, mapping)

    assert any(
        abs(scene.proxy_range.start - D("1")) <= D("0.15")
        for scene in result.scenes[1:]
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (field.name, value)
        for field in fields(SegmentationSettings)
        if field.type == "float"
        for value in (float("nan"), float("inf"), float("-inf"))
    ],
)
def test_segmentation_settings_reject_nonfinite_float(name: str, value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        SegmentationSettings(**{name: value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_segmentation_settings_reject_nonfinite_sample_fps(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        SegmentationSettings(sample_fps=value)


@pytest.mark.parametrize(
    "name",
    [field.name for field in fields(SegmentationSettings) if field.type == "Decimal"],
)
@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("-Infinity")])
def test_segmentation_settings_reject_nonfinite_decimal(
    name: str, value: Decimal
) -> None:
    with pytest.raises(ValueError, match="finite"):
        SegmentationSettings(**{name: value})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("sample_fps", 0),
        ("audio_window_seconds", D("0")),
        ("scene_threshold", 1.01),
        ("silence_rms", 1.01),
        ("transient_delta", 1.01),
        ("blur_variance_threshold", 0.0),
        ("exposure_luma_threshold", 255.01),
        ("obstruction_luma_threshold", 255.01),
        ("obstruction_flat_stddev", 0.0),
        ("peak_merge_seconds", D("-0.1")),
        ("lead_seconds", D("-0.1")),
        ("resolution_seconds", D("-0.1")),
    ],
)
def test_segmentation_settings_reject_out_of_domain_value(
    name: str, value: float | Decimal
) -> None:
    with pytest.raises(ValueError):
        SegmentationSettings(**{name: value})


def test_nonfinite_computed_score_is_rejected() -> None:
    from video_editor.analysis import segmentation

    with pytest.raises(ValueError, match="computed score must be finite"):
        segmentation._clamp(float("nan"))


EVIDENCE_COLLECTIONS = (
    "scenes",
    "silence_ranges",
    "speech_presence_ranges",
    "audio_energy",
    "audio_transients",
    "motion",
    "motion_continuity",
    "blur",
    "shake",
    "exposure",
    "obstruction",
    "boundary_suitability",
    "candidate_windows",
)


def test_all_evidence_ids_are_unique_and_stable(tmp_path: Path) -> None:
    proxy, wav = create_segmentation_fixture(tmp_path)

    first = segment_media(proxy, wav, _mapping())
    second = segment_media(proxy, wav, _mapping())
    first_ids = [
        item.evidence_id
        for name in EVIDENCE_COLLECTIONS
        for item in getattr(first, name)
    ]
    second_ids = [
        item.evidence_id
        for name in EVIDENCE_COLLECTIONS
        for item in getattr(second, name)
    ]

    assert first_ids
    assert first_ids == second_ids
    assert len(first_ids) == len(set(first_ids))
