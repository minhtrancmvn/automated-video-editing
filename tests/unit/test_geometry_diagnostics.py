"""Strict box contract and content-free rejection diagnostics."""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from video_editor.analysis.gemini import GeminiAdapter, _response_diagnostic
from video_editor.analysis.models import (
    AnalysisRequestContext,
    CandidateRefinementResponse,
    RequestReservation,
    RetryPolicy,
)
from video_editor.analysis.orchestrator import (
    AnalysisCacheIdentity,
    _persist_failure_evidence,
    analysis_cache_key,
)
from video_editor.analysis.pricing import pricing_for_model
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.persistence.database import JobStore
from video_editor.workflow import BROAD_PROMPT_VERSION, CANDIDATE_PROMPT_VERSION

FIXTURE = Path(__file__).parents[1] / "fixtures" / "gemini" / "refinement-response.json"


def test_geometry_prompt_has_genuine_version_without_changing_historical_prompt() -> (
    None
):
    request = AnalysisRequestContext(
        reservation_id="r",
        job_id="j",
        manifest_id="m",
        mode="candidate",
        chunk_id="c",
        candidate_id="v",
    )
    kwargs = {
        "request": request,
        "chunk_id": "c",
        "candidate_id": "v",
        "start": Decimal(1),
        "end": Decimal(3),
    }
    assert BROAD_PROMPT_VERSION == "broad-v4"
    assert CANDIDATE_PROMPT_VERSION == "candidate-v4"
    old = GeminiAdapter._prompt("candidate-v3", **kwargs)
    prompt = GeminiAdapter._prompt(CANDIDATE_PROMPT_VERSION, **kwargs)
    assert "prompt_version: candidate-v3" in old
    assert "x + w <= 1" not in old
    for instruction in (
        "prompt_version: candidate-v4",
        "JSON array",
        "w > 0",
        "h > 0",
        "x + w <= 1",
        "y + h <= 1",
        "top-left",
        "width and height",
        "empty array []",
        "Do not invent",
        "decimal-seconds strings",
        "Echo chunk_id, candidate_id, start, and end exactly",
    ):
        assert instruction in prompt
    estimator = GeminiAdapter(
        SimpleNamespace(),
        retry_policy=RetryPolicy(max_attempts=1),
        pricing=pricing_for_model("gemini-3.8-flash"),
    )
    window = SimpleNamespace(proxy_start=Decimal(1), proxy_end=Decimal(3))
    assert (
        estimator.maximum_request_cost("m", window, prompt_version="candidate-v4") > 0
    )


@pytest.mark.parametrize(
    ("start", "end"),
    [("50.0", "62.0"), ("0", "900.0324669999999969149939716")],
)
def test_geometry_prompt_fits_reserved_allowance_with_persisted_identity_lengths(
    start: str,
    end: str,
) -> None:
    chunk_id = "analysis-chunk-" + "a" * 64
    request = AnalysisRequestContext(
        reservation_id="12345678-1234-1234-1234-123456789012",
        job_id="12345678-1234-1234-1234-123456789012",
        manifest_id="proxy-manifest-" + "a" * 64,
        mode="candidate",
        chunk_id=chunk_id,
        candidate_id="candidate-01",
    )
    prompt = GeminiAdapter._prompt(
        "candidate-v4",
        request=request,
        chunk_id=chunk_id,
        candidate_id="candidate-01",
        start=Decimal(start),
        end=Decimal(end),
    )
    assert len(prompt.encode()) <= 1024
    for rule in ("x + w <= 1", "y + h <= 1", "w > 0", "h > 0", "empty array []"):
        assert rule in prompt


@pytest.mark.parametrize(
    ("boxes", "expected"),
    [
        (
            [{"time": "2", "x": 0.9, "y": 0.1, "w": 0.2, "h": 0.2, "priority": 0}],
            {"type": "value_error", "loc": ["subject_boxes", 0]},
        ),
        (
            [{"time": "2", "x": 0.1, "y": 0.9, "w": 0.2, "h": 0.2, "priority": 0}],
            {"type": "value_error", "loc": ["subject_boxes", 0]},
        ),
        (
            [{"time": "2", "x": 0.1, "y": 0.1, "w": 0, "h": 0.2, "priority": 0}],
            {"type": "value_error", "loc": ["subject_boxes", 0]},
        ),
        (None, {"type": "tuple_type", "loc": ["subject_boxes"]}),
        ({}, {"type": "tuple_type", "loc": ["subject_boxes"]}),
        (
            [
                {
                    "time": "2",
                    "x": 0.1,
                    "y": 0.1,
                    "w": 0.2,
                    "h": 0.2,
                    "priority": 0,
                    "private-provider-key": "private-content",
                }
            ],
            {"type": "extra_forbidden", "loc": ["subject_boxes", 0, "unknown"]},
        ),
    ],
)
def test_bad_box_diagnostic_keeps_type_index_not_provider_content(
    boxes, expected
) -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["subject_boxes"] = boxes
    with pytest.raises(ValidationError) as caught:
        CandidateRefinementResponse.model_validate(payload)
    diagnostic = _response_diagnostic(
        SimpleNamespace(candidates=[SimpleNamespace(finish_reason="STOP")]),
        CandidateRefinementResponse,
        caught.value,
    )
    assert diagnostic["errors"] == [expected]
    assert "private" not in json.dumps(diagnostic)
    assert "ctx" not in json.dumps(diagnostic)
    assert "input" not in json.dumps(diagnostic)
    assert "msg" not in json.dumps(diagnostic)


