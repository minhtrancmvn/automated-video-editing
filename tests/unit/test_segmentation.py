from __future__ import annotations

import importlib.util
import json
from dataclasses import fields
from decimal import Decimal
from pathlib import Path

import cv2
import pytest

from video_editor.analysis import segmentation
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


def test_read_video_decodes_sequentially_without_random_seek(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy, _ = create_segmentation_fixture(tmp_path)
    seek_calls: list[float] = []
    original_capture = cv2.VideoCapture

    class CaptureSpy:
        def __init__(self, path: str) -> None:
            self.inner = original_capture(path)

        def isOpened(self) -> bool:
            return self.inner.isOpened()

        def get(self, prop: int) -> float:
            return self.inner.get(prop)

        def set(self, prop: int, value: float) -> bool:
            seek_calls.append(value)
            return self.inner.set(prop, value)

        def read(self) -> tuple[bool, object]:
            return self.inner.read()

        def release(self) -> None:
            self.inner.release()

    monkeypatch.setattr(segmentation.cv2, "VideoCapture", CaptureSpy)

    stream, duration = segmentation._read_video(proxy, SegmentationSettings())
    samples = list(stream)

    assert duration == D(4)
    assert len(samples) == 40
    assert [timestamp for timestamp, _ in samples] == [D(i) / D(10) for i in range(40)]
    assert seek_calls == []


def test_read_video_uses_sequential_decoded_frames_for_exact_sample_schedule(
    tmp_path: Path,
) -> None:
    proxy, _ = create_segmentation_fixture(tmp_path)
    settings = SegmentationSettings(sample_fps=4)
    stream, duration = segmentation._read_video(proxy, settings)
    scheduled = list(stream)
    import cv2

    capture = cv2.VideoCapture(str(proxy))
    try:
        fps = D(str(capture.get(cv2.CAP_PROP_FPS)))
        expected = []
        target = D(0)
        decoded_index = 0
        while target < duration:
            ok, frame = capture.read()
            if not ok:
                break
            if D(decoded_index) / fps >= target:
                expected.append((target, frame))
                target += D(1) / D(settings.sample_fps)
            decoded_index += 1
    finally:
        capture.release()
    assert [timestamp for timestamp, _ in scheduled] == [
        timestamp for timestamp, _ in expected
    ]
    assert all(
        (actual == reference).all()
        for (_, actual), (_, reference) in zip(scheduled, expected, strict=True)
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


@pytest.mark.parametrize(
    "source_start", [D(0), D("100.00000000000000000000000000000001")]
)
def test_map_time_preserves_exact_endpoints_beyond_decimal_precision(
    source_start: Decimal,
) -> None:
    from video_editor.analysis.segmentation import _map_time

    source_end = D("900.0324669999999969149939715862274169921875")
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=source_start,
        source_end=source_end,
        proxy_start=D("10.00000000000000000000000000000001"),
        proxy_end=D("910.0324669999999969149939715862274169921875"),
        source_identity="bounded-v1:controlled-fixture",
        settings_hash="proxy-settings-hash",
        tool_version="fixture-v1",
    )

    assert _map_time(mapping.proxy_start, mapping) == mapping.source_start
    assert _map_time(mapping.proxy_end, mapping) == mapping.source_end
    assert mapping.source_start < _map_time(D(450), mapping) < mapping.source_end


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
    assert payload["implementation_version"] == "local-segmentation-v4"
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
    ) -> tuple[object, Decimal]:
        stream, _ = original_read_video(proxy_path, settings)

        def samples():
            last = None
            for timestamp, frame in stream:
                last = frame
                yield timestamp, frame
            assert last is not None
            yield D("4"), last

        return samples(), D("4.02")

    monkeypatch.setattr(segmentation, "_read_video", drifted_read_video)

    result = segment_media(proxy, wav, mapping)

    assert result.scenes[0].proxy_range.start == D("10")
    assert result.scenes[0].source_range.start == D("100")
    assert result.scenes[-1].proxy_range.end == D("14")
    assert result.scenes[-1].source_range.end == D("108")
    assert result.motion[-1].proxy_range.end == D("14")
    assert result.motion[-1].source_range.end == D("108")


@pytest.mark.parametrize("shortfall", [D("0.032467"), D("0.05")])
def test_audio_evidence_accepts_bounded_encoder_tail_without_extending_samples(
    tmp_path: Path, shortfall: Decimal
) -> None:
    from video_editor.analysis.segmentation import _audio_evidence

    wav = create_mono_wav(tmp_path / "tail.wav", frame_count=4 * 16_000)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("100"),
        source_end=D("104") + shortfall,
        proxy_start=D(0),
        proxy_end=D(4) + shortfall,
        source_identity="bounded-v1:controlled-fixture",
        settings_hash="proxy-settings-hash",
        tool_version="fixture-v1",
    )

    energy, silence, speech, transients = _audio_evidence(
        wav, mapping, SegmentationSettings()
    )

    assert energy[0].proxy_range.start == 0
    assert energy[-1].proxy_range.end == D(4) + D(1) / D(16_000)
    assert energy[-1].proxy_range.end < mapping.proxy_end
    assert all(
        item.proxy_range.end <= energy[-1].proxy_range.end
        for collection in (silence, speech, transients)
        for item in collection
    )


@pytest.mark.parametrize("seconds", [D("3.9499375"), D("3.9"), D("4.1")])
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


def _overshoot_mapping(proxy: Path, frames: int) -> ProxyMapping:
    import cv2

    capture = cv2.VideoCapture(str(proxy))
    fps = D(str(capture.get(cv2.CAP_PROP_FPS)))
    count = D(str(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
    capture.release()
    proxy_end = count / fps - D(frames) / fps
    return ProxyMapping(
        source_id="source-1",
        source_start=D("100"),
        source_end=D("100") + proxy_end,
        proxy_start=D("0"),
        proxy_end=proxy_end,
        source_identity="bounded-v1:controlled-fixture",
        settings_hash="proxy-settings-hash",
        tool_version="fixture-v1",
    )


def test_segmentation_accepts_two_frame_positive_decoder_overshoot(
    tmp_path: Path,
) -> None:
    proxy, _ = create_segmentation_fixture(tmp_path)
    mapping = _overshoot_mapping(proxy, 2)

    result = segment_media(proxy, None, mapping)

    assert result.scenes[-1].proxy_range.end == mapping.proxy_end
    assert result.scenes[-1].source_range.end == mapping.source_end


def test_segmentation_rejects_three_frame_decoder_overshoot(tmp_path: Path) -> None:
    proxy, _ = create_segmentation_fixture(tmp_path)

    with pytest.raises(ValueError, match="decoded proxy duration"):
        segment_media(proxy, None, _overshoot_mapping(proxy, 3))
