"""Offline evaluation execution using disposable DB and fake transport only."""

import io
from decimal import Decimal

import pytest
from test_analysis_orchestrator import _paths, _result_metadata, _save_chunk, _usage
from test_selection_gate import proposal

from video_editor.analysis import evaluation as e
from video_editor.analysis.evaluation_provider import (
    estimate_editorial_cost,
    estimate_grounding_cost,
)
from video_editor.analysis.models import BroadScanResponse, ProviderResult, UploadedFile
from video_editor.analysis.pricing import pricing_for_model
from video_editor.analysis.proxy_chunks import AuthorizedUpload
from video_editor.analysis.selection import selection_digest
from video_editor.persistence.database import JobStore

D = Decimal


@pytest.fixture
def context(tmp_path, monkeypatch):
    with JobStore(tmp_path / "synthetic.db") as store:
        job = store.create_job({}, {})
        store.initialize_budget(job, D(10))
        _save_chunk(store, job, "chunk-1", D(720))
        manifest = store.get_proxy_manifest_for_job(job, "manifest-chunk-1")["data"]
        media = tmp_path / "synthetic.bin"
        media.write_bytes(b"not real audiovisual evidence")
        from video_editor.analysis.selection import file_digest

        manifest["artifact_path"] = str(media)
        manifest["file_digest_sha256"] = file_digest(media)
        from video_editor.analysis.models import ProxyManifestData

        manifest = ProxyManifestData.model_validate(manifest)
        store.save_proxy_manifest(
            job, "manifest-chunk-1", manifest.file_digest_sha256, manifest
        )
        item = proposal().model_copy(
            update={
                "job_id": job,
                "chunk_id": "chunk-1",
                "source_identity": manifest.source_identity,
                "source_fingerprint": manifest.source_fingerprint,
                "proxy_digest": manifest.file_digest_sha256,
                "chunk_source_start": D(720),
                "chunk_source_end": D(721),
                "chunk_proxy_end": D(1),
                "broad": proposal().broad.model_copy(
                    update={"start": D("0.1"), "end": D("0.9")}
                ),
            }
        )
        from test_analysis_orchestrator import _broad_request

        request = _broad_request(job, "chunk-1", D(720))
        metadata = _result_metadata(request).model_copy(
            update={"request_id": item.broad_request_id}
        )
        store.save_analysis_result(
            item.broad_result_id,
            job,
            item.broad_cache_key,
            item.chunk_id,
            "broad",
            metadata,
            validated=True,
            normalized=BroadScanResponse(
                schema_version="broad-v1",
                chunk_id=item.chunk_id,
                scenes=(),
                speech_presence_ranges=(),
                candidates=(item.broad,),
            ).model_dump(mode="json"),
        )

        def authorize(manifest_id, expected_job_id, store, paths):
            assert (manifest_id, expected_job_id) == ("manifest-chunk-1", job)
            return AuthorizedUpload(manifest_id, io.BytesIO(media.read_bytes()))

        monkeypatch.setattr(e, "validate_upload_candidate", authorize)
        settings = pricing_for_model("gemini-3.8-flash")
        plan = e.EvaluationPlan(
            inputs=(
                e.EvaluationInput(
                    proposal=item, manifest_id="manifest-chunk-1", manifest=manifest
                ),
            ),
            video_costs=(estimate_grounding_cost(item, settings),),
            editorial_cost=estimate_editorial_cost(settings),
        )
        approval = e.EvaluationApproval(
            approval_id="synthetic-attempt",
            plan_digest=selection_digest(plan),
            maximum_generations=plan.maximum_generations,
            maximum_cost_usd=plan.maximum_cost_usd,
        )
        yield store, _paths(tmp_path), plan, approval, media


