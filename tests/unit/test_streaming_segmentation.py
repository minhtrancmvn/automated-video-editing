from __future__ import annotations

import weakref
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
        source_id="source-stream",
        source_start=D(100),
        source_end=D(102),
        proxy_start=D(10),
        proxy_end=D(12),
        source_identity="bounded-v1:stream-fixture",
        settings_hash="settings",
        tool_version="fixture",
    )


def _capture_factory(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    state: dict[str, object] = {"references": [], "peak": 0, "released": 0}
    references: list[weakref.ReferenceType[np.ndarray]] = []
    state["references"] = references

    class Capture:
        def __init__(self, path: str) -> None:
            self.index = 0

        def isOpened(self) -> bool:
            return True

        def get(self, prop: int) -> float:
            return 15.0 if prop == cv2.CAP_PROP_FPS else 30.0

        def read(self) -> tuple[bool, np.ndarray | None]:
            if self.index == 30:
                return False, None
            frame = np.full((16, 16, 3), self.index * 7, dtype=np.uint8)
            self.index += 1
            references.append(weakref.ref(frame))
            state["peak"] = max(
                int(state["peak"]), sum(r() is not None for r in references)
            )
            return True, frame

        def release(self) -> None:
            state["released"] = int(state["released"]) + 1

    monkeypatch.setattr(segmentation.cv2, "VideoCapture", Capture)
    return state


def test_segment_media_retains_bounded_decoded_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _capture_factory(monkeypatch)

    result = segmentation.segment_media(Path("fixture.mp4"), None, _mapping())

    assert len(result.motion) == 20
    assert int(state["peak"]) <= 4
    assert all(reference() is None for reference in state["references"])


def test_streamed_evidence_matches_materialized_frame_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_factory(monkeypatch)
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
    expected_scenes = segmentation._scene_ranges(frames, D(12), mapping, settings)
    expected_video = segmentation._frame_evidence(frames, D(12), mapping, settings)

    result = segmentation.segment_media(Path("fixture.mp4"), None, mapping, settings)

    assert result.scenes == expected_scenes
    for field, key in (
        ("motion", "motion"),
        ("motion_continuity", "motion-continuity"),
        ("blur", "blur"),
        ("shake", "shake"),
        ("exposure", "exposure"),
        ("obstruction", "obstruction"),
    ):
        assert getattr(result, field) == expected_video[key]
    assert result.motion[0].source_range.start == D(100)
    assert result.motion[-1].source_range.end == D(102)


def test_metric_failure_releases_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _capture_factory(monkeypatch)

    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("metric failed")

    monkeypatch.setattr(segmentation, "_histogram", fail)
    with pytest.raises(RuntimeError, match="metric failed"):
        segmentation.segment_media(Path("fixture.mp4"), None, _mapping())

    assert all(reference() is None for reference in state["references"])
    assert int(state["released"]) >= 1
