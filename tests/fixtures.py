"""Small local media fixtures for proxy and integration tests."""

from __future__ import annotations

import math
import subprocess
import wave
from pathlib import Path

import cv2
import numpy as np


def create_media_fixture(
    path: Path, *, with_audio: bool = True, duration_seconds: int = 1
) -> Path:
    """Create tiny deterministic MP4 fixture using local FFmpeg only."""

    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")

    path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x240:r=10",
        "-t",
        str(duration_seconds),
    ]
    if with_audio:
        args.extend(["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono"])
    args.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    if with_audio:
        args.extend(["-c:a", "aac", "-shortest"])
    args.append(str(path))
    completed = subprocess.run(
        args, check=False, capture_output=True, text=True, shell=False
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "ffmpeg fixture creation failed")
    return path


def create_segmentation_fixture(root: Path) -> tuple[Path, Path]:
    """Create deterministic four-second proxy and mono PCM evidence fixture."""
    root.mkdir(parents=True, exist_ok=True)
    proxy = root / "segmentation-proxy.avi"
    audio = root / "segmentation-audio.wav"
    fps = 20
    width = 160
    height = 120
    writer = cv2.VideoWriter(
        str(proxy), cv2.VideoWriter_fourcc(*"MJPG"), fps, (width, height)
    )
    if not writer.isOpened():
        raise RuntimeError("OpenCV fixture video writer failed")
    for frame_index in range(4 * fps):
        second = frame_index / fps
        if second < 2:
            frame = np.full((height, width, 3), (30, 80, 180), dtype=np.uint8)
            if second >= 1:
                x = 10 + int((second - 1) * 100)
                cv2.rectangle(frame, (x, 45), (x + 24, 69), (240, 240, 240), -1)
        else:
            frame = np.full((height, width, 3), (40, 180, 40), dtype=np.uint8)
            cv2.line(frame, (0, 0), (width - 1, height - 1), (200, 40, 200), 3)
            cv2.line(frame, (0, height - 1), (width - 1, 0), (200, 40, 200), 3)
            if second < 2.5:
                frame = cv2.GaussianBlur(frame, (21, 21), 0)
            elif second < 3:
                frame.fill(255)
            elif second < 3.5:
                frame.fill(0)
        writer.write(frame)
    writer.release()

    sample_rate = 16_000
    samples: list[int] = []
    for index in range(4 * sample_rate):
        second = index / sample_rate
        if second < 1:
            value = 0
        elif 2.75 <= second < 2.77:
            value = 28_000
        else:
            value = int(8_000 * math.sin(2 * math.pi * 440 * second))
        samples.append(value)
    pcm = np.asarray(samples, dtype="<i2")
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())
    return proxy, audio


def create_luminance_cut_fixture(root: Path) -> Path:
    """Create deterministic black-to-white hard-cut proxy fixture."""
    root.mkdir(parents=True, exist_ok=True)
    proxy = root / "luminance-cut-proxy.avi"
    fps = 20
    size = (160, 120)
    writer = cv2.VideoWriter(str(proxy), cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
    if not writer.isOpened():
        raise RuntimeError("OpenCV fixture video writer failed")
    width, height = size
    for frame_index in range(2 * fps):
        value = 0 if frame_index < fps else 255
        writer.write(np.full((height, width, 3), value, dtype=np.uint8))
    writer.release()
    return proxy


def create_mono_wav(path: Path, *, frame_count: int, sample_rate: int = 16_000) -> Path:
    """Create deterministic mono 16-bit PCM WAV with requested frame count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.zeros(frame_count, dtype="<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())
    return path


def create_tracking_fixture(
    root: Path,
    *,
    occlusion: tuple[int, int] | None = None,
    frame_count: int = 60,
) -> Path:
    """Create deterministic textured subject movement with optional occlusion."""
    root.mkdir(parents=True, exist_ok=True)
    proxy = root / "tracking-proxy.avi"
    fps = 10
    width, height = 160, 90
    writer = cv2.VideoWriter(
        str(proxy), cv2.VideoWriter_fourcc(*"MJPG"), fps, (width, height)
    )
    if not writer.isOpened():
        raise RuntimeError("OpenCV fixture video writer failed")
    for frame_index in range(frame_count):
        frame = np.full((height, width, 3), 20, dtype=np.uint8)
        hidden = occlusion is not None and occlusion[0] <= frame_index < occlusion[1]
        if not hidden:
            x = 15 + min(frame_index, 40) * 2
            y = 30
            cv2.rectangle(frame, (x, y), (x + 24, y + 24), (235, 235, 235), -1)
            for dx in (4, 12, 20):
                for dy in (4, 12, 20):
                    color = 15 if (dx + dy) % 8 == 0 else 100
                    cv2.circle(frame, (x + dx, y + dy), 2, (color, color, color), -1)
        writer.write(frame)
    writer.release()
    return proxy