class FakeProvider:
    def __init__(self, unknown=False, failure=False, support="supported"):
        self.calls = []
        self.unknown = unknown
        self.failure = failure
        self.support = support

    def upload(self, authorization):
        self.calls.append("upload")
        return UploadedFile(
            name="synthetic",
            uri="synthetic://video",
            mime_type="video/mp4",
            state="ACTIVE",
            manifest_id=authorization.manifest_id,
        )

    def delete_upload(self, upload):
        self.calls.append("delete")

    def ground(self, upload, *, request_id, proposal):
        self.calls.append("ground")
        if self.failure:
            raise RuntimeError("private arbitrary transport detail")
        result = e.GroundingResult(
            proposal_digest=selection_digest(proposal),
            observations=(
                {
                    "observation_id": "o1",
                    "start": "0.2",
                    "end": "0.8",
                    "modality": "visual",
                    "description": "Synthetic observation.",
                },
            ),
            assessments=tuple(
                {
                    "field": field,
                    "text": getattr(proposal.broad, field),
                    "claims": (
                        {
                            "text": "Synthetic claim",
                            "support": self.support,
                            "observation_ids": ("o1",),
                        },
                    ),
                }
                for field in ("category", "reason")
            ),
        )
        usage = _usage(request_id, self.unknown)
        return ProviderResult(response=result, usage=usage)

    def compare(self, *, request_id, set_digest, groundings):
        self.calls.append("compare")
        return ProviderResult(
            response=e.EditorialResult(
                set_digest=set_digest,
                decisions=tuple(
                    {
                        "proposal_digest": g.proposal_digest,
                        "disposition": "primary",
                        "reason": "Synthetic editorial opinion.",
                        "observation_refs": (f"{g.proposal_digest}:o1",),
                        "basis": "incident_value",
                    }
                    for g in groundings
                ),
            ),
            usage=_usage(request_id, False),
        )


def run(context, provider):
    store, paths, plan, approval, _ = context
    return e.run_evaluation(
        plan=plan,
        approval=approval,
        provider_factory=lambda: provider,
        store=store,
        paths=paths,
    )


def test_complete_run_admits_refinement_but_never_render(context):
    provider = FakeProvider()
    bundle = run(context, provider)
    assert bundle.complete is True
    assert provider.calls == ["upload", "ground", "delete", "compare"]
    store, paths, plan, _, _ = context
    report = e.admit_completed_bundle(bundle, store=store, paths=paths)
    assert report.eligible == (plan.inputs[0].proposal,)
    assert report.render_ready is False
    assert report.paid_dispatch_authorized is False


def test_unknown_billing_keeps_hold_and_stops_comparison(context):
    provider = FakeProvider(unknown=True)
    bundle = run(context, provider)
    assert bundle.complete is False
    assert "compare" not in provider.calls
    store, paths, plan, _, _ = context
    assert store.budget_state(plan.job_id).reserved_usd == plan.video_costs[0]
    with pytest.raises(ValueError, match="complete"):
        e.admit_completed_bundle(bundle, store=store, paths=paths)


def test_failure_is_sanitized_and_keeps_full_hold(context):
    bundle = run(context, FakeProvider(failure=True))
    assert bundle.complete is False
    assert "private arbitrary" not in bundle.model_dump_json()
    assert (
        context[0].budget_state(context[2].job_id).reserved_usd
        == context[2].video_costs[0]
    )


def test_unknown_grounding_review_is_preserved_when_editorial_skipped(context):
    bundle = run(context, FakeProvider(support="unknown"))
    reviews = e.evaluated_reviews(bundle, store=context[0], paths=context[1])
    assert len(reviews) == 1
    assert reviews[0].factual_support == "unknown"
    report = e.admit_completed_bundle(bundle, store=context[0], paths=context[1])
    assert report.decisions[0].review is not None
    assert report.decisions[0].reason == "factual_support_unknown"


def test_no_supported_proposals_skip_editorial(context):
    provider = FakeProvider(support="unknown")
    bundle = run(context, provider)
    assert bundle.complete is True
    assert "compare" not in provider.calls
    report = e.admit_completed_bundle(bundle, store=context[0], paths=context[1])
    assert report.eligible == ()
    assert report.decisions[0].state == "awaiting_evidence"


def test_same_attempt_cannot_dispatch_twice(context):
    provider = FakeProvider()
    run(context, provider)
    before = provider.calls.copy()
    with pytest.raises(ValueError, match="attempt"):
        run(context, provider)
    assert provider.calls == before


