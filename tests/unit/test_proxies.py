import importlib.util
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

_fixture_spec = importlib.util.spec_from_file_location(
    "video_editor_test_fixtures", Path(__file__).parents[1] / "fixtures.py"
)
assert _fixture_spec is not None and _fixture_spec.loader is not None
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)
create_media_fixture = _fixture_module.create_media_fixture
from video_editor.media.probe import AudioStream, MediaProbe, VideoStream, probe_media
from video_editor.media.proxies import (
    ProxySettings,
    build_audio_args,
    build_proxy_args,
    identity_mapping,
)


def write_derived(args: list[str], **kwargs: object) -> object:
    Path(args[-1]).write_bytes(b"derived")
    return type("Result", (), {"returncode": 0, "stderr": ""})()


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
    from video_editor.media.probe import MediaProbe
    from video_editor.media.proxies import create_analysis_media

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        "video_editor.media.proxies.probe_media",
        lambda path, ffprobe="ffprobe": MediaProbe(
            path=path,
            duration=12.5,
            video=VideoStream(
                codec_name="h264", width=960, height=540, avg_frame_rate=15
            ),
        ),
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> object:
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
    from video_editor.media.probe import MediaProbe
    from video_editor.media.proxies import create_analysis_media

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    cache = tmp_path / "cache"

    def probe(path: Path, ffprobe: str = "ffprobe") -> MediaProbe:
        if path.name.endswith(".audio.wav.partial"):
            return MediaProbe(
                path=path,
                format_name="wav",
                audio=AudioStream(
                    codec_name="pcm_s16le", channels=1, sample_rate=16000
                ),
            )
        return MediaProbe(
            path=path,
            duration=12.5,
            video=VideoStream(
                codec_name="h264", width=960, height=540, avg_frame_rate=15
            ),
            audio=None
            if path.name.endswith(".proxy.mp4.partial")
            else AudioStream(channels=2),
        )

    monkeypatch.setattr("video_editor.media.proxies.probe_media", probe)

    def run(args: list[str], **kwargs: Any) -> object:
        Path(args[-1]).write_bytes(b"derived")
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr("video_editor.media.proxies.subprocess.run", run)
    proxy, audio, _ = create_analysis_media(source, "source-1", cache, duration=12.5)

    assert proxy.parent == cache
    assert audio is not None and audio.parent == cache
    assert proxy.exists() and audio.exists()
    assert all(path.suffix != ".partial" for path in cache.iterdir())


@pytest.mark.parametrize(
    "video",
    [
        None,
        VideoStream(codec_name="h265", width=960, avg_frame_rate=15),
        VideoStream(codec_name="h264", width=961, avg_frame_rate=15),
        VideoStream(codec_name="h264", width=960, avg_frame_rate=30),
    ],
)
def test_invalid_proxy_properties_are_not_renamed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, video: VideoStream | None
) -> None:
    from video_editor.errors import ErrorCategory, VideoEditorError
    from video_editor.media.probe import MediaProbe
    from video_editor.media.proxies import create_analysis_media

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    cache = tmp_path / "cache"

    def probe(path: Path, ffprobe: str = "ffprobe") -> MediaProbe:
        if path.name.endswith(".partial"):
            return MediaProbe(path=path, video=video)
        return MediaProbe(path=path, duration=12.5, video=VideoStream())

    monkeypatch.setattr("video_editor.media.proxies.probe_media", probe)
    monkeypatch.setattr("video_editor.media.proxies.subprocess.run", write_derived)

    with pytest.raises(VideoEditorError) as raised:
        create_analysis_media(source, "source-1", cache)

    assert raised.value.category == ErrorCategory.OUTPUT
    assert not list(cache.glob("*.proxy.mp4"))
    assert not list(cache.glob("*.partial"))
    assert source.read_bytes() == b"source"


@pytest.mark.parametrize(
    "format_name,audio",
    [
        ("mov,mp4", AudioStream(codec_name="pcm_s16le", channels=1, sample_rate=16000)),
        ("wav", None),
        ("wav", AudioStream(codec_name="aac", channels=1, sample_rate=16000)),
        ("wav", AudioStream(codec_name="pcm_s16le", channels=2, sample_rate=16000)),
        ("wav", AudioStream(codec_name="pcm_s16le", channels=1, sample_rate=44100)),
    ],
)
def test_invalid_audio_properties_are_not_renamed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    format_name: str,
    audio: AudioStream | None,
) -> None:
    from video_editor.errors import ErrorCategory, VideoEditorError
    from video_editor.media.probe import MediaProbe
    from video_editor.media.proxies import create_analysis_media

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    cache = tmp_path / "cache"
    proxy_video = VideoStream(
        codec_name="h264", width=960, height=540, avg_frame_rate=15
    )

    def probe(path: Path, ffprobe: str = "ffprobe") -> MediaProbe:
        if path.name.endswith(".proxy.mp4.partial"):
            return MediaProbe(path=path, video=proxy_video)
        if path.name.endswith(".audio.wav.partial"):
            return MediaProbe(path=path, format_name=format_name, audio=audio)
        return MediaProbe(
            path=path,
            duration=12.5,
            video=VideoStream(),
            audio=AudioStream(),
        )

    monkeypatch.setattr("video_editor.media.proxies.probe_media", probe)
    monkeypatch.setattr("video_editor.media.proxies.subprocess.run", write_derived)

    with pytest.raises(VideoEditorError) as raised:
        create_analysis_media(source, "source-1", cache)

    assert raised.value.category == ErrorCategory.OUTPUT
    assert len(list(cache.glob("*.proxy.mp4"))) == 1
    assert not list(cache.glob("*.audio.wav"))
    assert not list(cache.glob("*.partial"))
    assert source.read_bytes() == b"source"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_default_proxy_accepts_ffmpeg_stream_codec_metadata(tmp_path: Path) -> None:
    from video_editor.media.proxies import create_analysis_media

    source = create_media_fixture(tmp_path / "source.mp4", with_audio=False)
    proxy, audio, mapping = create_analysis_media(
        source,
        "source-1",
        tmp_path / "cache",
    )

    inspected = probe_media(proxy)
    assert inspected.video is not None
    assert inspected.video.codec_name == "h264"
    assert audio is None
    assert mapping.source_end > 0


