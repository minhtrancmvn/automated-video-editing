"""Offline filesystem compatibility without weakening POSIX checks."""

import os
import stat
from itertools import count
from pathlib import Path

import pytest
import test_proxy_chunks
from test_proxy_chunks import _registered_chunk

from video_editor.analysis import proxy_chunks
from video_editor.errors import VideoEditorError
from video_editor.media.storage import VolumeIdentity


def _synthetic_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    actual_open = os.open

    def opened(path: object, flags: int, mode: int = 0o777, **kwargs: object) -> int:
        fd = actual_open(path, flags, mode, **kwargs)
        if flags & os.O_CREAT and mode == 0o600:
            os.fchmod(fd, 0o700)
        return fd

    monkeypatch.setattr(proxy_chunks.os, "open", opened)


def _volume(monkeypatch: pytest.MonkeyPatch, filesystem: str) -> None:
    monkeypatch.setattr(
        proxy_chunks,
        "inspect_volume",
        lambda path: VolumeIdentity(path.stat().st_dev, path, filesystem),
        raising=False,
    )


def test_exfat_synthetic_modes_generate_register_and_authorize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _volume(monkeypatch, "exfat")
    _synthetic_modes(monkeypatch)
    registered = _registered_chunk(tmp_path)
    try:
        with proxy_chunks.validate_upload_candidate(
            registered.manifest.manifest_id,
            registered.manifest.job_id,
            registered.store,
            registered.paths,
        ) as upload:
            assert upload.stream.read() == registered.manifest.path.read_bytes()
        assert stat.S_IMODE(registered.manifest.path.stat().st_mode) == 0o700
    finally:
        registered.store.__exit__(None, None, None)


def test_posix_still_rejects_nonprivate_temporary_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _volume(monkeypatch, "apfs")
    _synthetic_modes(monkeypatch)
    with pytest.raises(VideoEditorError, match="temporary proxy reservation is unsafe"):
        _registered_chunk(tmp_path)


def test_exfat_publication_does_not_use_hardlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _volume(monkeypatch, "exfat")
    monkeypatch.setattr(
        proxy_chunks.os,
        "link",
        lambda *_args, **_kwargs: pytest.fail("exFAT cannot hardlink"),
    )
    registered = _registered_chunk(tmp_path)
    try:
        assert registered.manifest.path.is_file()
        assert not list(registered.manifest.path.parent.glob(".*.mp4"))
    finally:
        registered.store.__exit__(None, None, None)


def test_exfat_failed_generation_preserves_replacement_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _volume(monkeypatch, "exfat")
    replacement_paths: list[Path] = []

    def replace_then_fail(*_args: object, **_kwargs: object) -> None:
        output = next((tmp_path / "cache").rglob("*.cloud-proxy.mp4"))
        output.unlink()
        output.write_bytes(b"unrelated replacement")
        replacement_paths.append(output)
        raise RuntimeError("interrupted generation")

    monkeypatch.setattr(proxy_chunks, "_run_ffmpeg", replace_then_fail)
    with pytest.raises(RuntimeError, match="interrupted generation"):
        _registered_chunk(tmp_path)

    assert replacement_paths[0].read_bytes() == b"unrelated replacement"
    assert replacement_paths[0].exists()


