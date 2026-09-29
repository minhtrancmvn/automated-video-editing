from __future__ import annotations

import importlib.util
import json
import os
import shutil
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from video_editor.analysis.proxy_chunks import (
    CloudProxySettings,
    ProxyManifest,
    create_cloud_proxy_chunk,
    plan_chunk_ranges,
    register_proxy_manifest,
    validate_upload_candidate,
)
from video_editor.config import PathSettings
from video_editor.errors import VideoEditorError
from video_editor.media.discovery import IDENTITY_VERSION, bounded_fingerprint
from video_editor.media.probe import probe_media
from video_editor.media.proxies import ProxyMapping
from video_editor.persistence.database import JobStore

_fixture_spec = importlib.util.spec_from_file_location(
    "video_editor_test_fixtures", Path(__file__).parents[1] / "fixtures.py"
)
assert _fixture_spec is not None and _fixture_spec.loader is not None
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)
create_media_fixture = _fixture_module.create_media_fixture

D = Decimal


@dataclass
class RegisteredChunk:
    manifest: ProxyManifest
    store: JobStore
    paths: PathSettings
    source: Path


def _paths(tmp_path: Path) -> PathSettings:
    return PathSettings(
        input_dir=tmp_path / "input",
        workspace_dir=tmp_path / "workspace",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
    )


