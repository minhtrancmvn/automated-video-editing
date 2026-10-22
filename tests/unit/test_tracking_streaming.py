"""Tracking a short candidate must not hold a whole long proxy in memory."""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import cv2
import numpy as np
import pytest

from video_editor.analysis import tracking as tracking_module
from video_editor.analysis.models import NormalizedSubjectBox, SubjectObservation
from video_editor.analysis.tracking import track_subject
from video_editor.config import CropSettings
from video_editor.media.proxies import ProxyMapping

_fixture_spec = importlib.util.spec_from_file_location(
    "video_editor_test_fixtures", Path(__file__).parents[1] / "fixtures.py"
)
assert _fixture_spec is not None and _fixture_spec.loader is not None
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)
create_tracking_fixture = _fixture_module.create_tracking_fixture

D = Decimal
FPS = 10
PAD = 600
IDENTITY = "bounded-v1:tracking-fixture"


class _CountingCapture:
    """Fixture frames embedded between long padding; counts retrieved frames."""

    def __init__(self, frames: list[np.ndarray]) -> None:
        self._frames = frames
        self._blank = np.full_like(frames[0], 20)
        self.total = PAD + len(frames) + PAD
        self.position = 0
        self.retrieved = 0

    def isOpened(self) -> bool:
        return True

    def get(self, prop: int) -> float:
        return float(FPS) if prop == cv2.CAP_PROP_FPS else 0.0

    def grab(self) -> bool:
        if self.position >= self.total:
            return False
        self.position += 1
        return True

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self.position >= self.total:
            return False, None
        index = self.position - PAD
        frame = self._frames[index] if 0 <= index < len(self._frames) else self._blank
        self.position += 1
        self.retrieved += 1
        return True, frame.copy()

    def release(self) -> None:
        pass


def _seed(time: str) -> SubjectObservation:
    return SubjectObservation(
        observation_id="seed-1",
        source_id="source-1",
        source_identity=IDENTITY,
        source_time=D(time),
        box=NormalizedSubjectBox(
            x_min=D("0.08"), y_min=D("0.30"), x_max=D("0.25"), y_max=D("0.62")
        ),
        priority=2,
        origin="gemini_seed",
    )


def _mapping(seconds: Decimal) -> ProxyMapping:
    return ProxyMapping(
        source_id="source-1",
        source_start=D("100"),
        source_end=D("100") + seconds,
        proxy_start=D("0"),
        proxy_end=seconds,
        source_identity=IDENTITY,
        settings_hash="proxy-settings-hash",
        tool_version="fixture-v1",
    )


def _track(proxy: Path, mapping: ProxyMapping, start: Decimal) -> object:
    return track_subject(
        proxy,
        mapping,
        clip_id="clip-1",
        candidate_start=start,
        candidate_end=start + D("6"),
        subject_observations=(_seed(str(start)),),
        source_size=(1920, 1080),
        output_size=(1080, 1920),
        settings=CropSettings(),
    )


def test_long_proxy_tracks_window_without_retaining_whole_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = create_tracking_fixture(tmp_path)
    capture = cv2.VideoCapture(str(proxy))
    frames: list[np.ndarray] = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(frame)
    capture.release()
    reference = _track(proxy, _mapping(D("6")), D("100"))

    fake = _CountingCapture(frames)
    monkeypatch.setattr(tracking_module.cv2, "VideoCapture", lambda _path: fake)
    seconds = D(fake.total) / FPS
    track = _track(proxy, _mapping(seconds), D("100") + D(PAD) / FPS)

    assert track.observations
    assert len(track.observations) == len(reference.observations)
    assert [
        (item.time, item.center_x, item.center_y, item.fallback)
        for item in track.keyframes
    ] == [
        (item.time, item.center_x, item.center_y, item.fallback)
        for item in reference.keyframes
    ]
    assert track.short_eligible == reference.short_eligible
    # Only the window, its preceding frame, and one stop frame are decoded to memory.
    assert fake.retrieved <= len(frames) + 2
    assert fake.position < fake.total