def test_exfat_failed_ffmpeg_output_does_not_block_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _volume(monkeypatch, "exfat")
    tokens = count()
    monkeypatch.setattr(
        proxy_chunks.secrets, "token_hex", lambda _bytes: f"{next(tokens):048x}"
    )
    original_run = proxy_chunks._run_ffmpeg
    original_create = proxy_chunks.create_cloud_proxy_chunk
    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []
    attempts = 0

    def capture_create(*args: object, **kwargs: object) -> object:
        captured.append((args, kwargs))
        return original_create(*args, **kwargs)

    def fail_once(*args: object, **kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise VideoEditorError(
                proxy_chunks.ErrorCategory.RENDER, "synthetic FFmpeg failure"
            )
        original_run(*args, **kwargs)

    monkeypatch.setattr(proxy_chunks, "_run_ffmpeg", fail_once)
    monkeypatch.setattr(proxy_chunks, "create_cloud_proxy_chunk", capture_create)
    monkeypatch.setattr(test_proxy_chunks, "create_cloud_proxy_chunk", capture_create)
    with pytest.raises(VideoEditorError, match="synthetic FFmpeg failure"):
        _registered_chunk(tmp_path)

    original_args, original_kwargs = captured[0]
    retry = original_create(*original_args, **original_kwargs)
    try:
        assert retry.path.is_file()
        assert retry.path.read_bytes()
    finally:
        original_kwargs["store"].__exit__(None, None, None)


def test_exfat_failed_validation_output_does_not_block_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _volume(monkeypatch, "exfat")
    tokens = count()
    monkeypatch.setattr(
        proxy_chunks.secrets, "token_hex", lambda _bytes: f"{next(tokens):048x}"
    )
    original_validate = proxy_chunks._validated_probe
    original_create = proxy_chunks.create_cloud_proxy_chunk
    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []
    attempts = 0

    def capture_create(*args: object, **kwargs: object) -> object:
        captured.append((args, kwargs))
        return original_create(*args, **kwargs)

    def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise VideoEditorError(
                proxy_chunks.ErrorCategory.OUTPUT, "synthetic validation failure"
            )
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(proxy_chunks, "_validated_probe", fail_once)
    monkeypatch.setattr(proxy_chunks, "create_cloud_proxy_chunk", capture_create)
    monkeypatch.setattr(test_proxy_chunks, "create_cloud_proxy_chunk", capture_create)
    with pytest.raises(VideoEditorError, match="synthetic validation failure"):
        _registered_chunk(tmp_path)

    original_args, original_kwargs = captured[0]
    retry = original_create(*original_args, **original_kwargs)
    try:
        assert retry.path.is_file()
    finally:
        original_kwargs["store"].__exit__(None, None, None)


def test_exfat_existing_output_collision_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _volume(monkeypatch, "exfat")
    actual_open = os.open
    collision_paths: list[Path] = []
    ffmpeg_called = False

    def collide_once(
        path: object, flags: int, mode: int = 0o777, **kwargs: object
    ) -> int:
        candidate = Path(path)  # type: ignore[arg-type]
        if candidate.name.endswith(".cloud-proxy.mp4") and flags & os.O_EXCL:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_bytes(b"unrelated existing output")
            collision_paths.append(candidate)
        return actual_open(path, flags, mode, **kwargs)

    def record_output(*_args: object, **_kwargs: object) -> None:
        nonlocal ffmpeg_called
        ffmpeg_called = True

    monkeypatch.setattr(proxy_chunks.os, "open", collide_once)
    monkeypatch.setattr(proxy_chunks, "_run_ffmpeg", record_output)
    with pytest.raises(FileExistsError):
        _registered_chunk(tmp_path)

    assert len(collision_paths) == 1
    assert collision_paths[0].read_bytes() == b"unrelated existing output"
    assert not ffmpeg_called


def test_generation_rejects_final_replaced_before_manifest_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _volume(monkeypatch, "exfat")
    actual_hash = proxy_chunks._sha256

    def swap_before_hash(path: Path) -> str:
        if str(path).startswith("/dev/fd/"):
            final = next((tmp_path / "cache").rglob("*.cloud-proxy.mp4"))
            replacement = final.with_suffix(".replacement")
            replacement.write_bytes(final.read_bytes())
            replacement.replace(final)
        return actual_hash(path)

    monkeypatch.setattr(proxy_chunks, "_sha256", swap_before_hash)
    with pytest.raises(VideoEditorError, match="identity changed"):
        _registered_chunk(tmp_path, register=False)


def test_exfat_still_rejects_replaced_registered_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _volume(monkeypatch, "exfat")
    _synthetic_modes(monkeypatch)
    registered = _registered_chunk(tmp_path)
    try:
        registered.manifest.path.write_bytes(b"tampered")
        with pytest.raises(VideoEditorError, match="digest"):
            proxy_chunks.validate_upload_candidate(
                registered.manifest.manifest_id,
                registered.manifest.job_id,
                registered.store,
                registered.paths,
            )
    finally:
        registered.store.__exit__(None, None, None)
