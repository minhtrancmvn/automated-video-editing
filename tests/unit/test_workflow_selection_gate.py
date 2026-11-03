"""Workflow boundary regressions using only disposable state and synthetic reviews."""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_analysis_orchestrator import _authorize, _paths, _save_chunk
from test_candidate_cache_lineage import WindowProvider

from video_editor import workflow
from video_editor.errors import VideoEditorError
from video_editor.persistence.database import JobStore

D = Decimal


@pytest.fixture
def selection_env(tmp_path):
    with JobStore(tmp_path / "state.db") as store:
        job_id = store.create_job({}, {})
        store.initialize_budget(job_id, D(3))
        _save_chunk(store, job_id, "chunk-1", D(0))
        paths = _paths(tmp_path)
        (paths.workspace_dir / job_id).mkdir(parents=True)
        provider = WindowProvider(store, job_id)
        service = workflow.WorkflowService(
            SimpleNamespace(
                cloud_enabled=True,
                paths=paths,
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
        with (
            patch.object(
                service, "_phase2_stage", side_effect=lambda *args: args[-1]()
            ),
            patch.object(
                workflow,
                "run_broad_analysis",
                side_effect=lambda **kw: real_broad(**kw, upload_validator=_authorize),
            ),
            patch.object(
                workflow,
                "run_candidate_refinement",
                side_effect=lambda **kw: real_candidate(
                    **kw, upload_validator=_authorize
                ),
            ),
        ):
            yield (
                service,
                store,
                job_id,
                provider,
                {"chunks": [chunk], "segmentations": {}},
            )


def test_missing_review_stops_before_refinement_reservation(selection_env):
    service, store, job_id, provider, segmented = selection_env
    with pytest.raises(VideoEditorError) as error:
        service._ensure_analyze(job_id, segmented)
    assert error.value.code == "selection_evidence_required"
    assert provider.candidate_calls == 0
    assert provider.events.count("upload") == 1  # Broad fixture only.
    assert (
        store.connection.execute(
            "SELECT COUNT(*) FROM analysis_requests WHERE mode='candidate'"
        ).fetchone()[0]
        == 0
    )
    audits = [
        row for row in store.get_job(job_id)["artifacts"] if row["stage"] == "analyze"
    ]
    report = json.loads(Path(audits[-1]["path"]).read_text())
    assert report["decisions"][0]["state"] == "awaiting_evidence"
    assert report["decisions"][0]["proposal"]["broad"]["start"] == "0.4"


def synthetic_review(service, tmp_path, job_id):
    from video_editor.analysis.selection import SelectionReview, file_digest

    audits = [
        row
        for row in service.store.get_job(job_id)["artifacts"]
        if row["stage"] == "analyze"
    ]
    report = json.loads(Path(audits[-1]["path"]).read_text())
    decision = report["decisions"][0]
    path = tmp_path / "synthetic-video.bin"
    path.write_bytes(b"synthetic contract evidence, not footage")
    return SelectionReview.model_validate(
        {
            "proposal_digest": decision["proposal_digest"],
            "evaluator": "synthetic-test",
            "evaluator_version": "1",
            "observations": ["Synthetic test input, not audiovisual observation."],
            "factual_support": "supported",
            "editorial": "primary",
            "reason": "Synthetic incident",
            "evidence": [
                {
                    "path": str(path),
                    "sha256": file_digest(path),
                    "scope": "video",
                    "source_start": "0.4",
                    "source_end": "0.6",
                }
            ],
        }
    )


def prime_audit(service, job_id, segmented):
    with pytest.raises(VideoEditorError, match="selection"):
        service._ensure_analyze(job_id, segmented)


def test_video_review_without_paid_approval_cannot_dispatch(selection_env, tmp_path):
    service, store, job_id, provider, segmented = selection_env
    prime_audit(service, job_id, segmented)
    service.selection_reviews = (synthetic_review(service, tmp_path, job_id),)
    with pytest.raises(VideoEditorError) as error:
        service._ensure_analyze(job_id, segmented)
    assert error.value.code == "selection_authorization_required"
    assert provider.candidate_calls == 0
    assert (
        store.connection.execute(
            "SELECT COUNT(*) FROM analysis_requests WHERE mode='candidate'"
        ).fetchone()[0]
        == 0
    )


def test_exact_review_and_paid_scope_only_refine_selected_proposal(
    selection_env, tmp_path
):
    from video_editor.analysis.selection import RefinementApproval

    service, _store, job_id, provider, segmented = selection_env
    prime_audit(service, job_id, segmented)
    review = synthetic_review(service, tmp_path, job_id)
    service.selection_reviews = (review,)
    service.refinement_approval = RefinementApproval(
        approval_id="synthetic",
        job_id=job_id,
        proposal_digests=(review.proposal_digest,),
        maximum_cost_usd=D("0.2"),
        maximum_requests=1,
    )
    result = service._ensure_analyze(job_id, segmented)
    assert len(result["candidates"]) == 1
    assert provider.candidate_calls == 1
    assert result["selection"]["decisions"][0]["state"] == "eligible_for_refinement"
    service.selection_reviews = ()
    with pytest.raises(VideoEditorError) as error:
        service._ensure_analyze(job_id, segmented)
    assert error.value.code == "selection_evidence_required"
    assert provider.candidate_calls == 1


def test_paid_approval_identity_does_not_change_cached_evidence_result(
    selection_env, tmp_path
):
    from video_editor.analysis.selection import RefinementApproval

    service, _store, job_id, provider, segmented = selection_env
    prime_audit(service, job_id, segmented)
    review = synthetic_review(service, tmp_path, job_id)
    service.selection_reviews = (review,)
    service.refinement_approval = RefinementApproval(
        approval_id="first-synthetic-approval",
        job_id=job_id,
        proposal_digests=(review.proposal_digest,),
        maximum_cost_usd=D("0.2"),
        maximum_requests=1,
    )
    with patch.object(
        service,
        "_phase2_stage",
        workflow.WorkflowService._phase2_stage.__get__(service),
    ):
        first = service._ensure_analyze(job_id, segmented)
        service.refinement_approval = service.refinement_approval.model_copy(
            update={"approval_id": "second-synthetic-approval"}
        )
        second = service._ensure_analyze(job_id, segmented)
    assert first == second
    assert provider.candidate_calls == 1


def test_same_approval_cannot_dispatch_changed_refinement_settings(
    selection_env, tmp_path
):
    from video_editor.analysis.selection import RefinementApproval

    service, store, job_id, provider, segmented = selection_env
    prime_audit(service, job_id, segmented)
    review = synthetic_review(service, tmp_path, job_id)
    service.selection_reviews = (review,)
    service.refinement_approval = RefinementApproval(
        approval_id="one-synthetic-attempt",
        job_id=job_id,
        proposal_digests=(review.proposal_digest,),
        maximum_cost_usd=D("0.2"),
        maximum_requests=1,
    )
    service._ensure_analyze(job_id, segmented)
    with (
        patch.object(workflow, "CANDIDATE_PROMPT_VERSION", "synthetic-changed-prompt"),
        pytest.raises((VideoEditorError, ValueError)),
    ):
        service._ensure_analyze(job_id, segmented)
    assert provider.candidate_calls == 1
    assert (
        store.connection.execute(
            "SELECT COUNT(*) FROM analysis_requests WHERE mode='candidate'"
        ).fetchone()[0]
        == 1
    )


def test_approval_id_cannot_be_reassigned_a_different_scope(selection_env, tmp_path):
    from video_editor.analysis.selection import RefinementApproval

    service, _store, job_id, provider, segmented = selection_env
    prime_audit(service, job_id, segmented)
    review = synthetic_review(service, tmp_path, job_id)
    service.selection_reviews = (review,)
    service.refinement_approval = RefinementApproval(
        approval_id="immutable-synthetic-scope",
        job_id=job_id,
        proposal_digests=(review.proposal_digest,),
        maximum_cost_usd=D("0.2"),
        maximum_requests=1,
    )
    service._ensure_analyze(job_id, segmented)
    service.refinement_approval = service.refinement_approval.model_copy(
        update={"maximum_cost_usd": D("0.4"), "maximum_requests": 2}
    )
    with pytest.raises(VideoEditorError) as error:
        service._ensure_analyze(job_id, segmented)
    assert error.value.code == "selection_authorization_invalid"
    assert provider.candidate_calls == 1


def test_tampered_existing_audit_blocks_without_overwriting(selection_env):
    service, store, job_id, provider, segmented = selection_env
    prime_audit(service, job_id, segmented)
    audit = next(
        row for row in store.get_job(job_id)["artifacts"] if row["stage"] == "analyze"
    )
    path = Path(audit["path"])
    path.write_text('{"tampered": true}\n')
    with pytest.raises(VideoEditorError) as error:
        service._ensure_analyze(job_id, segmented)
    assert error.value.code == "selection_audit_invalid"
    assert path.read_text() == '{"tampered": true}\n'
    assert provider.candidate_calls == 0


def test_analyze_restart_preserves_prior_decision_audits(selection_env, tmp_path):
    service, store, job_id, _provider, _segmented = selection_env
    old_path = tmp_path / "old-selection.json"
    old_path.write_text('{"synthetic": "prior decision"}\n')
    store.save_artifact(job_id, "analyze", old_path, {})
    with patch.object(
        service,
        "_phase2_stage",
        workflow.WorkflowService._phase2_stage.__get__(service),
    ):
        service._phase2_stage(
            job_id, "analyze", {}, {}, workflow.ErrorCategory.ANALYSIS, dict
        )
    assert old_path in {
        Path(row["path"])
        for row in store.get_job(job_id)["artifacts"]
        if row["stage"] == "analyze"
    }
