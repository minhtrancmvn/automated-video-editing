from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest
from pydantic import ValidationError

from video_editor.analysis.models import ProxyManifestData
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
from video_editor.media.probe import AudioStream, MediaProbe, VideoStream, probe_media
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


@dataclass(frozen=True)
class PersistedMappingStore:
    mapping: ProxyMapping

    def get_proxy_artifact_metadata(
        self, job_id: str, source_id: str
    ) -> dict[str, object]:
        assert source_id == self.mapping.source_id
        return {
            "source_identity": self.mapping.source_identity,
            "settings_hash": self.mapping.settings_hash,
            "tool_version": self.mapping.tool_version,
            "mapping": {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in self.mapping.__dict__.items()
            },
        }


def _paths(tmp_path: Path) -> PathSettings:
    return PathSettings(
        input_dir=tmp_path / "input",
        workspace_dir=tmp_path / "workspace",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
    )


def _registered_chunk(tmp_path: Path, *, register: bool = True) -> RegisteredChunk:
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
    phase1_proxy = paths.cache_dir / f"{source_id}.phase1.proxy.mp4"
    phase1_proxy.parent.mkdir(parents=True, exist_ok=True)
    phase1_proxy.write_bytes(b"phase1 proxy placeholder")
    mapping_data = {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in mapping.__dict__.items()
    }
    store.save_artifact(
        job_id,
        "proxy",
        phase1_proxy,
        {
            "kind": "proxy",
            "source_id": source_id,
            "source_path": str(source),
            "source_identity": source_identity,
            "settings_hash": mapping.settings_hash,
            "tool_version": mapping.tool_version,
            "mapping": mapping_data,
        },
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
        paths=paths,
        store=store,
    )
    if register:
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


def _rewrite_phase1_mapping(registered: RegisteredChunk, **changes: object) -> None:
    row = registered.store.connection.execute(
        "SELECT metadata_json FROM artifacts WHERE job_id = ? AND stage_name = 'proxy'",
        (registered.manifest.job_id,),
    ).fetchone()
    assert row is not None
    metadata = json.loads(row["metadata_json"])
    mapping = metadata["mapping"]
    assert isinstance(mapping, dict)
    mapping.update(changes)
    registered.store.connection.execute(
        "UPDATE artifacts SET metadata_json = ? WHERE job_id = ? AND stage_name = 'proxy'",
        (
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            registered.manifest.job_id,
        ),
    )
    registered.store.connection.commit()


def _retarget_manifest_file(registered: RegisteredChunk, path: Path) -> None:
    file_stat = path.stat(follow_symlinks=False)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    _rewrite_manifest_data(
        registered,
        artifact_path=str(path),
        file_size_bytes=file_stat.st_size,
        file_digest_sha256=digest,
        file_device=file_stat.st_dev,
        file_inode=file_stat.st_ino,
    )
    registered.store.connection.execute(
        "UPDATE proxy_manifests SET digest = ? WHERE manifest_id = ?",
        (digest, registered.manifest.manifest_id),
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
        (D("710"), D("1400")),
        (D("1400"), D("2000")),
    ]
    assert ranges[0][0] == D("0")
    assert ranges[-1][1] == D("2000")
    assert all(left[1] == right[0] for left, right in pairwise(ranges))


def test_chunk_ranges_use_fixed_target_without_safe_boundary() -> None:
    assert plan_chunk_ranges(D("1600"), [], CloudProxySettings()) == [
        (D("0"), D("720")),
        (D("720"), D("1600")),
    ]


def test_chunk_ranges_keep_at_most_maximum_seconds_as_one_final_chunk() -> None:
    assert plan_chunk_ranges(D("850"), [], CloudProxySettings()) == [
        (D("0"), D("850")),
    ]


def test_chunk_ranges_use_unavoidable_short_final_remainder() -> None:
    assert plan_chunk_ranges(D("1000"), [], CloudProxySettings()) == [
        (D("0"), D("720")),
        (D("720"), D("1000")),
    ]


def test_chunk_ranges_choose_boundary_that_leaves_valid_final_remainder() -> None:
    assert plan_chunk_ranges(
        D("1300"),
        [D("650"), D("710")],
        CloudProxySettings(),
    ) == [
        (D("0"), D("650")),
        (D("650"), D("1300")),
    ]


def test_chunk_ranges_balance_fixed_split_to_avoid_short_final_remainder() -> None:
    assert plan_chunk_ranges(D("1250"), [], CloudProxySettings()) == [
        (D("0"), D("650")),
        (D("650"), D("1250")),
    ]


