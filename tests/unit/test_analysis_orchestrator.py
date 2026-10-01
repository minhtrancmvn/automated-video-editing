"""Offline contract tests for analysis orchestration and coverage gates."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest

from video_editor.analysis.models import (
    AnalysisBoundaryKind,
    AnalysisChunkData,
    AnalysisRequestContext,
    AnalysisResultData,
    BroadScanResponse,
    CandidateRefinementResponse,
    CandidateWindow,
    LocalSegmentation,
    ProviderAttemptUsage,
    ProviderResult,
    ProviderUsage,
    ProxyManifestData,
    RequestReservation,
    SourceRange,
    UploadedFile,
)
from video_editor.analysis.orchestrator import (
    AnalysisCacheIdentity,
    AnalysisOutcome,
    BroadAnalysisRequest,
    CandidateAnalysisRequest,
    analysis_cache_key,
    coverage_report,
    run_broad_analysis,
    run_candidate_refinement,
)
from video_editor.analysis.proxy_chunks import AuthorizedUpload
from video_editor.config import PathSettings
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.persistence.database import JobStore

D = Decimal


@dataclass
class FakeProvider:
    """Record orchestration calls and return strict offline responses."""

    store: JobStore
    job_id: str
    events: list[str] = field(default_factory=list)
    broad_calls: int = 0
    candidate_calls: int = 0
    candidate_fps: list[int] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unknown_billing: bool = False
    candidate_unknown_billing: bool = False

    def upload(self, authorization: AuthorizedUpload) -> UploadedFile:
        self.events.append("upload")
        rows = self.store.connection.execute(
            "SELECT status FROM analysis_requests WHERE job_id = ? ORDER BY request_id",
            (self.job_id,),
        ).fetchall()
        assert rows
        if self.broad_calls == 0 and self.candidate_calls == 0:
            assert all(row["status"] == "reserved" for row in rows)
        authorization.close()
        return UploadedFile(
            name=f"files/{authorization.manifest_id}",
            uri="gs://offline/upload",
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
        del upload, prompt_version
        self.events.append("broad")
        self.broad_calls += 1
        chunk_id = request.chunk_id
        start = D(str(chunk.proxy_start))
        end = D(str(chunk.proxy_end))
        return ProviderResult(
            response=BroadScanResponse.model_validate(
                {
                    "schema_version": "broad-v1",
                    "chunk_id": chunk_id,
                    "scenes": [],
                    "speech_presence_ranges": [],
                    "candidates": [
                        {
                            "candidate_id": f"candidate-{chunk_id}",
                            "start": format(start, "f"),
                            "end": format(end, "f"),
                            "category": "action",
                            "reason": "offline fixture",
                            "confidence": 0.9,
                        }
                    ],
                }
            ),
            usage=_usage(request.reservation_id, self.unknown_billing),
        )

    def refine_candidate(
        self,
        upload: UploadedFile,
        candidate: CandidateWindow,
        request: AnalysisRequestContext,
        fps: int,
        *,
        prompt_version: str,
    ) -> ProviderResult[CandidateRefinementResponse]:
        del upload, prompt_version
        self.events.append("candidate")
        self.candidate_calls += 1
        self.candidate_fps.append(fps)
        return ProviderResult(
            response=CandidateRefinementResponse.model_validate(
                {
                    "schema_version": "candidate-v1",
                    "chunk_id": candidate.chunk_id,
                    "candidate_id": candidate.candidate_id,
                    "start": format(candidate.start, "f"),
                    "end": format(candidate.end, "f"),
                    "action_completeness": 0.8,
                    "visual_composition": 0.8,
                    "novelty": 0.8,
                    "semantic_importance": 0.8,
                    "duplicate_similarity": 0.1,
                    "vertical_subject_priority": "center",
                    "crop_intent": "follow subject",
                    "adjacent_scene_compatibility": 0.7,
                    "speech_meaning_summary": "bounded meaning",
                    "confidence": 0.9,
                }
            ),
            usage=_usage(request.reservation_id, self.candidate_unknown_billing),
        )

    def delete_upload(self, upload: UploadedFile) -> None:
        self.events.append("delete")
        self.deleted.append(upload.name)


def _usage(reservation_id: str, unknown: bool) -> ProviderUsage:
    status: Literal["succeeded", "failed_unknown_billing"] = (
        "failed_unknown_billing" if unknown else "succeeded"
    )
    return ProviderUsage(
        reservation_id=reservation_id,
        attempts=(
            ProviderAttemptUsage(
                request_id=f"provider-{reservation_id}",
                status=status,
                prompt_tokens=None if unknown else 0,
                media_input_tokens=None if unknown else 0,
                text_input_tokens=None if unknown else 0,
                candidates_tokens=None if unknown else 0,
                thoughts_tokens=None if unknown else 0,
                output_tokens=None if unknown else 0,
                total_tokens=None if unknown else 0,
            ),
        ),
        request_ids=(f"provider-{reservation_id}",),
        prompt_tokens=0,
        media_input_tokens=0,
        text_input_tokens=0,
        candidates_tokens=0,
        thoughts_tokens=0,
        output_tokens=0,
        total_tokens=0,
        has_unknown_billing=unknown,
        actual_cost_usd=D(0),
    )


def _identity() -> AnalysisCacheIdentity:
    return AnalysisCacheIdentity(
        source_identity="identity-v1:abc",
        source_fingerprint="fingerprint",
        proxy_digest="digest",
        mapping_version="mapping-v1",
        source_start=D("1.20"),
        source_end=D("4.50"),
        mode="broad",
        fps=D("0.5"),
        media_width=640,
        media_height=360,
        provider="gemini",
        model="gemini-2.5-flash",
        prompt_version="broad-v1",
        response_schema_version="broad-v1",
        implementation_version="analysis-v1",
    )


def _manifest(job_id: str, chunk_id: str, source_start: Decimal) -> ProxyManifestData:
    return ProxyManifestData(
        schema_version=1,
        job_id=job_id,
        source_id="source-1",
        chunk_id=chunk_id,
        source_fingerprint="fingerprint",
        source_identity="identity-v1:fingerprint",
        source_path="/input/source.mp4",
        source_device=1,
        source_inode=2,
        source_size_bytes=100,
        artifact_path=f"/generated/{chunk_id}.mp4",
        generated_root=f"/generated/{job_id}",
        generated_root_device=1,
        generated_root_inode=3,
        mapping_version="mapping-v1",
        upstream_settings_hash="settings-v1",
        upstream_tool_version="ffmpeg-test",
        source_start=source_start,
        source_end=source_start + D(1),
        proxy_start=D(0),
        proxy_end=D(1),
        media_duration=D(1),
        file_size_bytes=50,
        file_digest_sha256=f"digest-{chunk_id}",
        file_device=1,
        file_inode=4,
        video_codec="h264",
        video_width=640,
        video_height=360,
        video_fps=D(15),
        audio_codec="aac",
        audio_channels=1,
        audio_bitrate_bps=64000,
        audio_probe_bitrate_bps=64000,
        implementation_version="cloud-proxy-v1",
    )


def _chunk(job_id: str, source_start: Decimal) -> AnalysisChunkData:
    return AnalysisChunkData(
        schema_version=1,
        job_id=job_id,
        source_id="source-1",
        source_identity="identity-v1:fingerprint",
        mapping_version="mapping-v1",
        source_start=source_start,
        source_end=source_start + D(1),
        proxy_start=D(0),
        proxy_end=D(1),
        boundary_kind=AnalysisBoundaryKind.SCENE,
        implementation_version="cloud-proxy-v1",
    )


def _save_chunk(
    store: JobStore,
    job_id: str,
    chunk_id: str,
    source_start: Decimal,
) -> None:
    manifest_id = f"manifest-{chunk_id}"
    manifest = _manifest(job_id, chunk_id, source_start)
    store.save_proxy_manifest(
        job_id, manifest_id, manifest.file_digest_sha256, manifest
    )
    store.save_analysis_chunk(
        job_id,
        chunk_id,
        manifest_id,
        source_id="source-1",
        source_start=source_start,
        source_end=source_start + D(1),
        data=_chunk(job_id, source_start),
    )


def _broad_request(
    job_id: str, chunk_id: str, source_start: Decimal
) -> BroadAnalysisRequest:
    identity = _identity().model_copy(
        update={
            "source_identity": "identity-v1:fingerprint",
            "source_start": source_start,
            "source_end": source_start + D(1),
            "proxy_digest": f"digest-{chunk_id}",
        }
    )
    return BroadAnalysisRequest(
        manifest_id=f"manifest-{chunk_id}",
        chunk_id=chunk_id,
        cache_identity=identity,
        maximum_cost_usd=D("0.20"),
    )


def _result_metadata(request: BroadAnalysisRequest) -> AnalysisResultData:
    identity = request.cache_identity
    return AnalysisResultData(
        schema_version=1,
        provider=identity.provider,
        model=identity.model,
        request_id=request.request_id,
        prompt_version=identity.prompt_version,
        response_schema_version=identity.response_schema_version,
        implementation_version=identity.implementation_version,
        token_count=0,
        request_token_count=0,
        output_token_count=0,
        normalized_record_ids=(),
    )


def _paths(tmp_path: Path) -> PathSettings:
    return PathSettings(
        input_dir=tmp_path / "input",
        workspace_dir=tmp_path / "workspace",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
    )


def _authorize(
    manifest_id: str,
    expected_job_id: str,
    store: JobStore,
    paths: PathSettings,
) -> AuthorizedUpload:
    del expected_job_id, store, paths
    return AuthorizedUpload(manifest_id, io.BytesIO(b"offline proxy"))


def test_cache_key_is_canonical_and_changes_for_every_identity_field() -> None:
    identity = _identity()
    baseline = analysis_cache_key(identity)
    assert baseline == analysis_cache_key(identity)

    changes: dict[str, object] = {
        "source_identity": "identity-v2:def",
        "source_fingerprint": "other-fingerprint",
        "proxy_digest": "other-digest",
        "mapping_version": "mapping-v2",
        "source_start": D("1.21"),
        "source_end": D("4.51"),
        "mode": "candidate",
        "fps": D(2),
        "media_width": 480,
        "media_height": 270,
        "provider": "other-provider",
        "model": "other-model",
        "prompt_version": "broad-v2",
        "response_schema_version": "broad-v2",
        "implementation_version": "analysis-v2",
    }
    for field_name, value in changes.items():
        assert (
            analysis_cache_key(identity.model_copy(update={field_name: value}))
            != baseline
        )


def test_subsecond_gap_blocks_complete_coverage() -> None:
    report = coverage_report(
        [SourceRange(source_id="s", start=D(0), end=D(10))],
        [SourceRange(source_id="s", start=D(0), end=D("9.999"))],
    )

    assert report.ratio == D("0.9999")
    assert report.complete is False
    assert report.missing == (SourceRange(source_id="s", start=D("9.999"), end=D(10)),)


def test_coverage_merges_touching_overlap_and_retains_source_groups() -> None:
    report = coverage_report(
        [
            SourceRange(source_id="a", start=D(0), end=D(10)),
            SourceRange(source_id="b", start=D(0), end=D(2)),
        ],
        [
            SourceRange(source_id="a", start=D(0), end=D(3)),
            SourceRange(source_id="a", start=D(2), end=D(7)),
            SourceRange(source_id="a", start=D(7), end=D(10)),
            SourceRange(source_id="b", start=D(1), end=D(2)),
        ],
    )

    assert report.covered_seconds == D(11)
    assert report.expected_seconds == D(12)
    assert report.missing == (SourceRange(source_id="b", start=D(0), end=D(1)),)


def test_only_matching_persisted_inspection_exclusions_reduce_denominator() -> None:
    expected = [SourceRange(source_id="s", start=D(0), end=D(10))]
    persisted = [
        SourceRange(source_id="s", start=D(8), end=D(10)),
        SourceRange(source_id="s", start=D(2), end=D(3)),
    ]
    requested = [
        SourceRange(source_id="s", start=D(8), end=D(10)),
        SourceRange(source_id="s", start=D(4), end=D(10)),
    ]

    report = coverage_report(
        expected,
        [SourceRange(source_id="s", start=D(0), end=D(8))],
        exclusions=requested,
        persisted_inspection_exclusions=persisted,
    )

    assert report.complete is True
    assert report.expected_seconds == D(8)
    assert report.missing == ()


def test_broad_preflight_reserves_every_request_before_first_upload(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(1))
        _save_chunk(store, job_id, "chunk-1", D(0))
        _save_chunk(store, job_id, "chunk-2", D(1))
        provider = FakeProvider(store, job_id)

        outcome = run_broad_analysis(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            expected_ranges=(SourceRange(source_id="source-1", start=D(0), end=D(2)),),
            requests=(
                _broad_request(job_id, "chunk-1", D(0)),
                _broad_request(job_id, "chunk-2", D(1)),
            ),
            upload_validator=_authorize,
        )

        assert outcome.state == "complete"
        assert outcome.coverage.complete is True
        assert provider.events == [
            "upload",
            "broad",
            "delete",
            "upload",
            "broad",
            "delete",
        ]
        assert store.budget_state(job_id).reserved_usd == D(0)


def test_broad_preflight_budget_failure_emits_no_upload_or_provider_call(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D("0.30"))
        _save_chunk(store, job_id, "chunk-1", D(0))
        _save_chunk(store, job_id, "chunk-2", D(1))
        provider = FakeProvider(store, job_id)

        outcome = run_broad_analysis(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            expected_ranges=(SourceRange(source_id="source-1", start=D(0), end=D(2)),),
            requests=(
                _broad_request(job_id, "chunk-1", D(0)),
                _broad_request(job_id, "chunk-2", D(1)),
            ),
            upload_validator=_authorize,
        )

        assert outcome.state == "budget_exhausted"
        assert provider.events == []
        assert outcome.candidates == ()
        assert outcome.coverage.missing == (
            SourceRange(source_id="source-1", start=D(0), end=D(2)),
        )


def test_cache_identity_validation_rejects_wrong_processing_mode() -> None:
    from video_editor.analysis.orchestrator import _validate_identity

    manifest = _manifest("job-1", "chunk-1", D(0))
    chunk = _chunk("job-1", D(0))
    identity = _broad_request("job-1", "chunk-1", D(0)).cache_identity.model_copy(
        update={"mode": "candidate"}
    )

    with pytest.raises(
        VideoEditorError,
        match="analysis cache identity does not match persisted chunk",
    ):
        _validate_identity(identity, manifest, chunk, "broad")


def test_dispatch_rechecks_persisted_reservation_identity(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(1))
        _save_chunk(store, job_id, "chunk-1", D(0))
        request = _broad_request(job_id, "chunk-1", D(0))
        provider = FakeProvider(store, job_id)
        store.reserve_request(
            RequestReservation(
                request_id=request.request_id,
                job_id=job_id,
                cache_key=analysis_cache_key(request.cache_identity),
                mode="broad",
                maximum_cost_usd=request.maximum_cost_usd,
            )
        )
        store.connection.execute(
            "UPDATE analysis_requests SET cache_key = 'tampered' WHERE request_id = ?",
            (request.request_id,),
        )
        store.connection.commit()

        outcome = run_broad_analysis(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            expected_ranges=(SourceRange(source_id="source-1", start=D(0), end=D(1)),),
            requests=(request,),
            upload_validator=_authorize,
        )

        assert outcome.state == "analysis_incomplete"
        assert provider.events == []


def test_invalid_cached_broad_result_is_redispatched_and_does_not_count_coverage(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(1))
        _save_chunk(store, job_id, "chunk-1", D(0))
        request = _broad_request(job_id, "chunk-1", D(0))
        store.save_analysis_result(
            result_id="invalid-result",
            job_id=job_id,
            cache_key=analysis_cache_key(request.cache_identity),
            chunk_id=request.chunk_id,
            mode="broad",
            data=_result_metadata(request),
            validated=False,
            normalized={
                "schema_version": "broad-v1",
                "chunk_id": request.chunk_id,
                "scenes": [],
                "speech_presence_ranges": [],
                "candidates": [],
            },
        )
        provider = FakeProvider(store, job_id)

        outcome = run_broad_analysis(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            expected_ranges=(SourceRange(source_id="source-1", start=D(0), end=D(1)),),
            requests=(request,),
            upload_validator=_authorize,
        )

        assert outcome.state == "complete"
        assert provider.broad_calls == 1
        cached = store.find_analysis_result(
            job_id, analysis_cache_key(request.cache_identity)
        )
        assert cached is not None
        assert cached["validated"] is True


def test_unknown_billing_preserves_reservation_and_cleans_remote_upload(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(1))
        _save_chunk(store, job_id, "chunk-1", D(0))
        provider = FakeProvider(store, job_id, unknown_billing=True)

        outcome = run_broad_analysis(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            expected_ranges=(SourceRange(source_id="source-1", start=D(0), end=D(1)),),
            requests=(_broad_request(job_id, "chunk-1", D(0)),),
            upload_validator=_authorize,
        )

        assert outcome.state == "complete"
        assert store.budget_state(job_id).reserved_usd == D("0.20")
        assert provider.deleted == ["files/manifest-chunk-1"]


def test_unknown_billing_does_not_skip_other_pre_reserved_broad_requests(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(1))
        _save_chunk(store, job_id, "chunk-1", D(0))
        _save_chunk(store, job_id, "chunk-2", D(1))
        provider = FakeProvider(store, job_id, unknown_billing=True)

        outcome = run_broad_analysis(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            expected_ranges=(SourceRange(source_id="source-1", start=D(0), end=D(2)),),
            requests=(
                _broad_request(job_id, "chunk-1", D(0)),
                _broad_request(job_id, "chunk-2", D(1)),
            ),
            upload_validator=_authorize,
        )

        assert outcome.state == "complete"
        assert provider.broad_calls == 2
        assert store.budget_state(job_id).reserved_usd == D("0.40")


def test_valid_result_with_unknown_billing_is_persisted_and_counts_coverage(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(1))
        _save_chunk(store, job_id, "chunk-1", D(0))
        provider = FakeProvider(store, job_id, unknown_billing=True)
        request = _broad_request(job_id, "chunk-1", D(0))

        outcome = run_broad_analysis(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            expected_ranges=(SourceRange(source_id="source-1", start=D(0), end=D(1)),),
            requests=(request,),
            upload_validator=_authorize,
        )

        cached = store.find_analysis_result(
            job_id, analysis_cache_key(request.cache_identity)
        )
        assert outcome.state == "complete"
        assert outcome.coverage.complete is True
        assert cached is not None
        assert cached["validated"] is True
        assert store.budget_state(job_id).reserved_usd == D("0.20")


def test_upload_authorization_failure_releases_confirmed_nonbillable_reservation(
    tmp_path: Path,
) -> None:
    def reject_upload(
        manifest_id: str,
        expected_job_id: str,
        store: JobStore,
        paths: PathSettings,
    ) -> AuthorizedUpload:
        del manifest_id, expected_job_id, store, paths
        raise VideoEditorError(
            ErrorCategory.ANALYSIS,
            "upload candidate failed provenance validation",
            code="unsafe_upload_candidate",
        )

    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(1))
        _save_chunk(store, job_id, "chunk-1", D(0))
        provider = FakeProvider(store, job_id)

        outcome = run_broad_analysis(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            expected_ranges=(SourceRange(source_id="source-1", start=D(0), end=D(1)),),
            requests=(_broad_request(job_id, "chunk-1", D(0)),),
            upload_validator=reject_upload,
        )

        assert outcome.state == "analysis_incomplete"
        assert provider.events == []
        assert store.budget_state(job_id).reserved_usd == D(0)


def test_incomplete_coverage_blocks_candidate_refinement(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(1))
        provider = FakeProvider(store, job_id)
        incomplete = coverage_report(
            [SourceRange(source_id="source-1", start=D(0), end=D(2))],
            [SourceRange(source_id="source-1", start=D(0), end=D(1))],
        )

        outcome = run_candidate_refinement(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            broad_outcome=AnalysisOutcome(
                state="analysis_incomplete",
                coverage=incomplete,
            ),
            requests=(),
            segmentations=(),
            upload_validator=_authorize,
        )

        assert outcome.state == "analysis_incomplete"
        assert outcome.candidates == ()
        assert provider.candidate_calls == 0


def test_candidate_budget_exhaustion_emits_no_candidate_provider_call(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D("0.10"))
        provider = FakeProvider(store, job_id)
        complete = coverage_report(
            [SourceRange(source_id="source-1", start=D(0), end=D(1))],
            [SourceRange(source_id="source-1", start=D(0), end=D(1))],
        )
        identity = _identity().model_copy(
            update={
                "mode": "candidate",
                "fps": D(2),
                "prompt_version": "candidate-v1",
                "response_schema_version": "candidate-v1",
            }
        )
        request = CandidateAnalysisRequest(
            manifest_id="manifest-chunk-1",
            candidate=CandidateWindow(
                chunk_id="chunk-1",
                candidate_id="candidate-1",
                start=D(0),
                end=D(1),
            ),
            cache_identity=identity,
            maximum_cost_usd=D("0.20"),
        )

        outcome = run_candidate_refinement(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            broad_outcome=AnalysisOutcome(state="complete", coverage=complete),
            requests=(request,),
            segmentations=(),
            upload_validator=_authorize,
        )

        assert outcome.state == "budget_exhausted"
        assert provider.candidate_calls == 0


def test_candidate_motion_selects_exact_fps_and_persists_bounded_meaning(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(1))
        _save_chunk(store, job_id, "chunk-1", D(0))
        provider = FakeProvider(store, job_id)
        complete = coverage_report(
            [SourceRange(source_id="source-1", start=D(0), end=D(1))],
            [SourceRange(source_id="source-1", start=D(0), end=D(1))],
        )
        segmentations = tuple(
            _segmentation(index, score)
            for index, score in enumerate((0.1, 0.5, 0.9), start=1)
        )
        requests = tuple(
            _candidate_request(index, score)
            for index, score in enumerate((0.1, 0.5, 0.9), start=1)
        )
        for index in (2, 3):
            _save_chunk(store, job_id, f"chunk-{index}", D(index - 1))

        outcome = run_candidate_refinement(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            broad_outcome=AnalysisOutcome(state="complete", coverage=complete),
            requests=requests,
            segmentations=segmentations,
            upload_validator=_authorize,
        )

        assert outcome.state == "complete"
        assert provider.candidate_fps == [2, 3, 5]
        for request, fps in zip(requests, (2, 3, 5), strict=True):
            cached = store.find_analysis_result(
                job_id,
                analysis_cache_key(
                    request.cache_identity.model_copy(update={"fps": D(fps)})
                ),
            )
            assert cached is not None
            normalized = cached["normalized"]
            assert normalized["candidate_id"] == request.candidate.candidate_id
            assert D(normalized["start"]) == request.candidate.start
            assert D(normalized["end"]) == request.candidate.end
            assert normalized["speech_meaning_summary"] == "bounded meaning"


def test_candidate_motion_ignores_overlapping_evidence_from_other_source(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(1))
        _save_chunk(store, job_id, "chunk-1", D(0))
        provider = FakeProvider(store, job_id)
        complete = coverage_report(
            [SourceRange(source_id="source-1", start=D(0), end=D(1))],
            [SourceRange(source_id="source-1", start=D(0), end=D(1))],
        )
        request = _candidate_request(1, 0.1)
        own_motion = _segmentation(1, 0.1)
        foreign_motion = _segmentation(1, 0.9).model_copy(
            update={
                "source_id": "source-2",
                "source_identity": "identity-v1:other",
            }
        )

        outcome = run_candidate_refinement(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            broad_outcome=AnalysisOutcome(state="complete", coverage=complete),
            requests=(request,),
            segmentations=(own_motion, foreign_motion),
            upload_validator=_authorize,
        )

        assert outcome.state == "complete"
        assert provider.candidate_fps == [2]


def test_cached_candidate_is_returned_without_provider_call(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(1))
        _save_chunk(store, job_id, "chunk-1", D(0))
        complete = coverage_report(
            [SourceRange(source_id="source-1", start=D(0), end=D(1))],
            [SourceRange(source_id="source-1", start=D(0), end=D(1))],
        )
        request = _candidate_request(1, 0.1)
        first_provider = FakeProvider(store, job_id)
        first = run_candidate_refinement(
            store=store,
            paths=_paths(tmp_path),
            provider=first_provider,
            job_id=job_id,
            broad_outcome=AnalysisOutcome(state="complete", coverage=complete),
            requests=(request,),
            segmentations=(_segmentation(1, 0.1),),
            upload_validator=_authorize,
        )
        second_provider = FakeProvider(store, job_id)

        second = run_candidate_refinement(
            store=store,
            paths=_paths(tmp_path),
            provider=second_provider,
            job_id=job_id,
            broad_outcome=AnalysisOutcome(state="complete", coverage=complete),
            requests=(request,),
            segmentations=(_segmentation(1, 0.1),),
            upload_validator=_authorize,
        )

        assert first.state == "complete"
        assert second.state == "complete"
        assert second_provider.candidate_calls == 0
        assert len(second.candidates) == 1
        assert second.candidates[0].candidate_id == request.candidate.candidate_id


def test_candidate_unknown_billing_persists_validated_bounded_result(
    tmp_path: Path,
) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(1))
        _save_chunk(store, job_id, "chunk-1", D(0))
        provider = FakeProvider(store, job_id, candidate_unknown_billing=True)
        complete = coverage_report(
            [SourceRange(source_id="source-1", start=D(0), end=D(1))],
            [SourceRange(source_id="source-1", start=D(0), end=D(1))],
        )
        request = _candidate_request(1, 0.1)

        outcome = run_candidate_refinement(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            broad_outcome=AnalysisOutcome(state="complete", coverage=complete),
            requests=(request,),
            segmentations=(_segmentation(1, 0.1),),
            upload_validator=_authorize,
        )

        cached = store.find_analysis_result(
            job_id,
            analysis_cache_key(request.cache_identity.model_copy(update={"fps": D(2)})),
        )
        assert outcome.state == "complete"
        assert cached is not None
        assert cached["normalized"]["candidate_id"] == "candidate-1"
        assert store.budget_state(job_id).reserved_usd == D("0.20")


def _candidate_request(index: int, score: float) -> CandidateAnalysisRequest:
    del score
    chunk_id = f"chunk-{index}"
    identity = _identity().model_copy(
        update={
            "source_identity": "identity-v1:fingerprint",
            "source_start": D(index - 1),
            "source_end": D(index),
            "mode": "candidate",
            "fps": D(2),
            "prompt_version": "candidate-v1",
            "response_schema_version": "candidate-v1",
            "proxy_digest": f"digest-{chunk_id}",
        }
    )
    return CandidateAnalysisRequest(
        manifest_id=f"manifest-{chunk_id}",
        candidate=CandidateWindow(
            chunk_id=chunk_id,
            candidate_id=f"candidate-{index}",
            start=D(0),
            end=D(1),
        ),
        cache_identity=identity,
        maximum_cost_usd=D("0.20"),
    )


def _segmentation(index: int, score: float) -> LocalSegmentation:
    source_id = f"source-{index}"
    motion = {
        "evidence_id": f"motion-{source_id}",
        "proxy_range": {"start": D(index - 1), "end": D(index)},
        "source_range": {
            "source_id": source_id,
            "start": D(index - 1),
            "end": D(index),
        },
        "score": score,
    }
    return LocalSegmentation.model_validate(
        {
            "schema_version": 1,
            "implementation_version": "segmentation-v1",
            "settings_hash": "settings-v1",
            "source_id": source_id,
            "source_identity": "identity-v1:fingerprint",
            "proxy_settings_hash": "proxy-settings-v1",
            "proxy_tool_version": "ffmpeg-test",
            "scenes": [],
            "silence_ranges": [],
            "speech_presence_ranges": [],
            "audio_energy": [],
            "audio_transients": [],
            "motion": [motion],
            "motion_continuity": [],
            "blur": [],
            "shake": [],
            "exposure": [],
            "obstruction": [],
            "boundary_suitability": [],
            "candidate_windows": [],
        }
    )


def test_confirmed_nonbillable_provider_failure_releases_reservation(
    tmp_path: Path,
) -> None:
    class NonbillableProvider(FakeProvider):
        def broad_scan(
            self,
            upload: UploadedFile,
            chunk: object,
            request: AnalysisRequestContext,
            *,
            prompt_version: str,
        ) -> ProviderResult[BroadScanResponse]:
            del upload, chunk, request, prompt_version
            raise VideoEditorError(
                ErrorCategory.PROVIDER,
                "request rejected before generation",
                code="provider_invalid_request",
            )

    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(1))
        _save_chunk(store, job_id, "chunk-1", D(0))
        provider = NonbillableProvider(store, job_id)

        outcome = run_broad_analysis(
            store=store,
            paths=_paths(tmp_path),
            provider=provider,
            job_id=job_id,
            expected_ranges=(SourceRange(source_id="source-1", start=D(0), end=D(1)),),
            requests=(_broad_request(job_id, "chunk-1", D(0)),),
            upload_validator=_authorize,
        )

        assert outcome.state == "analysis_incomplete"
        assert store.budget_state(job_id).reserved_usd == D(0)
        assert provider.deleted == ["files/manifest-chunk-1"]
