from __future__ import annotations

import subprocess

from video_editor.media.capabilities import HostCapabilities, detect_capabilities


def test_detect_capabilities_parses_versions_architecture_memory_and_encoders(monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "ffmpeg" and "-encoders" not in args:
            return subprocess.CompletedProcess(args, 0, "ffmpeg version 7.0.1 Copyright", "")
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args, 0, "ffprobe version 7.0.1 Copyright", "")
        return subprocess.CompletedProcess(
            args,
            0,
            "Encoders:\n V..... h264_videotoolbox Apple VideoToolbox\n V..... libx264 H.264 / AVC / MPEG-4 AVC / part 10\n A..... aac AAC (Advanced Audio Coding)\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    monkeypatch.setattr("sys.platform", "darwin")
    memory_run = subprocess.CompletedProcess(
        ["sysctl", "-n", "hw.memsize"], 0, "34359738368\n", ""
    )
    original_run = run

    def run_with_memory(args, **kwargs):
        if args[0] == "sysctl":
            return memory_run
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run_with_memory)

    result = detect_capabilities()

    assert isinstance(result, HostCapabilities)
    assert result.ffmpeg_version == "7.0.1"
    assert result.ffprobe_version == "7.0.1"
    assert result.architecture == "arm64"
    assert result.memory_bytes == 34359738368
    assert result.encoders == ["h264_videotoolbox", "libx264", "aac"]
    assert result.hardware_encoders == ["h264_videotoolbox"]
    assert all(kwargs.get("shell", False) is False for _, kwargs in calls)
    assert all(isinstance(args, list) for args, _ in calls)


def test_missing_tools_return_unavailable_capabilities(monkeypatch) -> None:
    def run(args, **kwargs):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(subprocess, "run", run)

    result = detect_capabilities(ffmpeg="missing-ffmpeg", ffprobe="missing-ffprobe")

    assert result.ffmpeg_version is None
    assert result.ffprobe_version is None
    assert result.encoders == []
    assert result.hardware_encoders == []