def test_chunk_ranges_ignore_safe_boundary_that_leaves_avoidable_short_tail() -> None:
    assert plan_chunk_ranges(D("1250"), [D("700")], CloudProxySettings()) == [
        (D("0"), D("650")),
        (D("650"), D("1250")),
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
        with validate_upload_candidate(
            manifest.manifest_id,
            manifest.job_id,
            registered.store,
            registered.paths,
        ) as authorized:
            assert authorized.manifest_id == manifest.manifest_id
            assert not hasattr(authorized, "path")
            assert authorized.stream.read() == manifest.path.read_bytes()
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
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
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
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
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
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
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
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_creation_rejects_mapping_that_differs_from_persisted_phase1(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    source = create_media_fixture(
        paths.input_dir / "source.mp4", with_audio=True, duration_seconds=2
    )
    fingerprint = bounded_fingerprint(source)
    source_identity = f"{IDENTITY_VERSION}:{fingerprint}"
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("0"),
        source_end=D("2"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity=source_identity,
        settings_hash="forged-settings",
        tool_version="ffmpeg-test",
    )
    with JobStore(paths.state_dir / "jobs.db") as store:
        job_id = store.create_job({"input_path": str(paths.input_dir)}, {})
        phase1_proxy = paths.cache_dir / "source-1.phase1.proxy.mp4"
        phase1_proxy.parent.mkdir(parents=True, exist_ok=True)
        phase1_proxy.write_bytes(b"phase1 proxy placeholder")
        store.save_artifact(
            job_id,
            "proxy",
            phase1_proxy,
            {
                "kind": "proxy",
                "source_id": "source-1",
                "source_path": str(source),
                "source_identity": source_identity,
                "settings_hash": "phase1-settings",
                "tool_version": "ffmpeg-test",
                "mapping": {
                    "source_id": "source-1",
                    "source_start": "0",
                    "source_end": "2",
                    "proxy_start": "0",
                    "proxy_end": "2",
                    "source_identity": source_identity,
                    "settings_hash": "phase1-settings",
                    "tool_version": "ffmpeg-test",
                },
            },
        )

        with pytest.raises(VideoEditorError, match="upstream mapping"):
            create_cloud_proxy_chunk(
                source,
                paths.cache_dir / job_id,
                mapping,
                D("0"),
                D("1"),
                job_id=job_id,
                source_fingerprint=fingerprint,
                paths=paths,
                store=store,
            )


def test_upload_capable_creation_requires_persisted_phase1_store(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    source = create_media_fixture(
        paths.input_dir / "source.mp4", with_audio=True, duration_seconds=2
    )
    fingerprint = bounded_fingerprint(source)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("0"),
        source_end=D("2"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity=f"{IDENTITY_VERSION}:{fingerprint}",
        settings_hash="phase1-settings",
        tool_version="ffmpeg-test",
    )

    with pytest.raises((TypeError, VideoEditorError), match="store|persisted"):
        create_cloud_proxy_chunk(
            source,
            paths.cache_dir / "job-1",
            mapping,
            D("0"),
            D("1.5"),
            job_id="job-1",
            source_fingerprint=fingerprint,
            paths=paths,
        )


def test_generation_ignores_predictable_partial_links_and_preserves_source(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    source = create_media_fixture(
        paths.input_dir / "source.mp4", with_audio=True, duration_seconds=2
    )
    original = source.read_bytes()
    fingerprint = bounded_fingerprint(source)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("0"),
        source_end=D("2"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity=f"{IDENTITY_VERSION}:{fingerprint}",
        settings_hash="phase1-settings",
        tool_version="ffmpeg-test",
    )
    generated_root = paths.cache_dir / "job-1"
    generated_root.mkdir(parents=True, mode=0o700)
    generated_root.chmod(0o700)
    predictable = generated_root / "predictable.cloud-proxy.mp4.partial"
    predictable.symlink_to(source)
    hardlink_victim = tmp_path / "hardlink-victim.bin"
    hardlink_victim.write_bytes(b"victim bytes")
    predictable_hardlink = generated_root / "predictable-hardlink.partial"
    os.link(hardlink_victim, predictable_hardlink)

    manifest = create_cloud_proxy_chunk(
        source,
        generated_root,
        mapping,
        D("0"),
        D("1.5"),
        job_id="job-1",
        source_fingerprint=fingerprint,
        paths=paths,
        store=PersistedMappingStore(mapping),  # type: ignore[arg-type]
    )

    assert source.read_bytes() == original
    assert hardlink_victim.read_bytes() == b"victim bytes"
    assert predictable.is_symlink()
    assert predictable_hardlink.stat().st_ino == hardlink_victim.stat().st_ino
    assert manifest.path.is_file()
    assert not manifest.path.is_symlink()


def test_generation_rejects_hardlinked_source_without_modifying_it(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    source = paths.input_dir / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source bytes")
    original = source.read_bytes()
    os.link(source, source.with_name("source-copy.mp4"))
    fingerprint = bounded_fingerprint(source)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("0"),
        source_end=D("2"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity=f"{IDENTITY_VERSION}:{fingerprint}",
        settings_hash="phase1-settings",
        tool_version="ffmpeg-test",
    )

    with pytest.raises(VideoEditorError, match="hard links"):
        create_cloud_proxy_chunk(
            source,
            paths.cache_dir / "job-1",
            mapping,
            D("0"),
            D("1"),
            job_id="job-1",
            source_fingerprint=fingerprint,
            paths=paths,
            store=PersistedMappingStore(mapping),  # type: ignore[arg-type]
        )

    assert source.read_bytes() == original


def test_generation_keeps_private_random_temp_open_during_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    source = paths.input_dir / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source bytes")
    fingerprint = bounded_fingerprint(source)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("0"),
        source_end=D("2"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity=f"{IDENTITY_VERSION}:{fingerprint}",
        settings_hash="phase1-settings",
        tool_version="ffmpeg-test",
    )
    captured: dict[str, object] = {}

    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool,
        shell: bool,
        check: bool,
        pass_fds: tuple[int, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["pass_fds"] = pass_fds
        input_path = args[args.index("-i") + 1]
        output_path = args[-1]
        assert input_path.startswith("/dev/fd/")
        assert output_path.startswith("pipe:")
        output_fd = int(output_path.removeprefix("pipe:"))
        assert output_fd in pass_fds
        output_stat = os.fstat(output_fd)
        assert stat.S_ISREG(output_stat.st_mode)
        assert output_stat.st_nlink == 1
        os.write(output_fd, b"generated")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    inspected = MediaProbe(
        path=tmp_path / "generated.mp4",
        duration=1.0,
        video=VideoStream(
            codec_name="h264",
            width=640,
            height=360,
            avg_frame_rate=15.0,
        ),
        audio=AudioStream(codec_name="aac", channels=1),
    )
    monkeypatch.setattr(
        "video_editor.analysis.proxy_chunks._validated_probe",
        lambda path, duration, settings, *, ffprobe: (inspected, 64000),
    )

    manifest = create_cloud_proxy_chunk(
        source,
        paths.cache_dir / "job-1",
        mapping,
        D("0"),
        D("1"),
        job_id="job-1",
        source_fingerprint=fingerprint,
        paths=paths,
        store=PersistedMappingStore(mapping),  # type: ignore[arg-type]
    )

    args = captured["args"]
    pass_fds = captured["pass_fds"]
    assert isinstance(args, list)
    assert isinstance(pass_fds, tuple)
    assert "-n" in args
    assert "-y" not in args
    assert len(pass_fds) == 2
    assert manifest.path.read_bytes() == b"generated"


def test_generation_uses_no_overwrite_for_private_random_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    source = paths.input_dir / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source bytes")
    fingerprint = bounded_fingerprint(source)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("0"),
        source_end=D("2"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity=f"{IDENTITY_VERSION}:{fingerprint}",
        settings_hash="phase1-settings",
        tool_version="ffmpeg-test",
    )
    captured: dict[str, object] = {}

    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool,
        shell: bool,
        check: bool,
        pass_fds: tuple[int, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["pass_fds"] = pass_fds
        output_fd = int(args[-1].removeprefix("pipe:"))
        os.write(output_fd, b"generated")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    inspected = MediaProbe(
        path=tmp_path / "generated.mp4",
        duration=1.0,
        video=VideoStream(
            codec_name="h264",
            width=640,
            height=360,
            avg_frame_rate=15.0,
        ),
        audio=AudioStream(codec_name="aac", channels=1),
    )
    monkeypatch.setattr(
        "video_editor.analysis.proxy_chunks._validated_probe",
        lambda path, duration, settings, *, ffprobe: (inspected, 64000),
    )

    create_cloud_proxy_chunk(
        source,
        paths.cache_dir / "job-1",
        mapping,
        D("0"),
        D("1"),
        job_id="job-1",
        source_fingerprint=fingerprint,
        paths=paths,
        store=PersistedMappingStore(mapping),  # type: ignore[arg-type]
    )

    args = captured["args"]
    assert isinstance(args, list)
    assert "-n" in args
    assert "-y" not in args
    input_index = args.index("-i") + 1
    assert str(args[input_index]).startswith("/dev/fd/")
    assert captured["pass_fds"]
    assert source.read_bytes() == b"source bytes"


def test_generation_does_not_overwrite_existing_final_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    source = paths.input_dir / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source bytes")
    fingerprint = bounded_fingerprint(source)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("0"),
        source_end=D("2"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity=f"{IDENTITY_VERSION}:{fingerprint}",
        settings_hash="phase1-settings",
        tool_version="ffmpeg-test",
    )
    generated_root = paths.cache_dir / "job-1"
    original_link = os.link

    def race_link(
        source_path: object,
        destination_path: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        destination = Path(destination_path)  # type: ignore[arg-type]
        if destination.name.endswith(".cloud-proxy.mp4"):
            destination.write_bytes(b"existing final")
        original_link(source_path, destination_path, *args, **kwargs)

    monkeypatch.setattr(os, "link", race_link)

    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool,
        shell: bool,
        check: bool,
        pass_fds: tuple[int, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        output_fd = int(args[-1].removeprefix("pipe:"))
        os.write(output_fd, b"generated")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    inspected = MediaProbe(
        path=tmp_path / "generated.mp4",
        duration=1.0,
        video=VideoStream(
            codec_name="h264",
            width=640,
            height=360,
            avg_frame_rate=15.0,
        ),
        audio=AudioStream(codec_name="aac", channels=1),
    )
    monkeypatch.setattr(
        "video_editor.analysis.proxy_chunks._validated_probe",
        lambda path, duration, settings, *, ffprobe: (inspected, 64000),
    )

    with pytest.raises(VideoEditorError, match="final proxy path already exists"):
        create_cloud_proxy_chunk(
            source,
            generated_root,
            mapping,
            D("0"),
            D("1"),
            job_id="job-1",
            source_fingerprint=fingerprint,
            paths=paths,
            store=PersistedMappingStore(mapping),  # type: ignore[arg-type]
        )

    final_files = list(generated_root.glob("*.cloud-proxy.mp4"))
    assert len(final_files) == 1
    assert final_files[0].read_bytes() == b"existing final"


def test_generation_does_not_overwrite_existing_random_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    source = paths.input_dir / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source bytes")
    fingerprint = bounded_fingerprint(source)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("0"),
        source_end=D("2"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity=f"{IDENTITY_VERSION}:{fingerprint}",
        settings_hash="phase1-settings",
        tool_version="ffmpeg-test",
    )
    generated_root = paths.cache_dir / "job-1"
    generated_root.mkdir(parents=True, mode=0o700)
    generated_root.chmod(0o700)
    monkeypatch.setattr("secrets.token_hex", lambda size: "fixed-random-name")
    existing = generated_root / ".fixed-random-name.mp4"
    existing.write_bytes(b"do not overwrite")

    with pytest.raises(FileExistsError):
        create_cloud_proxy_chunk(
            source,
            generated_root,
            mapping,
            D("0"),
            D("1"),
            job_id="job-1",
            source_fingerprint=fingerprint,
            paths=paths,
            store=PersistedMappingStore(mapping),  # type: ignore[arg-type]
        )

    assert existing.read_bytes() == b"do not overwrite"
    assert source.read_bytes() == b"source bytes"


def test_generation_requires_job_root_directly_under_configured_generated_root(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    source = paths.input_dir / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source bytes")
    fingerprint = bounded_fingerprint(source)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("0"),
        source_end=D("2"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity=f"{IDENTITY_VERSION}:{fingerprint}",
        settings_hash="phase1-settings",
        tool_version="ffmpeg-test",
    )

    with pytest.raises(VideoEditorError, match="direct child"):
        create_cloud_proxy_chunk(
            source,
            paths.cache_dir / "nested" / "job-1",
            mapping,
            D("0"),
            D("1"),
            job_id="job-1",
            source_fingerprint=fingerprint,
            paths=paths,
            store=PersistedMappingStore(mapping),  # type: ignore[arg-type]
        )


def test_generation_rejects_configured_generated_root_containing_input_root(
    tmp_path: Path,
) -> None:
    paths = PathSettings(
        input_dir=tmp_path / "cache" / "input",
        workspace_dir=tmp_path / "workspace",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
    )
    source = paths.input_dir / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source bytes")
    fingerprint = bounded_fingerprint(source)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("0"),
        source_end=D("2"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity=f"{IDENTITY_VERSION}:{fingerprint}",
        settings_hash="phase1-settings",
        tool_version="ffmpeg-test",
    )

    with pytest.raises(VideoEditorError, match="configured generated root overlaps"):
        create_cloud_proxy_chunk(
            source,
            paths.cache_dir / "job-1",
            mapping,
            D("0"),
            D("1"),
            job_id="job-1",
            source_fingerprint=fingerprint,
            paths=paths,
            store=PersistedMappingStore(mapping),  # type: ignore[arg-type]
        )


def test_generation_rejects_root_overlapping_input_or_source_parent(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    source = paths.input_dir / "nested" / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source bytes")
    fingerprint = bounded_fingerprint(source)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("0"),
        source_end=D("2"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity=f"{IDENTITY_VERSION}:{fingerprint}",
        settings_hash="phase1-settings",
        tool_version="ffmpeg-test",
    )

    invalid_roots = (
        paths.input_dir / "job-1",
        source.parent / "job-1",
        tmp_path / "job-1",
    )
    for generated_root in invalid_roots:
        with pytest.raises(
            VideoEditorError, match="overlaps|configured roots|direct child"
        ):
            create_cloud_proxy_chunk(
                source,
                generated_root,
                mapping,
                D("0"),
                D("1"),
                job_id="job-1",
                source_fingerprint=fingerprint,
                paths=paths,
                store=PersistedMappingStore(mapping),  # type: ignore[arg-type]
            )


@pytest.mark.parametrize(
    "field",
    [
        "job_id",
        "source_id",
        "chunk_id",
        "source_path",
        "source_device",
        "source_inode",
        "source_size_bytes",
        "generated_root_device",
        "generated_root_inode",
        "upstream_settings_hash",
        "upstream_tool_version",
        "media_duration",
        "file_size_bytes",
        "file_digest_sha256",
        "file_device",
        "file_inode",
        "video_codec",
        "audio_channels",
        "audio_probe_bitrate_bps",
    ],
)
def test_proxy_manifest_security_provenance_is_mandatory(field: str) -> None:
    payload = {
        "schema_version": 1,
        "job_id": "job-1",
        "source_id": "source-1",
        "chunk_id": "chunk-1",
        "source_fingerprint": "fingerprint",
        "source_identity": "bounded-v1:fingerprint",
        "source_path": "/input/source.mp4",
        "source_device": 1,
        "source_inode": 1,
        "source_size_bytes": 100,
        "artifact_path": "/cache/job-1/proxy.mp4",
        "generated_root": "/cache/job-1",
        "generated_root_device": 1,
        "generated_root_inode": 2,
        "mapping_version": "mapping-v1",
        "upstream_settings_hash": "settings-hash",
        "upstream_tool_version": "ffmpeg-test",
        "source_start": D("0"),
        "source_end": D("1"),
        "proxy_start": D("0"),
        "proxy_end": D("1"),
        "media_duration": D("1"),
        "file_size_bytes": 100,
        "file_digest_sha256": "digest",
        "file_device": 1,
        "file_inode": 3,
        "video_codec": "h264",
        "video_width": 640,
        "video_height": 360,
        "video_fps": D("15"),
        "audio_codec": "aac",
        "audio_channels": 1,
        "audio_bitrate_bps": 64000,
        "audio_probe_bitrate_bps": 64000,
        "implementation_version": "cloud-proxy-v1",
    }
    payload.pop(field)

    with pytest.raises(ValidationError):
        ProxyManifestData.model_validate(payload)


def test_unregistered_manifest_is_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    with JobStore(paths.state_dir / "jobs.db") as store:
        job_id = store.create_job({"input_path": str(paths.input_dir)}, {})

        with pytest.raises(VideoEditorError, match="unregistered"):
            validate_upload_candidate("missing", job_id, store, paths)


def test_upload_uses_job_scoped_manifest_and_chunk_lookups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        monkeypatch.setattr(
            registered.store,
            "get_proxy_manifest",
            lambda manifest_id: pytest.fail("unscoped manifest lookup used"),
        )
        monkeypatch.setattr(
            registered.store,
            "get_analysis_chunk",
            lambda chunk_id: pytest.fail("unscoped chunk lookup used"),
        )

        with validate_upload_candidate(
            registered.manifest.manifest_id,
            registered.manifest.job_id,
            registered.store,
            registered.paths,
        ) as authorized:
            assert authorized.stream.read(16)
    finally:
        registered.store.__exit__(None, None, None)


def test_upload_validation_requires_expected_job_scope(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        wrong_job = registered.store.create_job(
            {"input_path": str(registered.paths.input_dir)}, {}
        )

        with pytest.raises(VideoEditorError, match="expected job"):
            validate_upload_candidate(
                registered.manifest.manifest_id,
                wrong_job,
                registered.store,
                registered.paths,
            )
    finally:
        registered.store.__exit__(None, None, None)


def test_generation_and_registration_share_media_evidence_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    source = create_media_fixture(
        paths.input_dir / "source.mp4", with_audio=True, duration_seconds=2
    )
    fingerprint = bounded_fingerprint(source)
    mapping = ProxyMapping(
        source_id="source-1",
        source_start=D("0"),
        source_end=D("2"),
        proxy_start=D("0"),
        proxy_end=D("2"),
        source_identity=f"{IDENTITY_VERSION}:{fingerprint}",
        settings_hash="phase1-settings",
        tool_version="ffmpeg-test",
    )
    from video_editor.analysis import proxy_chunks

    actual_parse = proxy_chunks._parse_probe_payload
    calls = 0

    def counting_parse(path: Path, raw: str) -> object:
        nonlocal calls
        calls += 1
        return actual_parse(path, raw)

    monkeypatch.setattr(proxy_chunks, "_parse_probe_payload", counting_parse)
    manifest = create_cloud_proxy_chunk(
        source,
        paths.cache_dir / "job-1",
        mapping,
        D("0"),
        D("1.5"),
        job_id="job-1",
        source_fingerprint=fingerprint,
        paths=paths,
        store=PersistedMappingStore(mapping),  # type: ignore[arg-type]
    )
    assert manifest.data.video_codec == "h264"
    assert calls == 1


def test_registration_rejects_manifest_media_fact_mismatch(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        forged = replace(
            registered.manifest,
            manifest_id=f"{registered.manifest.manifest_id}-forged-media",
            chunk_id=f"{registered.manifest.chunk_id}-forged-media",
            data=registered.manifest.data.model_copy(
                update={
                    "chunk_id": f"{registered.manifest.chunk_id}-forged-media",
                    "video_width": registered.manifest.data.video_width - 2,
                }
            ),
        )

        with pytest.raises(VideoEditorError, match="width"):
            register_proxy_manifest(forged, registered.store)
    finally:
        registered.store.__exit__(None, None, None)


def test_registration_rejects_manifest_file_fact_mismatch(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        forged = replace(
            registered.manifest,
            manifest_id=f"{registered.manifest.manifest_id}-forged-facts",
            chunk_id=f"{registered.manifest.chunk_id}-forged-facts",
            data=registered.manifest.data.model_copy(
                update={
                    "chunk_id": f"{registered.manifest.chunk_id}-forged-facts",
                    "file_digest_sha256": "0" * 64,
                }
            ),
            digest="0" * 64,
            chunk_data=registered.manifest.chunk_data,
        )

        with pytest.raises(VideoEditorError, match="digest"):
            register_proxy_manifest(forged, registered.store)
    finally:
        registered.store.__exit__(None, None, None)


def test_upload_authorization_keeps_validated_descriptor_when_path_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        original_bytes = registered.manifest.path.read_bytes()
        replacement = registered.manifest.path.with_suffix(".replacement.mp4")
        replacement.write_bytes(b"replacement")
        from video_editor.analysis import proxy_chunks

        actual_validate_probe = proxy_chunks._validate_probe

        def validate_then_swap(
            path: Path,
            manifest: ProxyManifestData,
            *,
            ffprobe: str,
            pass_fds: tuple[int, ...] = (),
        ) -> None:
            actual_validate_probe(path, manifest, ffprobe=ffprobe, pass_fds=pass_fds)
            registered.manifest.path.unlink()
            replacement.rename(registered.manifest.path)

        monkeypatch.setattr(proxy_chunks, "_validate_probe", validate_then_swap)

        with validate_upload_candidate(
            registered.manifest.manifest_id,
            registered.manifest.job_id,
            registered.store,
            registered.paths,
        ) as authorized:
            assert authorized.stream.read() == original_bytes
    finally:
        registered.store.__exit__(None, None, None)


def test_upload_authorization_closes_descriptor_on_context_exit(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        authorized = validate_upload_candidate(
            registered.manifest.manifest_id,
            registered.manifest.job_id,
            registered.store,
            registered.paths,
        )
        with authorized:
            assert not authorized.stream.closed
        assert authorized.stream.closed
    finally:
        registered.store.__exit__(None, None, None)


def test_upload_rejects_generated_root_for_another_job(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        wrong_job = registered.store.create_job(
            {"input_path": str(registered.paths.input_dir)}, {}
        )
        wrong_root = registered.paths.cache_dir / wrong_job
        wrong_root.mkdir(mode=0o700)
        moved = wrong_root / registered.manifest.path.name
        registered.manifest.path.rename(moved)
        root_stat = wrong_root.stat(follow_symlinks=False)
        _rewrite_manifest_data(
            registered,
            artifact_path=str(moved),
            generated_root=str(wrong_root),
            generated_root_device=root_stat.st_dev,
            generated_root_inode=root_stat.st_ino,
        )

        with pytest.raises(VideoEditorError, match="expected job"):
            validate_upload_candidate(
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
            )
    finally:
        registered.store.__exit__(None, None, None)


def test_upload_rejects_changed_source_file_identity(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        replacement = registered.source.with_suffix(".replacement.mp4")
        shutil.copyfile(registered.source, replacement)
        registered.source.unlink()
        replacement.rename(registered.source)

        with pytest.raises(VideoEditorError, match="source identity"):
            validate_upload_candidate(
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
            )
    finally:
        registered.store.__exit__(None, None, None)


def test_missing_unrelated_persisted_source_does_not_block_upload(
    tmp_path: Path,
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        missing = registered.paths.input_dir / "deleted-other-source.mp4"
        other_job = registered.store.create_job(
            {"input_path": str(registered.paths.input_dir)}, {}
        )
        registered.store.save_sources(
            other_job,
            [
                {
                    "source_id": "deleted-source",
                    "path": str(missing),
                    "size_bytes": 123,
                    "fingerprint": "a" * 64,
                    "identity_version": IDENTITY_VERSION,
                }
            ],
        )

        with validate_upload_candidate(
            registered.manifest.manifest_id,
            registered.manifest.job_id,
            registered.store,
            registered.paths,
        ) as authorized:
            assert authorized.stream.read(16)
    finally:
        registered.store.__exit__(None, None, None)


def test_missing_cross_job_source_copy_is_globally_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        copied_source = registered.manifest.path.with_name("copied-source.mp4")
        shutil.copyfile(registered.source, copied_source)
        missing = registered.paths.input_dir / "deleted-cross-job-source.mp4"
        other_job = registered.store.create_job(
            {"input_path": str(registered.paths.input_dir)}, {}
        )
        registered.store.save_sources(
            other_job,
            [
                {
                    "source_id": "deleted-source",
                    "path": str(missing),
                    "size_bytes": registered.source.stat().st_size,
                    "fingerprint": bounded_fingerprint(registered.source),
                    "identity_version": IDENTITY_VERSION,
                }
            ],
        )
        _retarget_manifest_file(registered, copied_source)
        monkeypatch.setattr(
            "video_editor.analysis.proxy_chunks._validate_probe",
            lambda path, manifest, *, ffprobe, pass_fds=(): None,
        )

        with pytest.raises(VideoEditorError, match="original source"):
            validate_upload_candidate(
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
            )
    finally:
        registered.store.__exit__(None, None, None)


def test_malformed_persisted_source_identity_fails_closed(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        other_job = registered.store.create_job(
            {"input_path": str(registered.paths.input_dir)}, {}
        )
        registered.store.save_sources(
            other_job,
            [
                {
                    "source_id": "malformed-source",
                    "path": str(registered.paths.input_dir / "missing.mp4"),
                    "size_bytes": 123,
                    "identity_version": IDENTITY_VERSION,
                }
            ],
        )

        with pytest.raises(VideoEditorError, match="persisted source identity"):
            validate_upload_candidate(
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
            )
    finally:
        registered.store.__exit__(None, None, None)


def test_missing_persisted_source_with_malformed_bounded_fingerprint_fails_closed(
    tmp_path: Path,
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        other_job = registered.store.create_job(
            {"input_path": str(registered.paths.input_dir)}, {}
        )
        registered.store.save_sources(
            other_job,
            [
                {
                    "source_id": "malformed-source",
                    "path": str(registered.paths.input_dir / "missing.mp4"),
                    "size_bytes": 123,
                    "fingerprint": "not-a-valid-bounded-fingerprint",
                    "identity_version": IDENTITY_VERSION,
                }
            ],
        )

        with pytest.raises(VideoEditorError, match="persisted source identity"):
            validate_upload_candidate(
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
            )
    finally:
        registered.store.__exit__(None, None, None)


def test_persisted_source_wrapper_and_data_ids_must_match(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        row = registered.store.connection.execute(
            "SELECT id, data_json FROM sources WHERE job_id = ? AND source_id = ?",
            (registered.manifest.job_id, registered.manifest.source_id),
        ).fetchone()
        assert row is not None
        data = json.loads(row["data_json"])
        data["source_id"] = "different-source"
        registered.store.connection.execute(
            "UPDATE sources SET data_json = ? WHERE id = ?",
            (json.dumps(data, sort_keys=True, separators=(",", ":")), row["id"]),
        )
        registered.store.connection.commit()

        with pytest.raises(VideoEditorError, match="persisted source identity"):
            validate_upload_candidate(
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
            )
    finally:
        registered.store.__exit__(None, None, None)


def test_cross_job_source_alias_is_globally_protected(tmp_path: Path) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        other_job = registered.store.create_job(
            {"input_path": str(registered.paths.input_dir)}, {}
        )
        registered.store.save_sources(
            other_job,
            [
                {
                    "source_id": "other-source",
                    "path": str(registered.manifest.path),
                    "size_bytes": registered.manifest.path.stat().st_size,
                    "fingerprint": bounded_fingerprint(registered.manifest.path),
                    "identity_version": IDENTITY_VERSION,
                }
            ],
        )

        with pytest.raises(VideoEditorError, match="original source"):
            validate_upload_candidate(
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
            )
    finally:
        registered.store.__exit__(None, None, None)


def test_cross_job_source_hardlink_identity_is_globally_protected(
    tmp_path: Path,
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        alias = registered.paths.input_dir / "cross-job-alias.mp4"
        os.link(registered.manifest.path, alias)
        other_job = registered.store.create_job(
            {"input_path": str(registered.paths.input_dir)}, {}
        )
        registered.store.save_sources(
            other_job,
            [
                {
                    "source_id": "other-source",
                    "path": str(alias),
                    "size_bytes": alias.stat().st_size,
                    "fingerprint": bounded_fingerprint(alias),
                    "identity_version": IDENTITY_VERSION,
                }
            ],
        )
        file_stat = registered.manifest.path.stat()
        _rewrite_manifest_data(
            registered,
            file_device=file_stat.st_dev,
            file_inode=file_stat.st_ino,
        )

        with pytest.raises(VideoEditorError, match="original source"):
            validate_upload_candidate(
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_identity", "bounded-v1:wrong"),
        ("settings_hash", "wrong-settings"),
        ("tool_version", "wrong-tool"),
        ("source_start", "0.5"),
        ("source_end", "1.5"),
        ("proxy_start", "0.5"),
        ("proxy_end", "2.5"),
    ],
)
def test_upload_rejects_persisted_phase1_mapping_mismatch(
    tmp_path: Path, field: str, value: object
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        _rewrite_phase1_mapping(registered, **{field: value})

        with pytest.raises(VideoEditorError, match="upstream mapping"):
            validate_upload_candidate(
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("video_args", "audio_args", "message"),
    [
        (("-c:v", "libx264"), ("-c:a", "aac", "-ac", "1", "-b:a", "64k"), "width|FPS"),
        (("-c:v", "libx264"), ("-an",), "stream count"),
        (("-c:v", "mpeg4"), ("-c:a", "aac", "-ac", "1", "-b:a", "64k"), "H.264"),
        (("-c:v", "libx264"), ("-c:a", "mp3", "-ac", "1", "-b:a", "64k"), "AAC"),
        (("-c:v", "libx264"), ("-c:a", "aac", "-ac", "2", "-b:a", "64k"), "mono"),
        (("-c:v", "libx264"), ("-c:a", "aac", "-ac", "1", "-b:a", "128k"), "64 kbps"),
    ],
)
def test_registration_rejects_forged_generated_media_profile(
    tmp_path: Path,
    video_args: tuple[str, ...],
    audio_args: tuple[str, ...],
    message: str,
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        forged = registered.manifest.path.with_name("forged.mp4")
        video_source = (
            "color=c=black:s=800x450:r=30"
            if message == "width|FPS"
            else "color=c=black:s=640x360:r=15"
        )
        args = [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            video_source,
            "-f",
            "lavfi",
            "-i",
            (
                "sine=frequency=1000:sample_rate=48000"
                if message == "64 kbps"
                else "anullsrc=r=16000:cl=stereo"
            ),
            "-t",
            "10" if message == "64 kbps" else "1.5",
            *video_args,
            *audio_args,
            "-shortest",
            str(forged),
        ]
        if audio_args == ("-an",):
            args = [value for value in args if value not in ("-shortest",)]
        completed = subprocess.run(args, check=False, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stderr
        file_stat = forged.stat()
        forged_data = registered.manifest.data.model_copy(
            update={
                "artifact_path": str(forged),
                "file_size_bytes": file_stat.st_size,
                "file_digest_sha256": hashlib.sha256(forged.read_bytes()).hexdigest(),
                "file_device": file_stat.st_dev,
                "file_inode": file_stat.st_ino,
            }
        )
        forged_manifest = replace(
            registered.manifest,
            path=forged,
            digest=forged_data.file_digest_sha256,
            data=forged_data,
        )

        with pytest.raises(VideoEditorError, match=message):
            register_proxy_manifest(forged_manifest, registered.store)
    finally:
        registered.store.__exit__(None, None, None)


def test_public_creation_rejects_executable_wrapper_bitrate_bypass(
    tmp_path: Path,
) -> None:
    generated = _registered_chunk(tmp_path, register=False)
    try:
        original = generated.manifest
        original.path.unlink()
        mapping = ProxyMapping(
            source_id=original.source_id,
            source_start=D("0"),
            source_end=D("2"),
            proxy_start=D("0"),
            proxy_end=D("2"),
            source_identity=original.data.source_identity,
            settings_hash=original.data.upstream_settings_hash,
            tool_version=original.data.upstream_tool_version,
        )
        real_ffmpeg = shutil.which("ffmpeg")
        assert real_ffmpeg is not None
        wrapper = tmp_path / "ffmpeg-wrapper"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import os\n"
            "import sys\n"
            f"executable = {real_ffmpeg!r}\n"
            'args = ["32k" if value == "64k" else value for value in sys.argv[1:]]\n'
            "os.execv(executable, [executable, *args])\n"
        )
        wrapper.chmod(0o700)

        with pytest.raises(TypeError, match="ffmpeg|unexpected keyword"):
            manifest = create_cloud_proxy_chunk(
                generated.source,
                generated.paths.cache_dir / original.job_id,
                mapping,
                D("0"),
                D("1.5"),
                job_id=original.job_id,
                source_fingerprint=original.data.source_fingerprint,
                paths=generated.paths,
                store=generated.store,
                ffmpeg=str(wrapper),
            )
            assert manifest.data.audio_probe_bitrate_bps < 60_000
            register_proxy_manifest(manifest, generated.store)
    finally:
        generated.store.__exit__(None, None, None)


def test_generation_proof_is_consumed_after_successful_registration(
    tmp_path: Path,
) -> None:
    generated = _registered_chunk(tmp_path, register=False)
    try:
        register_proxy_manifest(generated.manifest, generated.store)

        with pytest.raises(
            VideoEditorError, match="trusted generation|consumed|replay"
        ):
            register_proxy_manifest(generated.manifest, generated.store)
    finally:
        generated.store.__exit__(None, None, None)


def test_equal_manifest_object_cannot_reuse_generation_proof(tmp_path: Path) -> None:
    generated = _registered_chunk(tmp_path, register=False)
    try:
        equal_manifest = replace(generated.manifest)
        assert equal_manifest == generated.manifest
        assert equal_manifest is not generated.manifest

        with pytest.raises(VideoEditorError, match="trusted generation"):
            register_proxy_manifest(equal_manifest, generated.store)
    finally:
        generated.store.__exit__(None, None, None)


def test_registration_rejects_caller_forged_32kbps_media(
    tmp_path: Path,
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        forged = registered.manifest.path.with_name("forged-32kbps.mp4")
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                (
                    "color=c=black:s="
                    f"{registered.manifest.data.video_width}x"
                    f"{registered.manifest.data.video_height}:r=15"
                ),
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:sample_rate=16000",
                "-t",
                "1.5",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-ac",
                "1",
                "-b:a",
                "32k",
                "-shortest",
                str(forged),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=bit_rate",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(forged),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        observed_bitrate = int(probe.stdout.strip())
        assert observed_bitrate < 60_000
        file_stat = forged.stat()
        digest = hashlib.sha256(forged.read_bytes()).hexdigest()
        forged_data = registered.manifest.data.model_copy(
            update={
                "artifact_path": str(forged),
                "file_size_bytes": file_stat.st_size,
                "file_digest_sha256": digest,
                "file_device": file_stat.st_dev,
                "file_inode": file_stat.st_ino,
                "audio_bitrate_bps": 64_000,
                "audio_probe_bitrate_bps": observed_bitrate,
            }
        )
        forged_manifest = replace(
            registered.manifest,
            path=forged,
            digest=digest,
            data=forged_data,
        )

        with pytest.raises(VideoEditorError, match="trusted generation"):
            register_proxy_manifest(forged_manifest, registered.store)
    finally:
        registered.store.__exit__(None, None, None)


def test_registration_rejects_generated_media_with_extra_subtitle_stream(
    tmp_path: Path,
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        forged = registered.manifest.path.with_name("extra-subtitle.mp4")
        subtitle = tmp_path / "extra.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nextra\n")
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(registered.manifest.path),
                "-i",
                str(subtitle),
                "-map",
                "0:v",
                "-map",
                "0:a",
                "-map",
                "1:s",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-c:s",
                "mov_text",
                str(forged),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        file_stat = forged.stat()
        forged_data = registered.manifest.data.model_copy(
            update={
                "artifact_path": str(forged),
                "file_size_bytes": file_stat.st_size,
                "file_digest_sha256": hashlib.sha256(forged.read_bytes()).hexdigest(),
                "file_device": file_stat.st_dev,
                "file_inode": file_stat.st_ino,
            }
        )
        forged_manifest = replace(
            registered.manifest,
            path=forged,
            digest=forged_data.file_digest_sha256,
            data=forged_data,
        )

        with pytest.raises(VideoEditorError, match="stream count"):
            register_proxy_manifest(forged_manifest, registered.store)
    finally:
        registered.store.__exit__(None, None, None)


def test_registration_rejects_generated_media_with_two_video_streams(
    tmp_path: Path,
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        forged = registered.manifest.path.with_name("two-video.mp4")
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=640x360:r=15",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=640x360:r=15",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=16000:cl=mono",
                "-t",
                "1.5",
                "-map",
                "0:v",
                "-map",
                "1:v",
                "-map",
                "2:a",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-ac",
                "1",
                "-b:a",
                "64k",
                "-shortest",
                str(forged),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        file_stat = forged.stat()
        forged_data = registered.manifest.data.model_copy(
            update={
                "artifact_path": str(forged),
                "file_size_bytes": file_stat.st_size,
                "file_digest_sha256": hashlib.sha256(forged.read_bytes()).hexdigest(),
                "file_device": file_stat.st_dev,
                "file_inode": file_stat.st_ino,
            }
        )
        forged_manifest = replace(
            registered.manifest,
            path=forged,
            digest=forged_data.file_digest_sha256,
            data=forged_data,
        )

        with pytest.raises(VideoEditorError, match="stream count"):
            register_proxy_manifest(forged_manifest, registered.store)
    finally:
        registered.store.__exit__(None, None, None)


def test_upload_rejects_manifest_range_outside_persisted_phase1_mapping(
    tmp_path: Path,
) -> None:
    registered = _registered_chunk(tmp_path)
    try:
        _rewrite_manifest_data(
            registered,
            source_start="1.5",
            source_end="2.5",
            proxy_start="0",
            proxy_end="1",
        )
        _rewrite_chunk_data(
            registered,
            source_start="1.5",
            source_end="2.5",
            proxy_start="0",
            proxy_end="1",
        )
        registered.store.connection.execute(
            "UPDATE analysis_chunks SET source_start = ?, source_end = ? WHERE chunk_id = ?",
            ("1.5", "2.5", registered.manifest.chunk_id),
        )
        registered.store.connection.commit()

        with pytest.raises(VideoEditorError, match="upstream mapping"):
            validate_upload_candidate(
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
            )
    finally:
        registered.store.__exit__(None, None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="local FFmpeg and ffprobe required",
)
def test_registration_rolls_back_manifest_when_chunk_save_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = _registered_chunk(tmp_path, register=False)
    try:
        manifest = registered.manifest
        actual_save = registered.store.save_analysis_chunk
        calls = 0

        def fail_once(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("chunk write failed")
            actual_save(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(registered.store, "save_analysis_chunk", fail_once)

        with pytest.raises(RuntimeError, match="chunk write failed"):
            register_proxy_manifest(manifest, registered.store)

        assert registered.store.get_proxy_manifest(manifest.manifest_id) is None
        register_proxy_manifest(manifest, registered.store)
        assert registered.store.get_proxy_manifest(manifest.manifest_id) is not None
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
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
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
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
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
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
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
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
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
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
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
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
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
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
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
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
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
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
            )

        assert caught.value.code == "unsafe_upload_candidate"
        assert "source" in str(caught.value)
    finally:
        registered.store.__exit__(None, None, None)
