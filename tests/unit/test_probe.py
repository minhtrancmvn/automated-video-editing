from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from video_editor.media.probe import MediaProbe, probe_media, summarize_batch_warnings


def completed_probe(payload: dict | None = None) -> subprocess.CompletedProcess[str]:
    if payload is None:
        payload = {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "profile": "Main 10",
                    "pix_fmt": "yuv420p10le",
                    "width": 3840,
                    "height": 2160,
                    "duration": "12.5",
                    "avg_frame_rate": "60000/1001",
                    "r_frame_rate": "30/1",
                    "color_primaries": "bt2020",
                    "color_transfer": "smpte2084",
                    "color_space": "bt2020nc",
                    "color_range": "tv",
                    "tags": {"rotate": "90"},
                    "side_data_list": [
                        {"side_data_type": "Display Matrix", "rotation": -90}
                    ],
                }
            ],
            "format": {
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "duration": "12.5",
                "tags": {"creation_time": "2024-01-02T03:04:05Z"},
            },
        }
    return subprocess.CompletedProcess(["ffprobe"], 0, json.dumps(payload), "")


def test_probe_parses_stream_metadata_and_warnings(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed_probe())

    result = probe_media(tmp_path / "clip.mp4")

    assert isinstance(result, MediaProbe)
    assert result.duration == 12.5
    assert result.creation_time is not None
    assert result.video is not None
    assert result.video.codec_name == "hevc"
    assert result.video.width == 3840
    assert result.video.height == 2160
    assert result.video.avg_frame_rate == pytest.approx(60000 / 1001)
    assert result.video.r_frame_rate == 30.0
    assert result.video.rotation == -90
    assert result.audio is None
    assert {warning.code for warning in result.warnings} == {
        "hevc",
        "ten_bit",
        "rotation",
        "hdr",
        "possible_vfr",
        "missing_audio",
    }


def test_probe_uses_argument_vector_without_shell(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)) or completed_probe(),
    )

    probe_media(tmp_path / "clip;touch owned.mp4")

    assert calls[0][0][-1].endswith("clip;touch owned.mp4")
    assert calls[0][1].get("shell", False) is False
    assert calls[0][0][0] == "ffprobe"
    assert calls[0][0][1:6] == [
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-print_format",
    ]


@pytest.mark.parametrize("color_value", ["unknown", "unspecified", "reserved"])
def test_probe_warns_for_uninformative_color_values(
    monkeypatch,
    tmp_path: Path,
    color_value: str,
) -> None:
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": 1920,
                "height": 1080,
                "color_transfer": color_value,
            }
        ],
        "format": {},
    }
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: completed_probe(payload)
    )

    result = probe_media(tmp_path / "clip.mp4")

    assert result.video is not None
    assert result.video.color_transfer == color_value
    assert {warning.code for warning in result.warnings} == {
        "missing_audio",
        "unknown_color",
    }


def test_probe_preserves_unknown_color_as_warning(monkeypatch, tmp_path: Path) -> None:
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": 1920,
                "height": 1080,
                "color_transfer": "mystery",
            }
        ],
        "format": {},
    }
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: completed_probe(payload)
    )

    result = probe_media(tmp_path / "clip.mp4")

    assert result.video is not None
    assert result.video.color_transfer == "mystery"
    assert {warning.code for warning in result.warnings} == {
        "missing_audio",
        "unknown_color",
    }


@pytest.mark.parametrize(
    ("payload", "expected_codes"),
    [
        ({"streams": [], "format": {}}, {"missing_audio", "no_video"}),
        (
            {
                "streams": [{"codec_type": "audio", "codec_name": "aac"}],
                "format": {},
            },
            {"no_video"},
        ),
        (
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "pix_fmt": "yuv420p",
                        "width": 1920,
                        "height": 1080,
                    }
                ]
            },
            {"missing_audio", "missing_format"},
        ),
        (
            {"streams": [{"codec_type": "video", "codec_name": "h264"}], "format": {}},
            {"missing_audio", "missing_video_metadata"},
        ),
    ],
)
def test_probe_warns_for_incomplete_media_payloads(
    monkeypatch,
    tmp_path: Path,
    payload: dict,
    expected_codes: set[str],
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: completed_probe(payload)
    )

    result = probe_media(tmp_path / "clip.mp4")

    assert {warning.code for warning in result.warnings} == expected_codes


def test_batch_warning_summary_deduplicates_codes() -> None:
    from video_editor.media.probe import InspectionWarning

    warning = InspectionWarning(code="hevc", message="HEVC")
    assert summarize_batch_warnings(
        [
            MediaProbe(path="a", warnings=[warning]),
            MediaProbe(path="b", warnings=[warning]),
        ]
    ) == [warning]


def test_probe_reports_ffprobe_failure(monkeypatch, tmp_path: Path) -> None:
    failed = subprocess.CompletedProcess(["ffprobe"], 1, "", "bad input")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: failed)

    with pytest.raises(Exception, match="ffprobe"):
        probe_media(tmp_path / "clip.mp4")