def test_underquoted_plan_refused_before_provider(context):
    store, paths, plan, _, _ = context
    plan = plan.model_copy(update={"video_costs": (D("0.000001"),)})
    approval = e.EvaluationApproval(
        approval_id="underquote",
        plan_digest=selection_digest(plan),
        maximum_generations=2,
        maximum_cost_usd=plan.maximum_cost_usd,
    )
    calls = []
    with pytest.raises(ValueError, match="cost"):
        e.run_evaluation(
            plan=plan,
            approval=approval,
            provider_factory=lambda: calls.append("provider"),
            store=store,
            paths=paths,
        )
    assert calls == []


def test_changed_registered_manifest_blocks_before_factory(context):
    store, paths, plan, approval, _ = context
    store.connection.execute(
        "UPDATE proxy_manifests SET data_json = replace(data_json, 'mapping-v1', 'mapping-changed')"
    )
    store.connection.commit()
    calls = []
    with pytest.raises(ValueError, match="registered"):
        e.run_evaluation(
            plan=plan,
            approval=approval,
            provider_factory=lambda: calls.append("provider"),
            store=store,
            paths=paths,
        )
    assert calls == []


def test_admission_checks_ledger_not_only_bundle_hash(context):
    bundle = run(context, FakeProvider())
    data = bundle.model_dump(mode="json")
    data["audit"]["groundings"][0]["result"]["observations"][0]["description"] = (
        "Forged observation"
    )
    audit = e.EvaluationAudit.model_validate(data["audit"])
    forged = e.EvaluationBundle(audit=audit, audit_digest=selection_digest(audit))
    with pytest.raises(ValueError, match="persisted"):
        e.admit_completed_bundle(forged, store=context[0], paths=context[1])


def test_invalid_returned_grounding_still_charges_known_usage(context):
    class InvalidProvider(FakeProvider):
        def ground(self, upload, *, request_id, proposal):
            result = super().ground(upload, request_id=request_id, proposal=proposal)
            return ProviderResult(
                response=result.response.model_copy(
                    update={"proposal_digest": "0" * 64}
                ),
                usage=result.usage.model_copy(update={"actual_cost_usd": D("0.01")}),
            )

    bundle = run(context, InvalidProvider())
    assert not bundle.complete
    budget = context[0].budget_state(context[2].job_id)
    assert budget.spent_usd == D("0.01")
    assert budget.reserved_usd == 0


def test_overrun_receipt_cannot_be_admitted_or_reused(context):
    bundle = run(context, FakeProvider())
    store, paths, plan, approval, _ = context
    store.connection.execute(
        "UPDATE evaluation_requests SET actual_cost_microusd = maximum_cost_microusd + 1"
    )
    store.connection.commit()
    with pytest.raises(ValueError, match="billing"):
        e.admit_completed_bundle(bundle, store=store, paths=paths)
    calls = []
    with pytest.raises(ValueError, match="billing"):
        e.run_evaluation(
            plan=plan,
            approval=approval.model_copy(update={"approval_id": "overrun-reuse"}),
            provider_factory=lambda: calls.append("factory"),
            store=store,
            paths=paths,
        )
    assert calls == []


def test_claim_identity_cannot_reassign_same_approval_id(context):
    _, paths, _, approval, _ = context
    e._claim_attempt(paths, approval)
    changed = approval.model_copy(
        update={"maximum_cost_usd": approval.maximum_cost_usd + 1}
    )
    with pytest.raises(ValueError, match="attempt"):
        e._claim_attempt(paths, changed)


def test_cache_reuse_does_not_create_provider_or_new_requests(context):
    run(context, FakeProvider())
    store, paths, plan, approval, _ = context
    approval = approval.model_copy(update={"approval_id": "new-synthetic-attempt"})
    calls = []
    bundle = e.run_evaluation(
        plan=plan,
        approval=approval,
        provider_factory=lambda: calls.append("provider"),
        store=store,
        paths=paths,
    )
    assert bundle.complete is True
    assert calls == []
    assert (
        store.connection.execute("SELECT count(*) FROM evaluation_requests").fetchone()[
            0
        ]
        == 2
    )
