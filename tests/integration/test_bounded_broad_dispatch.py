"""Bounded dispatch integration against synthetic media and an offline provider."""

from __future__ import annotations

import hashlib
import json
import subprocess
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from video_editor import bounded_broad
from video_editor.analysis.models import (
    AnalysisRequestContext,
    BroadScanResponse,
    ProviderAttemptUsage,
    ProviderResult,
    ProviderUsage,
    UploadedFile,
)
from video_editor.analysis.proxy_chunks import AuthorizedUpload
from video_editor.cli import app
from video_editor.config import AppConfig, PathSettings
from video_editor.errors import VideoEditorError
from video_editor.persistence.database import JobStore

D = Decimal


class OfflineProvider:
    def __init__(self, *, unknown: bool = False) -> None:
        self.uploads = 0
        self.scans = 0
        self.deleted: list[str] = []
        self.unknown = unknown

    def upload(self, authorization: AuthorizedUpload) -> UploadedFile:
        self.uploads += 1
        with authorization:
            assert authorization.stream.read(8)
        return UploadedFile(
            name="files/offline",
            uri="gs://offline/proxy",
            mime_type="video/mp4",
            state="ACTIVE",
            manifest_id=authorization.manifest_id,
        )

    def broad_scan(
        self,
        upload: UploadedFile,
        chunk: object,
        request: AnalysisRequestContext,
        *,
        prompt_version: str,
    ) -> ProviderResult[BroadScanResponse]:
        self.scans += 1
        assert prompt_version == "broad-v4"
        return ProviderResult(
            response=BroadScanResponse(
                schema_version="broad-v1",
                chunk_id=request.chunk_id,
                scenes=(),
                speech_presence_ranges=(),
                candidates=(),
            ),
            usage=ProviderUsage(
                reservation_id=request.reservation_id,
                attempts=(
                    ProviderAttemptUsage(
                        request_id="offline",
                        status="failed_unknown_billing"
                        if self.unknown
                        else "succeeded",
                        prompt_tokens=None if self.unknown else 0,
                        media_input_tokens=None if self.unknown else 0,
                        text_input_tokens=None if self.unknown else 0,
                        candidates_tokens=None if self.unknown else 0,
                        thoughts_tokens=None if self.unknown else 0,
                        output_tokens=None if self.unknown else 0,
                        total_tokens=None if self.unknown else 0,
                    ),
                ),
                request_ids=("offline",),
                prompt_tokens=0,
                media_input_tokens=0,
                text_input_tokens=0,
                candidates_tokens=0,
                thoughts_tokens=0,
                output_tokens=0,
                total_tokens=0,
                has_unknown_billing=self.unknown,
                actual_cost_usd=None if self.unknown else D("0.001"),
            ),
        )

    def delete_upload(self, upload: UploadedFile) -> None:
        self.deleted.append(upload.name)


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, AppConfig]:
    paths = PathSettings(
        *(
            tmp_path / name
            for name in ("input", "workspace", "cache", "output", "state")
        )
    )
    paths.input_dir.mkdir()
    source = paths.input_dir / "GX011485.MP4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=640x360:r=30:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    return source, AppConfig(paths=paths, storage_reserve_bytes=0)


@pytest.mark.parametrize("unknown", [False, True])
def test_one_broad_dispatch_preserves_source_and_blocks_replay(
    setup: tuple[Path, AppConfig],
    monkeypatch: pytest.MonkeyPatch,
    unknown: bool,
) -> None:
    source, config = setup
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    provider = OfflineProvider(unknown=unknown)
    factory = Mock(return_value=provider)
    monkeypatch.setattr(bounded_broad.GeminiAdapter, "from_environment", factory)
    result = bounded_broad.run_bounded_broad(
        source=source,
        start=D(1),
        end=D(3),
        attempt_id="offline-once",
        config=config,
        dispatch=True,
    )
    assert result["state"] == "complete"
    assert result["broad_requests"] == 1
    assert result["candidate_refinement"] is False
    assert result["render"] is False
    assert provider.uploads == provider.scans == 1
    assert provider.deleted == ["files/offline"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    claim = json.loads(
        (
            config.paths.state_dir
            / "bounded-attempts"
            / "offline-once"
            / "claimed.json"
        ).read_text()
    )
    assert claim["job_id"] == result["job_id"]
    with JobStore(config.paths.state_dir / "jobs.sqlite3") as store:
        job = store.get_job(result["job_id"])
        assert len(job["sources"]) == 1
        rows = store.connection.execute(
            "SELECT * FROM analysis_requests WHERE job_id = ?", (result["job_id"],)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == ("billing_unknown" if unknown else "completed")
        budget = store.budget_state(result["job_id"])
        assert budget.reserved_usd == (
            D(result["request_maximum_usd"]) if unknown else D(0)
        )
        assert budget.spent_usd == (D(0) if unknown else D("0.001"))
    with pytest.raises(VideoEditorError, match="refusing replay"):
        bounded_broad.run_bounded_broad(
            source=source,
            start=D(1),
            end=D(3),
            attempt_id="offline-once",
            config=config,
            dispatch=True,
        )
    assert provider.uploads == provider.scans == 1
    assert factory.call_count == 1


def test_out_of_source_bounds_never_constructs_provider(
    setup: tuple[Path, AppConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, config = setup
    factory = Mock(side_effect=AssertionError("no provider allowed"))
    monkeypatch.setattr(bounded_broad.GeminiAdapter, "from_environment", factory)
    with pytest.raises(VideoEditorError, match="exceeds source duration"):
        bounded_broad.run_bounded_broad(
            source=source,
            start=D(0),
            end=D(10),
            attempt_id="invalid-range",
            config=config,
            dispatch=True,
        )
    factory.assert_not_called()


def test_final_proxy_cost_recheck_precedes_provider_construction(
    setup: tuple[Path, AppConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, config = setup
    estimate = Mock(side_effect=[D("0.1"), D("0.262052")])
    factory = Mock(side_effect=AssertionError("no provider allowed"))
    monkeypatch.setattr(bounded_broad, "_request_maximum", estimate)
    monkeypatch.setattr(bounded_broad.GeminiAdapter, "from_environment", factory)
    with pytest.raises(VideoEditorError, match="final digest-bound"):
        bounded_broad.run_bounded_broad(
            source=source,
            start=D(1),
            end=D(3),
            attempt_id="final-cap",
            config=config,
            dispatch=True,
        )
    assert estimate.call_count == 2
    factory.assert_not_called()


def test_cli_dry_run_no_source_or_credentials_or_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = PathSettings(
        *(
            tmp_path / name
            for name in ("input", "workspace", "cache", "output", "state")
        )
    )
    config = AppConfig(paths=paths, storage_reserve_bytes=0)
    config_file = tmp_path / "config.toml"
    config_file.write_text("")
    monkeypatch.setattr("video_editor.cli.resolve_config", lambda _: config)
    factory = Mock(side_effect=AssertionError("no provider allowed"))
    monkeypatch.setattr(bounded_broad.GeminiAdapter, "from_environment", factory)
    result = CliRunner().invoke(
        app,
        [
            "bounded-broad",
            str(paths.input_dir / "missing.MP4"),
            "--config",
            str(config_file),
            "--start",
            "0",
            "--end",
            "720",
            "--attempt-id",
            "nominal",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "0.262051" in result.output
    factory.assert_not_called()
    assert not paths.state_dir.exists()
