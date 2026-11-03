"""Offline cache and lineage regression tests for broad-v4."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from test_analysis_orchestrator import (
    FakeProvider,
    _authorize,
    _paths,
    _save_chunk,
)

from video_editor import bounded_broad, workflow
from video_editor.analysis.budget import reserve_all_uncached_broad_requests
from video_editor.analysis.models import (
    BroadCandidate,
    ProviderAttemptUsage,
    ProviderUsage,
    RequestReservation,
)
from video_editor.analysis.orchestrator import (
    AnalysisCacheIdentity,
    BroadAnalysisRequest,
    _cached_broad,
    _metadata,
    analysis_cache_key,
)
from video_editor.errors import VideoEditorError
from video_editor.persistence.database import JobStore

D = Decimal


class WindowProvider(FakeProvider):
    window = ("0.4", "0.6")

    def maximum_request_cost(self, *args: Any, **kwargs: Any) -> Decimal:
        return D("0.20")

    def broad_scan(
        self, upload: Any, chunk: Any, request: Any, *, prompt_version: str
    ) -> Any:
        result = super().broad_scan(
            upload, chunk, request, prompt_version=prompt_version
        )
        candidate = BroadCandidate.model_validate(
            {
                "candidate_id": "cand_1",
                "start": self.window[0],
                "end": self.window[1],
                "category": "scenic",
                "reason": "Offline fixture, not a footage observation.",
                "confidence": 0.8,
            }
        )
        return result.model_copy(
            update={
                "response": result.response.model_copy(
                    update={"candidates": (candidate,)}
                )
            }
        )


@pytest.fixture
def analysis(
    tmp_path: Path,
    synthetic_selection: Any,
) -> Any:
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(3))
        _save_chunk(store, job_id, "chunk-1", D(0))
        provider = WindowProvider(store, job_id)
        service = workflow.WorkflowService(
            SimpleNamespace(
                cloud_enabled=True,
                paths=_paths(tmp_path),
                gemini=SimpleNamespace(
                    broad_fps=D("0.5"),
                    model="gemini-3.8-flash",
                    max_cost_per_source_hour_usd=D(3),
                ),
            ),
            store,
            provider=provider,
        )
        (service.config.paths.workspace_dir / job_id).mkdir(parents=True)
        synthetic_selection(service)
        chunk = {
            "source_id": "source-1",
            "source_identity": "identity-v1:fingerprint",
            "source_fingerprint": "fingerprint",
            "digest": "digest-chunk-1",
            "mapping_version": "mapping-v1",
            "source_start": "0",
            "source_end": "1",
            "proxy_start": "0",
            "proxy_end": "1",
            "width": 640,
            "height": 360,
            "manifest_id": "manifest-chunk-1",
            "chunk_id": "chunk-1",
        }
        real_broad = workflow.run_broad_analysis
        real_candidate = workflow.run_candidate_refinement

        def broad_call(**kwargs: Any) -> Any:
            return real_broad(**kwargs, upload_validator=_authorize)

        def candidate_call(**kwargs: Any) -> Any:
            return real_candidate(**kwargs, upload_validator=_authorize)

        with (
            patch.object(
                service, "_phase2_stage", side_effect=lambda *args: args[-1]()
            ),
            patch.object(workflow, "run_broad_analysis", side_effect=broad_call),
            patch.object(
                workflow, "run_candidate_refinement", side_effect=candidate_call
            ),
        ):
            yield service, store, job_id, provider, chunk


def test_identical_v3_v4_prompt_identities_separate_broad_cache_keys() -> None:
    """Goal D6: Identical v3/v4 prompt identities separate broad cache keys."""
    base_identity = AnalysisCacheIdentity(
        source_identity="source-1-identity",
        source_fingerprint="fingerprint-abc",
        proxy_digest="digest-xyz",
        mapping_version="mapping-v1",
        source_start=D("0"),
        source_end=D("720"),
        mode="broad",
        fps=D("0.5"),
        media_width=640,
        media_height=360,
        provider="gemini",
        model="gemini-3.8-flash",
        prompt_version="broad-v3",
        response_schema_version="broad-v2",
        implementation_version="gemini-static-v1",
    )
    identity_v3 = base_identity
    identity_v4 = base_identity.model_copy(update={"prompt_version": "broad-v4"})

    # Schema identity must remain unchanged across v3 and v4
    assert identity_v3.response_schema_version == "broad-v2"
    assert identity_v4.response_schema_version == "broad-v2"

    key_v3 = analysis_cache_key(identity_v3)
    key_v4 = analysis_cache_key(identity_v4)

    assert key_v3 != key_v4
    assert len(key_v3) == 64
    assert len(key_v4) == 64
    assert all(c in "0123456789abcdef" for c in key_v3)
    assert all(c in "0123456789abcdef" for c in key_v4)
    assert analysis_cache_key(identity_v3) == key_v3
    assert analysis_cache_key(identity_v4) == key_v4


def test_v3_stored_result_preserved_in_disposable_store_when_v4_runs(
    tmp_path: Path,
) -> None:
    """Goal D6: v3 stored result preserved in disposable store; no v3 hit for v4."""
    db_path = tmp_path / "disposable.db"
    with JobStore(db_path) as store:
        job_id = store.create_job({}, {})
        _save_chunk(store, job_id, "chunk-1", D(0))
        identity_v3 = AnalysisCacheIdentity(
            source_identity="source-1-identity",
            source_fingerprint="fingerprint-abc",
            proxy_digest="digest-xyz",
            mapping_version="mapping-v1",
            source_start=D("0"),
            source_end=D("720"),
            mode="broad",
            fps=D("0.5"),
            media_width=640,
            media_height=360,
            provider="gemini",
            model="gemini-3.8-flash",
            prompt_version="broad-v3",
            response_schema_version="broad-v2",
            implementation_version="gemini-static-v1",
        )
        identity_v4 = identity_v3.model_copy(update={"prompt_version": "broad-v4"})
        key_v3 = analysis_cache_key(identity_v3)
        key_v4 = analysis_cache_key(identity_v4)

        usage = ProviderUsage(
            reservation_id="res-1",
            attempts=(
                ProviderAttemptUsage(
                    request_id="req-v3",
                    status="succeeded",
                    prompt_tokens=10,
                    media_input_tokens=100,
                    text_input_tokens=0,
                    candidates_tokens=50,
                    thoughts_tokens=0,
                    output_tokens=50,
                    total_tokens=160,
                ),
            ),
            request_ids=("req-v3",),
            prompt_tokens=10,
            media_input_tokens=100,
            text_input_tokens=0,
            candidates_tokens=50,
            thoughts_tokens=0,
            output_tokens=50,
            total_tokens=160,
            has_unknown_billing=False,
            actual_cost_usd=D("0.05"),
        )
        v3_data = _metadata("req-v3", identity_v3, usage, ("chunk-1",))
        v3_normalized = {
            "schema_version": "broad-v1",
            "chunk_id": "chunk-1",
            "scenes": [],
            "speech_presence_ranges": [],
            "candidates": [
                {
                    "candidate_id": "cand-v3",
                    "start": "0.1",
                    "end": "0.5",
                    "category": "scenic",
                    "reason": "broad-v3 observation",
                    "confidence": 0.8,
                }
            ],
        }
        store.save_analysis_result(
            result_id="res-v3",
            job_id=job_id,
            cache_key=key_v3,
            chunk_id="chunk-1",
            mode="broad",
            data=v3_data,
            validated=True,
            normalized=v3_normalized,
        )

        # Verify v4 check does NOT hit v3 cache
        request_v4 = BroadAnalysisRequest(
            manifest_id="manifest-1",
            chunk_id="chunk-1",
            cache_identity=identity_v4,
            maximum_cost_usd=D("0.25"),
            request_id="req-v4",
        )
        assert _cached_broad(store, job_id, request_v4) is None

        # Save v4 result in the same store
        v4_data = _metadata("req-v4", identity_v4, usage, ("chunk-1",))
        v4_normalized = {
            "schema_version": "broad-v1",
            "chunk_id": "chunk-1",
            "scenes": [],
            "speech_presence_ranges": [],
            "candidates": [
                {
                    "candidate_id": "cand-v4",
                    "start": "0.2",
                    "end": "0.6",
                    "category": "scenic",
                    "reason": "broad-v4 observation",
                    "confidence": 0.9,
                }
            ],
        }
        store.save_analysis_result(
            result_id="res-v4",
            job_id=job_id,
            cache_key=key_v4,
            chunk_id="chunk-1",
            mode="broad",
            data=v4_data,
            validated=True,
            normalized=v4_normalized,
        )

        # Both results must be preserved intact in disposable store
        stored_v3 = store.find_analysis_result(job_id, key_v3)
        stored_v4 = store.find_analysis_result(job_id, key_v4)
        assert stored_v3 is not None
        assert stored_v4 is not None
        assert stored_v3["result_id"] == "res-v3"
        assert stored_v4["result_id"] == "res-v4"
        assert stored_v3["cache_key"] == key_v3
        assert stored_v4["cache_key"] == key_v4
        assert stored_v3["normalized"]["candidates"][0]["candidate_id"] == "cand-v3"
        assert stored_v4["normalized"]["candidates"][0]["candidate_id"] == "cand-v4"


def test_v3_reservations_preserved_in_disposable_store_across_v4_batch(
    tmp_path: Path,
) -> None:
    """Goal D6: v3 reservations preserved when v4 batch reservations occur."""
    db_path = tmp_path / "disposable.db"
    with JobStore(db_path) as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D("1.00"))

        identity_v3 = AnalysisCacheIdentity(
            source_identity="source-1-identity",
            source_fingerprint="fingerprint-abc",
            proxy_digest="digest-xyz",
            mapping_version="mapping-v1",
            source_start=D("0"),
            source_end=D("720"),
            mode="broad",
            fps=D("0.5"),
            media_width=640,
            media_height=360,
            provider="gemini",
            model="gemini-3.8-flash",
            prompt_version="broad-v3",
            response_schema_version="broad-v2",
            implementation_version="gemini-static-v1",
        )
        identity_v4 = identity_v3.model_copy(update={"prompt_version": "broad-v4"})
        key_v3 = analysis_cache_key(identity_v3)
        key_v4 = analysis_cache_key(identity_v4)

        # Reserve v3
        store.reserve_request(
            RequestReservation(
                request_id="req-v3",
                job_id=job_id,
                cache_key=key_v3,
                mode="broad",
                maximum_cost_usd=D("0.25"),
            )
        )

        # Reserve v4 batch
        res_v4 = RequestReservation(
            request_id="req-v4",
            job_id=job_id,
            cache_key=key_v4,
            mode="broad",
            maximum_cost_usd=D("0.25"),
        )
        reserved = reserve_all_uncached_broad_requests(store, job_id, [res_v4])
        assert reserved == ("req-v4",)

        # Verify v3 reservation is untouched
        rows = store.connection.execute(
            "SELECT request_id, cache_key, status FROM analysis_requests "
            "WHERE job_id = ? ORDER BY request_id",
            (job_id,),
        ).fetchall()
        assert len(rows) == 2
        row_v3 = next(r for r in rows if r["request_id"] == "req-v3")
        row_v4 = next(r for r in rows if r["request_id"] == "req-v4")
        assert row_v3["cache_key"] == key_v3
        assert row_v3["status"] == "reserved"
        assert row_v4["cache_key"] == key_v4
        assert row_v4["status"] == "reserved"


def test_v3_attempt_claim_preserved_in_disposable_store(tmp_path: Path) -> None:
    """Goal D6: v3 attempt claims preserved; replay blocked."""
    state_dir = tmp_path / "state"
    claim_v3 = bounded_broad._claim_attempt(state_dir, "attempt-v3")
    bounded_broad._write_attempt_state(
        claim_v3,
        {"attempt_id": "attempt-v3", "job_id": "job-1", "state": "complete"},
    )

    claim_v4 = bounded_broad._claim_attempt(state_dir, "attempt-v4")
    bounded_broad._write_attempt_state(
        claim_v4,
        {"attempt_id": "attempt-v4", "job_id": "job-2", "state": "complete"},
    )

    # Verify both claims exist independently
    assert (claim_v3 / "claimed.json").is_file()
    assert (claim_v4 / "claimed.json").is_file()
    state_v3 = json.loads((claim_v3 / "claimed.json").read_text())
    state_v4 = json.loads((claim_v4 / "claimed.json").read_text())
    assert state_v3["attempt_id"] == "attempt-v3"
    assert state_v4["attempt_id"] == "attempt-v4"

    # Replaying v3 is refused
    with pytest.raises(VideoEditorError, match="bounded attempt ID already exists"):
        bounded_broad._claim_attempt(state_dir, "attempt-v3")

    # Replaying v4 is refused
    with pytest.raises(VideoEditorError, match="bounded attempt ID already exists"):
        bounded_broad._claim_attempt(state_dir, "attempt-v4")


def test_candidate_broad_lineage_separates_v3_and_v4_with_identical_schema_identity() -> (
    None
):
    """Goal D6: Candidate broad lineage separates v3/v4; schema identity unchanged."""
    base_identity = AnalysisCacheIdentity(
        source_identity="source-1-identity",
        source_fingerprint="fingerprint-abc",
        proxy_digest="digest-xyz",
        mapping_version="mapping-v1",
        source_start=D("0"),
        source_end=D("720"),
        mode="broad",
        fps=D("0.5"),
        media_width=640,
        media_height=360,
        provider="gemini",
        model="gemini-3.8-flash",
        prompt_version="broad-v3",
        response_schema_version="broad-v2",
        implementation_version="gemini-static-v1",
    )
    identity_v3 = base_identity
    identity_v4 = base_identity.model_copy(update={"prompt_version": "broad-v4"})

    key_broad_v3 = analysis_cache_key(identity_v3)
    key_broad_v4 = analysis_cache_key(identity_v4)

    candidate = BroadCandidate.model_validate(
        {
            "candidate_id": "cand_1",
            "start": "0.4",
            "end": "0.6",
            "category": "scenic",
            "reason": "reason text",
            "confidence": 0.8,
        }
    )

    # Candidate lineage hashes the upstream broad cache key
    cand_v3 = identity_v3.model_copy(
        update={
            "mode": "candidate",
            "prompt_version": workflow.CANDIDATE_PROMPT_VERSION,
            "response_schema_version": "candidate-v2",
            "implementation_version": workflow._hash(
                [
                    workflow.CANDIDATE_IDENTITY_VERSION,
                    key_broad_v3,
                    candidate.candidate_id,
                    candidate.start.as_integer_ratio(),
                    candidate.end.as_integer_ratio(),
                ]
            ),
        }
    )
    cand_v4 = identity_v4.model_copy(
        update={
            "mode": "candidate",
            "prompt_version": workflow.CANDIDATE_PROMPT_VERSION,
            "response_schema_version": "candidate-v2",
            "implementation_version": workflow._hash(
                [
                    workflow.CANDIDATE_IDENTITY_VERSION,
                    key_broad_v4,
                    candidate.candidate_id,
                    candidate.start.as_integer_ratio(),
                    candidate.end.as_integer_ratio(),
                ]
            ),
        }
    )

    # Candidate implementation version and cache key must differ
    assert cand_v3.implementation_version != cand_v4.implementation_version
    assert analysis_cache_key(cand_v3) != analysis_cache_key(cand_v4)

    # Schema identity must remain unchanged
    assert cand_v3.response_schema_version == "candidate-v2"
    assert cand_v4.response_schema_version == "candidate-v2"
    assert cand_v3.prompt_version == workflow.CANDIDATE_PROMPT_VERSION
    assert cand_v4.prompt_version == workflow.CANDIDATE_PROMPT_VERSION
    assert cand_v3.mode == "candidate"
    assert cand_v4.mode == "candidate"


def test_broad_v3_to_v4_workflow_invalidates_refinement_without_clobbering_stored_results(
    analysis: Any,
) -> None:
    """Goal D6: Workflow migration v3->v4 triggers fresh broad and refinement without clobbering old results."""
    service, store, job_id, provider, chunk = analysis
    segmented = {"chunks": [chunk], "segmentations": {}}

    # Run 1: with broad-v3
    with patch.object(workflow, "BROAD_PROMPT_VERSION", "broad-v3"):
        service._ensure_analyze(job_id, segmented)
    assert provider.broad_calls == 1
    assert provider.candidate_calls == 1

    key_broad_v3 = analysis_cache_key(
        service._broad_identity(chunk).model_copy(update={"prompt_version": "broad-v3"})
    )
    stored_broad_v3 = store.find_analysis_result(job_id, key_broad_v3)
    assert stored_broad_v3 is not None
    assert stored_broad_v3["validated"] is True

    # Run 2: with broad-v4 (forward migration)
    with patch.object(workflow, "BROAD_PROMPT_VERSION", "broad-v4"):
        service._ensure_analyze(job_id, segmented)
    # Broad scan and candidate refinement must both be dispatched again for v4
    assert provider.broad_calls == 2
    assert provider.candidate_calls == 2

    key_broad_v4 = analysis_cache_key(
        service._broad_identity(chunk).model_copy(update={"prompt_version": "broad-v4"})
    )
    stored_broad_v4 = store.find_analysis_result(job_id, key_broad_v4)
    assert stored_broad_v4 is not None
    assert stored_broad_v4["validated"] is True

    # Verify v3 stored result was preserved and not overwritten
    recheck_v3 = store.find_analysis_result(job_id, key_broad_v3)
    assert recheck_v3 == stored_broad_v3

    # Run 3: repeat broad-v4 to verify caching
    with patch.object(workflow, "BROAD_PROMPT_VERSION", "broad-v4"):
        service._ensure_analyze(job_id, segmented)
    # Neither broad nor candidate should be called again
    assert provider.broad_calls == 2
    assert provider.candidate_calls == 2


def test_default_broad_prompt_version_is_v4(analysis: Any) -> None:
    """Goal D6: New default broad prompt version is broad-v4 (expected RED before default update)."""
    service, _, _, _, chunk = analysis

    # Expected RED assertion: checks workflow module default constant
    assert workflow.BROAD_PROMPT_VERSION == "broad-v4"

    # When default constant is updated, service._broad_identity uses v4
    identity = service._broad_identity(chunk)
    assert identity.prompt_version == "broad-v4"
    assert identity.response_schema_version == "broad-v2"
