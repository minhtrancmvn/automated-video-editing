"""Small local media fixtures for proxy and integration tests."""

from __future__ import annotations

import subprocess
from pathlib import Path


def create_media_fixture(path: Path, *, with_audio: bool = True) -> Path:
    """Create tiny deterministic MP4 fixture using local FFmpeg only."""

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
        "1",
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