def test_geometry_version_does_not_reset_settled_failed_request(tmp_path: Path) -> None:
    old = AnalysisCacheIdentity(
        source_identity="s",
        source_fingerprint="f",
        proxy_digest="d",
        mapping_version="m",
        source_start=Decimal(0),
        source_end=Decimal(10),
        mode="candidate",
        fps=Decimal(5),
        media_width=640,
        media_height=360,
        provider="gemini",
        model="gemini-3.8-flash",
        prompt_version="candidate-v3",
        response_schema_version="candidate-v2",
        implementation_version="gemini-static-v1:v",
    )
    new = old.model_copy(update={"prompt_version": CANDIDATE_PROMPT_VERSION})
    old_key = analysis_cache_key(old)
    new_key = analysis_cache_key(new)
    assert old_key != new_key
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job({}, {})
        store.initialize_budget(job, Decimal(1))
        reservation = RequestReservation(
            request_id="old",
            job_id=job,
            cache_key=old_key,
            mode="candidate",
            maximum_cost_usd=Decimal("0.1"),
        )
        store.reserve_request(reservation)
        store.mark_request_dispatched("old")
        store.settle_request("old", Decimal("0.006257"))
        before = dict(
            store.connection.execute(
                "select * from analysis_requests where request_id='old'"
            ).fetchone()
        )
        with pytest.raises((VideoEditorError, ValueError)):
            store.reserve_request(
                reservation.model_copy(update={"request_id": "retry"})
            )
        assert (
            dict(
                store.connection.execute(
                    "select * from analysis_requests where request_id='old'"
                ).fetchone()
            )
            == before
        )
        assert store.find_analysis_result(job, new_key) is None
        assert store.budget_state(job).spent_usd == Decimal("0.006257")


def test_durable_error_paths_are_filtered_and_bounded_again(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job({}, {})
        store.initialize_budget(job, Decimal(1))
        store.reserve_request(
            RequestReservation(
                request_id="r",
                job_id=job,
                cache_key="key",
                mode="candidate",
                maximum_cost_usd=Decimal("0.1"),
            )
        )
        entries = [
            {
                "type": "value_error",
                "loc": ["subject_boxes", 0],
                "msg": "private",
                "input": "private",
                "ctx": {"secret": "private"},
            },
            {"type": "private", "loc": ["subject_boxes", 0, "private"]},
            {
                "type": "extra_forbidden",
                "loc": ["subject_boxes", True, -1, 1000001, "private"],
            },
        ] + [{"type": "value_error", "loc": ["subject_boxes"] * 20}] * 30
        _persist_failure_evidence(
            store,
            "r",
            VideoEditorError(
                ErrorCategory.PROVIDER,
                "private",
                code="provider_response_invalid",
                safe_details={
                    "diagnostic": json.dumps(
                        {
                            "reason": "schema_validation",
                            "fields": ["subject_boxes", "private"],
                            "errors": entries,
                        }
                    )
                },
            ),
            None,
        )
        path = tmp_path / "analysis-failures" / job / "analysis-failure-r.json"
        payload = json.loads(path.read_text())
        errors = payload["diagnostic"]["errors"]
        assert len(errors) == 16
        assert errors[0] == {"type": "value_error", "loc": ["subject_boxes", 0]}
        assert errors[1] == {"type": "unknown", "loc": ["subject_boxes", 0, "unknown"]}
        assert errors[2]["loc"] == ["subject_boxes"] + ["unknown"] * 4
        assert max(len(entry["loc"]) for entry in errors) <= 8
        assert "private" not in json.dumps(payload)
        assert store.budget_state(job).reserved_usd == Decimal("0.1")
        assert (
            store.connection.execute(
                "select count(*) from analysis_results"
            ).fetchone()[0]
            == 0
        )