def test_cached_media_validation_rejects_truncated_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from video_editor.errors import ErrorCategory, VideoEditorError
    from video_editor.media.proxies import valid_cached_media

    cached = tmp_path / "cached.proxy.mp4"
    cached.write_bytes(b"truncated")
    monkeypatch.setattr(
        "video_editor.media.proxies.probe_media",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            VideoEditorError(ErrorCategory.INSPECTION, "invalid media")
        ),
    )
    assert not valid_cached_media(
        cached, ProxySettings(), kind="proxy", ffprobe="ffprobe"
    )


def test_analysis_media_reuses_valid_proxy_and_audio_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from video_editor.media.proxies import _name, create_analysis_media

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    cache = tmp_path / "cache"
    settings = ProxySettings()
    proxy = cache / _name(source, "source-1", ".proxy.mp4", settings, "ffmpeg")
    audio = cache / _name(source, "source-1", ".audio.wav", settings, "ffmpeg")
    cache.mkdir()
    proxy.write_bytes(b"cached proxy")
    audio.write_bytes(b"cached audio")

    def probe(path: Path, ffprobe: str = "ffprobe") -> MediaProbe:
        if path == source:
            return MediaProbe(
                path=path,
                duration=12.5,
                video=VideoStream(codec_name="h264", width=960, avg_frame_rate=15),
                audio=AudioStream(codec_name="aac", channels=2, sample_rate=44100),
            )
        if path == proxy:
            return MediaProbe(
                path=path,
                format_name="mov,mp4",
                video=VideoStream(codec_name="h264", width=960, avg_frame_rate=15),
            )
        return MediaProbe(
            path=path,
            format_name="wav",
            audio=AudioStream(codec_name="pcm_s16le", channels=1, sample_rate=16000),
        )

    monkeypatch.setattr("video_editor.media.proxies.probe_media", probe)
    monkeypatch.setattr(
        "video_editor.media.proxies.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("valid cache must not regenerate"),
    )

    reused_proxy, reused_audio, mapping = create_analysis_media(
        source,
        "source-1",
        cache,
        settings=settings,
        tool_version="ffmpeg",
    )

    assert reused_proxy == proxy
    assert reused_audio == audio
    assert mapping.source_id == "source-1"


def test_proxy_replace_failure_cleans_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from video_editor.media.proxies import _validated_rename

    partial = tmp_path / "proxy.mp4.partial"
    final = tmp_path / "proxy.mp4"
    partial.write_bytes(b"derived")
    monkeypatch.setattr(
        "video_editor.media.proxies.probe_media",
        lambda path, ffprobe="ffprobe": MediaProbe(
            path=path,
            video=VideoStream(codec_name="h264", width=960, avg_frame_rate=15),
        ),
    )

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("rename failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="rename failed"):
        _validated_rename(partial, final, ProxySettings(), ffprobe="ffprobe")

    assert not partial.exists()
    assert not final.exists()


def test_ffprobe_failure_cleans_partial_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from video_editor.errors import ErrorCategory, VideoEditorError
    from video_editor.media.probe import MediaProbe
    from video_editor.media.proxies import create_analysis_media

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    cache = tmp_path / "cache"
    calls = 0

    def probe(path: Path, ffprobe: str = "ffprobe") -> MediaProbe:
        nonlocal calls
        calls += 1
        if calls == 1:
            return MediaProbe(path=path, duration=12.5, video=VideoStream())
        raise VideoEditorError(ErrorCategory.INSPECTION, "ffprobe failed")

    monkeypatch.setattr("video_editor.media.proxies.probe_media", probe)
    monkeypatch.setattr("video_editor.media.proxies.subprocess.run", write_derived)

    with pytest.raises(VideoEditorError, match="ffprobe failed") as raised:
        create_analysis_media(source, "source-1", cache)

    assert raised.value.category == ErrorCategory.INSPECTION
    assert not list(cache.glob("*.partial"))
    assert not list(cache.glob("*.proxy.mp4"))
    assert source.read_bytes() == b"source"
