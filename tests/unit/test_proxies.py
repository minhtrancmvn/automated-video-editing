from decimal import Decimal
from pathlib import Path

import pytest

from video_editor.media.proxies import (
    ProxySettings,
    build_audio_args,
    build_proxy_args,
    identity_mapping,
)


def test_audio_is_whisper_compatible(tmp_path: Path) -> None:
    args = build_audio_args(tmp_path / "in.mp4", tmp_path / "out.wav")
    assert args[-6:] == [
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        str(tmp_path / "out.wav"),
    ]
    assert "-ac" in args
    assert args[args.index("-ac") + 1] == "1"


def test_proxy_args_are_bounded_and_muted(tmp_path: Path) -> None:
    args = build_proxy_args(
        tmp_path / "clip;touch owned.mp4",
        tmp_path / "proxy.mp4",
        ProxySettings(),
    )
    assert args[0] == "ffmpeg"
    assert args[-1] == str(tmp_path / "proxy.mp4")
    assert "-an" in args
    assert "-r" in args
    assert args[args.index("-r") + 1] == "15"
    assert "libx264" in args
    assert "scale='min(960,iw)':-2" in args


def test_full_proxy_mapping_is_explicit() -> None:
    mapping = identity_mapping("source-1", Decimal("12.5"), "hash", "ffmpeg-8")
    assert (mapping.source_start, mapping.source_end) == (Decimal(0), Decimal("12.5"))
    assert (mapping.proxy_start, mapping.proxy_end) == (Decimal(0), Decimal("12.5"))
    assert mapping.source_id == "source-1"
    assert mapping.settings_hash == "hash"
    assert mapping.tool_version == "ffmpeg-8"


def test_default_proxy_settings() -> None:
    assert ProxySettings() == ProxySettings(
        max_width=960, fps=15, video_codec="libx264"
    )


def test_no_audio_returns_none_and_outputs_stay_in_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from video_editor.media.probe import MediaProbe, VideoStream
    from video_editor.media.proxies import create_analysis_media

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        "video_editor.media.proxies.probe_media",
        lambda path, ffprobe="ffprobe": MediaProbe(
            path=path, duration=12.5, video=VideoStream(width=1920, height=1080)
        ),
    )
    calls: list[tuple[list[str], dict]] = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        Path(args[-1]).write_bytes(b"derived")
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr("video_editor.media.proxies.subprocess.run", run)
    proxy, audio, mapping = create_analysis_media(
        source,
        "source-1",
        cache,
        duration=Decimal("12.5"),
    )

    assert proxy.parent == cache
    assert audio is None
    assert proxy != source
    assert source not in (proxy, audio)
    assert proxy.exists()
    assert not list(cache.glob("*.partial"))
    assert mapping.source_end == Decimal("12.5")
    assert all(kwargs.get("shell") is False for _, kwargs in calls)


def test_audio_output_is_created_only_inside_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from video_editor.media.probe import AudioStream, MediaProbe, VideoStream
    from video_editor.media.proxies import create_analysis_media

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        "video_editor.media.proxies.probe_media",
        lambda path, ffprobe="ffprobe": MediaProbe(
            path=path,
            duration=12.5,
            video=VideoStream(width=1920, height=1080),
            audio=AudioStream(channels=2),
        ),
    )

    def run(args, **kwargs):
        Path(args[-1]).write_bytes(b"derived")
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr("video_editor.media.proxies.subprocess.run", run)
    proxy, audio, _ = create_analysis_media(source, "source-1", cache, duration=12.5)

    assert proxy.parent == cache
    assert audio is not None and audio.parent == cache
    assert proxy.exists() and audio.exists()
    assert all(path.suffix != ".partial" for path in cache.iterdir())
