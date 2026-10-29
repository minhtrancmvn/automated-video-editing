"""Offline migration regressions for candidate refinement cache provenance."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_analysis_orchestrator import (
    FakeProvider,
    _authorize,
    _paths,
    _save_chunk,
)

from video_editor import workflow
from video_editor.analysis.models import (
    AnalysisResultData,
    BroadCandidate,
    CandidateWindow,
    SourceRange,
)
from video_editor.analysis.orchestrator import (
    AnalysisOutcome,
    CandidateAnalysisRequest,
    analysis_cache_key,
    coverage_report,
    run_candidate_refinement,
)
from video_editor.persistence.database import JobStore

D = Decimal


class WindowProvider(FakeProvider):
    window = ("0.4", "0.6")

    def maximum_request_cost(self, *args, **kwargs):
        return D("0.20")

    def broad_scan(self, upload, chunk, request, *, prompt_version):
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
def analysis(tmp_path):
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

        def broad_call(**kwargs):
            return real_broad(**kwargs, upload_validator=_authorize)

        def candidate_call(**kwargs):
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


def test_historical_refinement_does_not_hide_corrected_broad_candidate(analysis):
    service, store, job_id, provider, chunk = analysis
    broad = service._broad_identity(chunk).model_copy(
        update={"prompt_version": "broad-v2"}
    )
    legacy_identity = broad.model_copy(
        update={
            "mode": "candidate",
            "fps": D(2),
            "prompt_version": workflow.CANDIDATE_PROMPT_VERSION,
            "response_schema_version": "candidate-v2",
            "implementation_version": f"{workflow.STAGE_IMPLEMENTATION_VERSIONS['analyze']}:cand_1",
        }
    )
    ranges = [SourceRange(source_id="source-1", start=D(0), end=D(1))]
    historical = run_candidate_refinement(
        store=store,
        paths=service.config.paths,
        provider=provider,
        job_id=job_id,
        broad_outcome=AnalysisOutcome(
            state="complete", coverage=coverage_report(ranges, ranges)
        ),
        requests=[
            CandidateAnalysisRequest(
                manifest_id="manifest-chunk-1",
                candidate=CandidateWindow(
                    chunk_id="chunk-1",
                    candidate_id="cand_1",
                    start=D("0.1"),
                    end=D("0.3"),
                ),
                cache_identity=legacy_identity,
                maximum_cost_usd=D("0.20"),
            )
        ],
        segmentations=[],
        upload_validator=_authorize,
    )
    assert historical.state == "complete"
    old_key = analysis_cache_key(legacy_identity)
    old_result = store.find_analysis_result(job_id, old_key)

    result = service._ensure_analyze(job_id, {"chunks": [chunk], "segmentations": {}})

    assert [
        (item["refined"]["start"], item["refined"]["end"])
        for item in result["candidates"]
    ] == [("0.4", "0.6")]
    assert provider.candidate_calls == 2
    assert store.find_analysis_result(job_id, old_key) == old_result


def test_broad_lineage_change_invalidates_refinement_with_same_bounds(analysis):
    service, _, job_id, provider, chunk = analysis
    segmented = {"chunks": [chunk], "segmentations": {}}
    with patch.object(workflow, "BROAD_PROMPT_VERSION", "broad-v2"):
        service._ensure_analyze(job_id, segmented)
    assert provider.candidate_calls == 1

    service._ensure_analyze(job_id, segmented)
    assert provider.candidate_calls == 2
    service._ensure_analyze(job_id, segmented)
    assert provider.candidate_calls == 2
    assert provider.broad_calls == 2


@pytest.mark.parametrize(
    "initial,revised",
    [
        (("0.4", "0.6"), ("0.7", "0.9")),
        (
            ("0.40000000000000000000000000001", "0.60000000000000000000000000001"),
            ("0.40000000000000000000000000002", "0.60000000000000000000000000002"),
        ),
    ],
)
def test_same_broad_lineage_uses_distinct_candidate_window_identity(
    analysis, initial, revised
):
    service, store, job_id, provider, chunk = analysis
    provider.window = initial
    segmented = {"chunks": [chunk], "segmentations": {}}
    first = service._ensure_analyze(job_id, segmented)
    assert first["candidates"][0]["refined"]["start"] == initial[0]
    key = analysis_cache_key(service._broad_identity(chunk))
    cached = store.find_analysis_result(job_id, key)
    normalized = cached["normalized"]
    normalized["candidates"][0].update(start=revised[0], end=revised[1])
    store.save_analysis_result(
        result_id=cached["result_id"],
        job_id=job_id,
        cache_key=key,
        chunk_id="chunk-1",
        mode="broad",
        data=AnalysisResultData.model_validate(cached["data"]),
        validated=True,
        normalized=normalized,
    )

    result = service._ensure_analyze(job_id, segmented)

    assert [
        (item["refined"]["start"], item["refined"]["end"])
        for item in result["candidates"]
    ] == [revised]
    assert provider.candidate_calls == 2
    assert provider.broad_calls == 1
