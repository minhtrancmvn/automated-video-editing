from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import cv2
import numpy as np
import pytest

from video_editor.analysis import segmentation
from video_editor.media.proxies import ProxyMapping

D = Decimal


def _mapping() -> ProxyMapping:
    return ProxyMapping(
        source_id="source-1",
        source_start=D(100),
        source_end=D(102),
        proxy_start=D(10),
        proxy_end=D(12),
        source_identity="bounded-v1:fixture",
        settings_hash="settings",
        tool_version="fixture",
    )


def _captures(
    monkeypatch: pytest.MonkeyPatch, *, count: int = 31, failure: str | None = None
) -> list[object]:
    instances = []

    class Capture:
        def __init__(self, path: str) -> None:
            self.index = 0
            self.released = False
            self.number = len(instances)
            instances.append(self)

        def isOpened(self) -> bool:
            return not (failure == "second_open" and self.number == 4)

        def get(self, prop: int) -> float:
            if failure == "metadata":
                return float("nan")
            return 15.0 if prop == cv2.CAP_PROP_FPS else float(count)

        def read(self) -> tuple[bool, np.ndarray | None]:
            if failure == "second_decode" and self.number == 4:
                raise RuntimeError("decode failed")
            if failure == "empty" or self.index == count:
                return False, None
            frame = np.full((16, 16, 3), self.index * 7, dtype=np.uint8)
            self.index += 1
            return True, frame

        def release(self) -> None:
            self.released = True

    monkeypatch.setattr(segmentation.cv2, "VideoCapture", Capture)
    return instances


def test_nonzero_origin_tail_evidence_matches_clipped_materialized_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances = _captures(monkeypatch)
    mapping = _mapping()
    settings = segmentation.SegmentationSettings()
    frames = []
    timestamp = D(0)
    for i in range(20):
        frames.append(
            (
                D(10) + timestamp,
                np.full((16, 16, 3), ((3 * i + 1) // 2) * 7, dtype=np.uint8),
            )
        )
        timestamp += D("0.1")
    scenes = segmentation._scene_ranges(frames, D(12), mapping, settings)
    video = segmentation._frame_evidence(frames, D(12), mapping, settings)

    result = segmentation.segment_media(Path("tail.mp4"), None, mapping)

    assert result.scenes == scenes
    for field, key in (
        ("motion", "motion"),
        ("motion_continuity", "motion-continuity"),
        ("blur", "blur"),
        ("shake", "shake"),
        ("exposure", "exposure"),
        ("obstruction", "obstruction"),
    ):
        assert getattr(result, field) == video[key]
        assert all(record.proxy_range.end <= D(12) for record in getattr(result, field))
        assert all(
            record.source_range.end <= D(102) for record in getattr(result, field)
        )
    assert all(capture.released for capture in instances)


@pytest.mark.parametrize(
    "failure", ["metadata", "second_open", "second_decode", "empty", "frame_metric"]
)
def test_failed_streaming_paths_release_all_opened_captures(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    instances = _captures(monkeypatch, failure=failure)
    if failure == "frame_metric":

        def fail(*args: object, **kwargs: object) -> object:
            raise RuntimeError("metric failed")

        monkeypatch.setattr(segmentation.cv2, "Laplacian", fail)

    with pytest.raises((ValueError, RuntimeError)):
        segmentation.segment_media(Path("failure.mp4"), None, _mapping())

    assert instances
    assert all(capture.released for capture in instances)


def test_duration_rejection_closes_metadata_capture_without_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances = _captures(monkeypatch, count=45)

    with pytest.raises(
        ValueError, match="decoded proxy duration does not match mapping"
    ):
        segmentation.segment_media(Path("long.mp4"), None, _mapping())

    assert all(capture.released for capture in instances)
    assert all(capture.index == 0 for capture in instances)
