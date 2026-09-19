"""Detection of local FFmpeg and host capabilities."""

from __future__ import annotations

import platform
import re
import subprocess
import sys

from pydantic import BaseModel, ConfigDict, Field


class HostCapabilities(BaseModel):
    """Observed toolchain and host resources, not proof of encoder usability."""

    model_config = ConfigDict(extra="forbid")

    ffmpeg_version: str | None = None
    ffprobe_version: str | None = None
    architecture: str = "unknown"
    memory_bytes: int | None = None
    encoders: list[str] = Field(default_factory=list)
    hardware_encoders: list[str] = Field(default_factory=list)


def _run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, shell=False, check=False)
    except OSError:
        return None


def _version(output: str) -> str | None:
    match = re.search(r"\bversion\s+([^\s]+)", output, re.IGNORECASE)
    return match.group(1) if match else None


def _encoders(output: str) -> list[str]:
    result: list[str] = []
    for line in output.splitlines():
        # FFmpeg prints six capability flags, whose first character identifies
        # stream type. Keep rows such as ``V....D libx264`` and ignore headers.
        match = re.match(r"\s*([A-Z\.]{6})\s+(\S+)", line)
        if match and match.group(1)[0] in "VAS":
            result.append(match.group(2))
    return result


def _memory_bytes() -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        output = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            shell=False,
            check=True,
        ).stdout
        return int(output.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def detect_capabilities(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> HostCapabilities:
    """Inspect installed binaries and host facts using argument vectors only."""

    ffmpeg_version_run = _run([ffmpeg, "-version"])
    ffprobe_version_run = _run([ffprobe, "-version"])
    encoder_run = _run([ffmpeg, "-hide_banner", "-encoders"])
    encoders = _encoders((encoder_run.stdout + encoder_run.stderr) if encoder_run else "")
    hardware = [
        encoder
        for encoder in encoders
        if "videotoolbox" in encoder or encoder.endswith(("_nvenc", "_qsv", "_vaapi"))
    ]
    return HostCapabilities(
        ffmpeg_version=_version((ffmpeg_version_run.stdout + ffmpeg_version_run.stderr) if ffmpeg_version_run else ""),
        ffprobe_version=_version((ffprobe_version_run.stdout + ffprobe_version_run.stderr) if ffprobe_version_run else ""),
        architecture=platform.machine() or "unknown",
        memory_bytes=_memory_bytes(),
        encoders=encoders,
        hardware_encoders=hardware,
    )