def _registered_chunk(tmp_path: Path) -> RegisteredChunk:
    paths = _paths(tmp_path)
    source = create_media_fixture(
        paths.input_dir / "source.mp4", with_audio=True, duration_seconds=2
    )
    fingerprint = bounded_fingerprint(source)
    source_id = "source-1"
    source_identity = f"{IDENTITY_VERSION}:{fingerprint}"
    mapping = ProxyMapping(
        source_id=source_id,
        source_start=D("0"),
        source_end=D("2"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity=source_identity,
        settings_hash="phase1-settings",
        tool_version="ffmpeg-test",
    )
    store = JobStore(paths.state_dir / "jobs.db")
    store.__enter__()
    job_id = store.create_job(
        {"input_path": str(paths.input_dir)},
        {},
    )
    store.save_sources(
        job_id,
        [
            {
                "source_id": source_id,
                "path": str(source),
                "size_bytes": source.stat().st_size,
                "fingerprint": fingerprint,
                "identity_version": IDENTITY_VERSION,
            }
        ],
    )
    generated_root = paths.cache_dir / job_id
    manifest = create_cloud_proxy_chunk(
        source,
        generated_root,
        mapping,
        D("0"),
        D("1.5"),
        job_id=job_id,
        source_fingerprint=fingerprint,
    )
    register_proxy_manifest(manifest, store)
    return RegisteredChunk(manifest, store, paths, source)


def _rewrite_manifest_data(registered: RegisteredChunk, **changes: object) -> None:
    row = registered.store.connection.execute(
        "SELECT data_json FROM proxy_manifests WHERE manifest_id = ?",
        (registered.manifest.manifest_id,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row["data_json"])
    payload.update(changes)
    registered.store.connection.execute(
        "UPDATE proxy_manifests SET data_json = ? WHERE manifest_id = ?",
        (
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            registered.manifest.manifest_id,
        ),
    )
    registered.store.connection.commit()


def _rewrite_chunk_data(registered: RegisteredChunk, **changes: object) -> None:
    row = registered.store.connection.execute(
        "SELECT data_json FROM analysis_chunks WHERE chunk_id = ?",
        (registered.manifest.chunk_id,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row["data_json"])
    payload.update(changes)
    registered.store.connection.execute(
        "UPDATE analysis_chunks SET data_json = ? WHERE chunk_id = ?",
        (
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            registered.manifest.chunk_id,
        ),
    )
    registered.store.connection.commit()


def test_chunk_ranges_cover_source_once_and_split_near_safe_boundaries() -> None:
    ranges = plan_chunk_ranges(
        D("2000"),
        [D("710"), D("1430")],
        CloudProxySettings(),
    )

    assert ranges == [
        (D("0"), D("710")),
        (D("710"), D("1430")),
        (D("1430"), D("2000")),
    ]
    assert ranges[0][0] == D("0")
    assert ranges[-1][1] == D("2000")
    assert all(left[1] == right[0] for left, right in pairwise(ranges))


def test_chunk_ranges_use_fixed_target_without_safe_boundary() -> None:
    assert plan_chunk_ranges(D("1600"), [], CloudProxySettings()) == [
        (D("0"), D("720")),
        (D("720"), D("1440")),
        (D("1440"), D("1600")),
    ]


@pytest.mark.parametrize(
    ("duration", "boundaries", "message"),
    [
        (D("0"), [], "duration"),
        (D("NaN"), [], "duration"),
        (D("1000"), [D("700"), D("600")], "monotonic"),
        (D("1000"), [D("1001")], "source duration"),
    ],
)
def test_chunk_ranges_reject_invalid_coverage_inputs(
    duration: Decimal, boundaries: list[Decimal], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        plan_chunk_ranges(duration, boundaries, CloudProxySettings())


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_cloud_proxy_chunk_is_real_bounded_media_and_is_registered(
    tmp_path: Path,
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        manifest = registered.manifest
        inspected = probe_media(manifest.path)

        assert manifest.path.is_file()
        assert manifest.path.parent == registered.paths.cache_dir / manifest.job_id
        assert not list(manifest.path.parent.glob("*.partial"))
        assert inspected.video is not None
        assert inspected.video.codec_name == "h264"
        assert inspected.video.width is not None and inspected.video.width <= 640
        assert inspected.video.avg_frame_rate == pytest.approx(15)
        assert inspected.audio is not None
        assert inspected.audio.codec_name == "aac"
        assert inspected.audio.channels == 1
        assert manifest.data.audio_probe_bitrate_bps > 0

        stored_manifest = registered.store.get_proxy_manifest(manifest.manifest_id)
        stored_chunk = registered.store.get_analysis_chunk(manifest.chunk_id)
        assert stored_manifest is not None
        assert stored_manifest["job_id"] == manifest.job_id
        assert stored_manifest["data"] == manifest.data.model_dump(mode="json")
        assert stored_chunk is not None
        assert stored_chunk["manifest_id"] == manifest.manifest_id
        assert stored_chunk["data"] == manifest.chunk_data.model_dump(mode="json")
        assert (
            validate_upload_candidate(
                manifest.manifest_id, registered.store, registered.paths
            )
            == manifest.path
        )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_registered_file_replaced_after_manifest_is_rejected(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        registered.manifest.path.write_bytes(b"replacement")

        with pytest.raises(VideoEditorError, match="digest"):
            validate_upload_candidate(
                registered.manifest.manifest_id, registered.store, registered.paths
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_symlink_to_original_is_rejected_even_inside_cache(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        registered.manifest.path.unlink()
        registered.manifest.path.symlink_to(registered.source)

        with pytest.raises(VideoEditorError, match="original"):
            validate_upload_candidate(
                registered.manifest.manifest_id, registered.store, registered.paths
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_hard_link_to_original_is_rejected(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        registered.manifest.path.unlink()
        os.link(registered.source, registered.manifest.path)

        with pytest.raises(VideoEditorError, match="original"):
            validate_upload_candidate(
                registered.manifest.manifest_id, registered.store, registered.paths
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_symlinked_generated_parent_is_rejected(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        actual_root = registered.manifest.path.parent
        relocated_root = actual_root.with_name(actual_root.name + "-actual")
        actual_root.rename(relocated_root)
        actual_root.symlink_to(relocated_root, target_is_directory=True)

        with pytest.raises(VideoEditorError, match="links"):
            validate_upload_candidate(
                registered.manifest.manifest_id, registered.store, registered.paths
            )
    finally:
        registered.store.__exit__(None, None, None)


def test_unregistered_manifest_is_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    with JobStore(paths.state_dir / "jobs.db") as store:
        store.create_job({"input_path": str(paths.input_dir)}, {})

        with pytest.raises(VideoEditorError, match="unregistered"):
            validate_upload_candidate("missing", store, paths)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_registration_rolls_back_manifest_when_chunk_save_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        manifest = registered.manifest
        registered.store.connection.execute(
            "DELETE FROM analysis_chunks WHERE chunk_id = ?", (manifest.chunk_id,)
        )
        registered.store.connection.execute(
            "DELETE FROM proxy_manifests WHERE manifest_id = ?", (manifest.manifest_id,)
        )
        registered.store.connection.commit()

        def fail_chunk_save(*args: object, **kwargs: object) -> None:
            raise RuntimeError("chunk write failed")

        monkeypatch.setattr(registered.store, "save_analysis_chunk", fail_chunk_save)

        with pytest.raises(RuntimeError, match="chunk write failed"):
            register_proxy_manifest(manifest, registered.store)

        assert registered.store.get_proxy_manifest(manifest.manifest_id) is None
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_manifest_job_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        _rewrite_manifest_data(registered, job_id="wrong-job")

        with pytest.raises(VideoEditorError, match="job"):
            validate_upload_candidate(
                registered.manifest.manifest_id, registered.store, registered.paths
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_manifest_source_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        _rewrite_manifest_data(registered, source_id="wrong-source")

        with pytest.raises(VideoEditorError, match="source"):
            validate_upload_candidate(
                registered.manifest.manifest_id, registered.store, registered.paths
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_manifest_path_escape_is_rejected(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        escaped = tmp_path / "escaped.mp4"
        escaped.write_bytes(registered.manifest.path.read_bytes())
        _rewrite_manifest_data(registered, artifact_path=str(escaped))

        with pytest.raises(VideoEditorError, match="generated root"):
            validate_upload_candidate(
                registered.manifest.manifest_id, registered.store, registered.paths
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_input_descendant_is_rejected(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        input_copy = registered.paths.input_dir / "not-uploadable.mp4"
        input_copy.write_bytes(registered.manifest.path.read_bytes())
        _rewrite_manifest_data(registered, artifact_path=str(input_copy))

        with pytest.raises(VideoEditorError, match="input root"):
            validate_upload_candidate(
                registered.manifest.manifest_id, registered.store, registered.paths
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("video_width", 641, "width"),
        ("video_fps", "30", "FPS"),
        ("video_codec", "hevc", "video codec"),
        ("audio_codec", "mp3", "audio codec"),
        ("audio_channels", 2, "audio channels"),
        ("audio_bitrate_bps", 128000, "audio bitrate"),
        ("audio_probe_bitrate_bps", 1, "audio probe bitrate"),
    ],
)
def test_wrong_persisted_stream_settings_are_rejected(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        _rewrite_manifest_data(registered, **{field: value})

        with pytest.raises(VideoEditorError, match=message):
            validate_upload_candidate(
                registered.manifest.manifest_id, registered.store, registered.paths
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_non_monotonic_manifest_mapping_is_rejected(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        _rewrite_manifest_data(
            registered,
            source_start="1.5",
            source_end="1.0",
        )

        with pytest.raises(VideoEditorError, match="mapping"):
            validate_upload_candidate(
                registered.manifest.manifest_id, registered.store, registered.paths
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_manifest_and_chunk_mapping_mismatch_is_rejected(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        _rewrite_chunk_data(registered, mapping_version="wrong-version")

        with pytest.raises(VideoEditorError, match="mapping"):
            validate_upload_candidate(
                registered.manifest.manifest_id, registered.store, registered.paths
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_stale_source_identity_is_rejected(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        with registered.source.open("ab") as source_file:
            source_file.write(b"changed")

        with pytest.raises(VideoEditorError, match="source identity"):
            validate_upload_candidate(
                registered.manifest.manifest_id, registered.store, registered.paths
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_missing_source_is_rejected_as_unsafe_upload_candidate(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        registered.source.unlink()

        with pytest.raises(VideoEditorError) as caught:
            validate_upload_candidate(
                registered.manifest.manifest_id, registered.store, registered.paths
            )

        assert caught.value.code == "unsafe_upload_candidate"
        assert "source" in str(caught.value)
    finally:
        registered.store.__exit__(None, None, None)
