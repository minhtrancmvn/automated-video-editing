"""Bounded evaluator contracts; observations remain attributed model assertions."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, model_validator

from video_editor.analysis.models import (
    AnalysisModel,
    BroadScanResponse,
    FiniteDecimal,
    ProviderDecimal,
    ProviderResult,
    ProviderUsage,
    ProxyManifestData,
    UploadedFile,
)
from video_editor.analysis.pricing import pricing_for_model
from video_editor.analysis.proxy_chunks import (
    AuthorizedUpload,
    validate_upload_candidate,
)
from video_editor.analysis.selection import (
    Digest,
    SelectionEvidence,
    SelectionProposal,
    SelectionReport,
    SelectionReview,
    evaluate_selection,
    selection_digest,
)
from video_editor.config import PathSettings
from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.persistence.database import JobStore
from video_editor.persistence.evaluation import EvaluationLedger

Text = Annotated[str, Field(min_length=1, max_length=2048)]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Support = Literal["supported", "contradicted", "unknown"]
EVALUATION_VERSION = "registered-evaluation-v1"


class EvaluationInput(AnalysisModel):
    """Exported proposal plus registered manifest; never upload-authority alone."""

    proposal: SelectionProposal
    manifest_id: Identifier
    manifest: ProxyManifestData

    @model_validator(mode="after")
    def lineage(self) -> Self:
        p, m = self.proposal, self.manifest
        if (
            (
                p.job_id,
                p.chunk_id,
                p.source_identity,
                p.source_fingerprint,
                p.proxy_digest,
                p.mapping_version,
                p.chunk_source_start,
                p.chunk_source_end,
                p.chunk_proxy_start,
                p.chunk_proxy_end,
            )
            != (
                m.job_id,
                m.chunk_id,
                m.source_identity,
                m.source_fingerprint,
                m.file_digest_sha256,
                m.mapping_version,
                m.source_start,
                m.source_end,
                m.proxy_start,
                m.proxy_end,
            )
            or not m.proxy_start <= p.broad.start < p.broad.end <= m.proxy_end
            or p.source_start < m.source_start
            or p.source_end > m.source_end
            or p._source_time(m.proxy_end) != m.source_end
        ):
            raise ValueError("evaluation input lineage differs")
        exact_offset(p.broad.start)
        exact_offset(p.broad.end)
        return self


class EvaluationPlan(AnalysisModel):
    """Finite conservative request plan; no credentials or DB writes required."""

    version: Literal["registered-evaluation-v1"] = "registered-evaluation-v1"
    inputs: tuple[EvaluationInput, ...] = Field(min_length=1, max_length=64)
    model: Literal["gemini-3.8-flash"] = "gemini-3.8-flash"
    fps: Literal[3] = 3
    output_tokens: Literal[8192] = 8192
    input_bytes: Literal[32768] = 32768
    video_costs: tuple[FiniteDecimal, ...] = Field(min_length=1, max_length=64)
    editorial_cost: FiniteDecimal = Field(gt=0)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        digests = [selection_digest(item.proposal) for item in self.inputs]
        if len(set(digests)) != len(digests):
            raise ValueError("duplicate evaluation proposal")
        if len({item.proposal.job_id for item in self.inputs}) != 1:
            raise ValueError("evaluation job differs")
        if len(self.video_costs) != len(self.inputs) or any(
            cost <= 0 for cost in self.video_costs
        ):
            raise ValueError("evaluation request cost plan differs")
        return self

    @property
    def request_costs(self) -> tuple[Decimal, ...]:
        """At most one conditional editorial request; conservative total includes it."""
        return (*self.video_costs, self.editorial_cost)

    @property
    def maximum_generations(self) -> int:
        return len(self.request_costs)

    @property
    def maximum_cost_usd(self) -> Decimal:
        return sum(self.request_costs, Decimal(0))

    @property
    def job_id(self) -> str:
        return self.inputs[0].proposal.job_id


def evaluation_set_digest(inputs: tuple[EvaluationInput, ...]) -> str:
    """Canonical identity for fixed editorial comparison input set."""
    import hashlib
    import json

    payload = [selection_digest(item.proposal) for item in inputs]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":")).encode()
    ).hexdigest()


class EvaluationApproval(AnalysisModel):
    """Trusted local scope assertion, not authenticated external consent."""

    approval_id: Identifier
    plan_digest: Digest
    maximum_generations: int = Field(gt=0, strict=True)
    maximum_cost_usd: FiniteDecimal = Field(gt=0)


def require_evaluation_approval(
    plan: EvaluationPlan | None, approval: EvaluationApproval | None
) -> None:
    """Exact scope and money check before creating a credentialed provider."""
    from fractions import Fraction

    if (
        not isinstance(plan, EvaluationPlan)
        or not isinstance(approval, EvaluationApproval)
        or approval.plan_digest != selection_digest(plan)
        or approval.maximum_generations != plan.maximum_generations
        or sum((Fraction(cost) for cost in plan.request_costs), Fraction(0))
        > Fraction(approval.maximum_cost_usd)
    ):
        raise ValueError("evaluation approval required for exact plan")


class Observation(AnalysisModel):
    """Attributed observation inside the requested proxy interval."""

    observation_id: Identifier
    start: ProviderDecimal = Field(ge=0)
    end: ProviderDecimal = Field(gt=0)
    modality: Literal["visual", "audio", "audiovisual"]
    description: Text

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("observation interval must be ordered")
        return self


class Claim(AnalysisModel):
    """Factual assertion; editorial wording is not evidence."""

    text: Text
    support: Support
    observation_ids: tuple[Identifier, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def referenced(self) -> Self:
        if self.support != "unknown" and not self.observation_ids:
            raise ValueError("supported or contradicted claim requires observation")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("duplicate observation reference")
        return self


class Assessment(AnalysisModel):
    """Account for each original broad field without rewriting it."""

    field: Literal["category", "reason"]
    text: Text
    claims: tuple[Claim, ...] = Field(max_length=32)
    editorial_wording: tuple[Text, ...] = Field(default=(), max_length=16)


class GroundingResult(AnalysisModel):
    """Strict semantic assertions, not independently verified facts."""

    proposal_digest: Digest
    observations: tuple[Observation, ...] = Field(min_length=1, max_length=64)
    assessments: tuple[Assessment, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def complete(self) -> Self:
        if {item.field for item in self.assessments} != {"category", "reason"}:
            raise ValueError("category and reason assessments required")
        ids = [item.observation_id for item in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate observation identity")
        if any(
            ref not in ids
            for block in self.assessments
            for claim in block.claims
            for ref in claim.observation_ids
        ):
            raise ValueError("unknown observation reference")
        return self

    @property
    def factual_support(self) -> Support:
        states = [claim.support for block in self.assessments for claim in block.claims]
        if "contradicted" in states:
            return "contradicted"
        if any(not block.claims for block in self.assessments) or "unknown" in states:
            return "unknown"
        return "supported"


def validate_grounding(
    result: GroundingResult,
    proposal_digest: str,
    category: str,
    reason: str,
    start: Decimal,
    end: Decimal,
) -> None:
    """Check exact input identity and references, never truth of observations."""
    if result.proposal_digest != proposal_digest:
        raise ValueError("grounding proposal identity differs")
    expected = {"category": category, "reason": reason}
    if any(expected[item.field] != item.text for item in result.assessments):
        raise ValueError("original claim text differs")
    if any(not start <= item.start < item.end <= end for item in result.observations):
        raise ValueError("observation outside reviewed interval")


class EditorialDecision(AnalysisModel):
    """Editorial opinion tied to explicitly cited grounded observations."""

    proposal_digest: Digest
    disposition: Literal["primary", "reject", "alternative"]
    reason: Text
    observation_refs: tuple[Text, ...] = Field(min_length=1, max_length=128)
    alternative_to: tuple[Digest, ...] = Field(default=(), max_length=64)
    basis: Literal["semantic", "incident_value", "uncertain"]


class EditorialResult(AnalysisModel):
    """Text-only comparison cannot supply new visual observations."""

    set_digest: Digest
    decisions: tuple[EditorialDecision, ...] = Field(min_length=1, max_length=64)


def validate_editorial(
    result: EditorialResult,
    set_digest: str,
    evidence: dict[str, tuple[Support, set[str]]],
) -> None:
    """Validate citations and edges, not perceptual similarity or viewer appeal."""
    rows = {item.proposal_digest: item for item in result.decisions}
    if (
        result.set_digest != set_digest
        or len(rows) != len(result.decisions)
        or set(rows) != set(evidence)
    ):
        raise ValueError("editorial proposal set differs")
    available = {
        f"{digest}:{obs}" for digest, (_, ids) in evidence.items() for obs in ids
    }
    for digest, item in rows.items():
        if any(ref not in available for ref in item.observation_refs):
            raise ValueError("editorial observation reference unavailable")
        if not any(ref.startswith(f"{digest}:") for ref in item.observation_refs):
            raise ValueError("editorial reason lacks own observation")
        if item.disposition != "reject" and evidence[digest][0] != "supported":
            raise ValueError("unsupported proposal cannot be promoted")
        if item.disposition == "primary" and item.basis == "uncertain":
            raise ValueError("uncertain comparison cannot become primary")
        if item.disposition == "alternative":
            if not item.alternative_to or item.basis != "semantic":
                raise ValueError("alternative requires explicit semantic primary")
            for target in item.alternative_to:
                if (
                    target == digest
                    or target not in rows
                    or rows[target].disposition != "primary"
                    or evidence[target][0] != "supported"
                ):
                    raise ValueError("alternative primary unavailable")
                if not any(
                    ref.startswith(f"{target}:") for ref in item.observation_refs
                ):
                    raise ValueError("alternative lacks primary observation")
        elif item.alternative_to:
            raise ValueError("non-alternative has primary links")


class EvaluationProvider(Protocol):
    """Single-generation transport boundary; factory remains lazy."""

    def upload(self, authorization: AuthorizedUpload) -> UploadedFile: ...
    def delete_upload(self, upload: UploadedFile) -> None: ...
    def ground(
        self, upload: UploadedFile, *, request_id: str, proposal: SelectionProposal
    ) -> ProviderResult[GroundingResult]: ...
    def compare(
        self,
        *,
        request_id: str,
        set_digest: str,
        groundings: tuple[GroundingResult, ...],
    ) -> ProviderResult[EditorialResult]: ...


class GroundingReceipt(AnalysisModel):
    """Full supplied file extent and smaller sampled review extent remain distinct."""

    input: EvaluationInput
    cache_key: Digest
    request_id: Identifier
    upload: UploadedFile
    result: GroundingResult
    usage: ProviderUsage
    sampled_fps: Literal[3] = 3
    audio_supplied: Literal[True] = True
    continuous_playback_verified: Literal[False] = False


class EditorialReceipt(AnalysisModel):
    cache_key: Digest
    request_id: Identifier
    result: EditorialResult
    usage: ProviderUsage


class EvaluationAudit(AnalysisModel):
    version: Literal["registered-evaluation-v1"] = "registered-evaluation-v1"
    plan: EvaluationPlan
    approval: EvaluationApproval
    groundings: tuple[GroundingReceipt, ...]
    editorial: EditorialReceipt | None = None
    complete: bool
    stop_reason: (
        Literal["provider_failure", "billing_unknown", "cleanup_failure"] | None
    ) = None


class EvaluationBundle(AnalysisModel):
    """Integrity envelope; admission also checks authoritative persisted records."""

    audit: EvaluationAudit
    audit_digest: Digest

    @property
    def complete(self) -> bool:
        return self.audit.complete


def _digest_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _grounding_key(item: EvaluationInput) -> str:
    return _digest_payload(
        {
            "version": EVALUATION_VERSION,
            "model": "gemini-3.8-flash",
            "fps": 3,
            "output_tokens": 8192,
            "input_bytes": 32768,
            "input": item.model_dump(mode="json"),
        }
    )


def _editorial_key(
    inputs: tuple[EvaluationInput, ...], groundings: tuple[GroundingReceipt, ...]
) -> str:
    return _digest_payload(
        {
            "version": EVALUATION_VERSION,
            "rubric": "motorbike-vlog-v1",
            "model": "gemini-3.8-flash",
            "output_tokens": 8192,
            "input_bytes": 32768,
            "set": evaluation_set_digest(inputs),
            "groundings": [g.result.model_dump(mode="json") for g in groundings],
        }
    )


def _registered_input(item: EvaluationInput, store: JobStore) -> None:
    record = store.get_proxy_manifest_for_job(item.proposal.job_id, item.manifest_id)
    if (
        record is None
        or ProxyManifestData.model_validate(record["data"]) != item.manifest
    ):
        raise ValueError("registered evaluation manifest differs")
    proposal = item.proposal
    broad = store.find_analysis_result(proposal.job_id, proposal.broad_cache_key)
    if (
        broad is None
        or not broad["validated"]
        or broad["mode"] != "broad"
        or broad["result_id"] != proposal.broad_result_id
        or broad["chunk_id"] != proposal.chunk_id
        or broad["data"]["request_id"] != proposal.broad_request_id
    ):
        raise ValueError("registered broad proposal lineage differs")
    normalized = BroadScanResponse.model_validate(broad.get("normalized"))
    if (
        normalized.chunk_id != proposal.chunk_id
        or sum(c == proposal.broad for c in normalized.candidates) != 1
    ):
        raise ValueError("registered broad proposal contents differ")


def _check_grounding(receipt: GroundingReceipt, item: EvaluationInput) -> None:
    if (
        receipt.input != item
        or receipt.cache_key != _grounding_key(item)
        or receipt.upload.manifest_id != item.manifest_id
        or receipt.upload.state != "ACTIVE"
        or receipt.usage.reservation_id != receipt.request_id
    ):
        raise ValueError("grounding receipt identity differs")
    p = item.proposal
    validate_grounding(
        receipt.result,
        selection_digest(p),
        p.broad.category,
        p.broad.reason,
        p.broad.start,
        p.broad.end,
    )


def _check_usage(usage: ProviderUsage, request_id: str) -> None:
    if usage.reservation_id != request_id or len(usage.attempts) != 1:
        raise ValueError("evaluation usage identity or generation count differs")


def _persisted_receipt(
    ledger: EvaluationLedger,
    job_id: str,
    key: str,
    phase: str,
    receipt: GroundingReceipt | EditorialReceipt,
) -> None:
    row = ledger.find_result(job_id, key)
    request = ledger.get_request(receipt.request_id)
    if (
        row is None
        or request is None
        or row["status"] != "completed"
        or row["phase"] != phase
        or row["request_id"] != receipt.request_id
        or row["result"] != receipt.model_dump(mode="json")
        or row["usage"] != receipt.usage.safe_payload()
        or request["job_id"] != job_id
        or request["cache_key"] != key
        or request["payload"].get("version") != EVALUATION_VERSION
    ):
        raise ValueError("evaluation receipt differs from persisted result")
    _check_usage(receipt.usage, receipt.request_id)
    if (
        receipt.usage.has_unknown_billing
        or receipt.usage.actual_cost_usd is None
        or row["actual_cost_usd"] != receipt.usage.actual_cost_usd
        or row["actual_cost_usd"] > row["maximum_cost_usd"]
    ):
        raise ValueError("persisted billing is incomplete or exceeds approved maximum")


def _failed_usage(error: Exception, request_id: str) -> ProviderUsage | None:
    if not isinstance(error, VideoEditorError):
        return None
    try:
        usage = ProviderUsage.model_validate_json(error.safe_details.get("usage", ""))
        _check_usage(usage, request_id)
        return usage
    except ValueError:
        return None


def _finish_request(
    ledger: EvaluationLedger,
    request_id: str,
    receipt: GroundingReceipt | EditorialReceipt | None,
    usage: ProviderUsage | None,
) -> None:
    if usage is not None:
        _check_usage(usage, request_id)
    ledger.finish(
        request_id,
        result=receipt.model_dump(mode="json") if receipt else None,
        usage=usage.safe_payload() if usage else {"outcome": "unknown"},
        actual_cost_usd=usage.actual_cost_usd
        if usage and not usage.has_unknown_billing
        else None,
    )


def _bundle(
    plan: EvaluationPlan,
    approval: EvaluationApproval,
    groundings: list[GroundingReceipt],
    editorial: EditorialReceipt | None,
    stop_reason: Literal["provider_failure", "billing_unknown", "cleanup_failure"]
    | None,
) -> EvaluationBundle:
    audit = EvaluationAudit(
        plan=plan,
        approval=approval,
        groundings=tuple(groundings),
        editorial=editorial,
        complete=stop_reason is None,
        stop_reason=stop_reason,
    )
    return EvaluationBundle(audit=audit, audit_digest=selection_digest(audit))


def _claim_attempt(paths: PathSettings, approval: EvaluationApproval) -> None:
    root = paths.state_dir / "evaluation-attempts"
    identity = hashlib.sha256(approval.approval_id.encode()).hexdigest()
    try:
        if paths.state_dir.is_symlink():
            raise ValueError("evaluation attempt root is unsafe")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        facts = root.lstat()
        if (
            root.is_symlink()
            or not root.is_dir()
            or facts.st_uid != os.getuid()
            or facts.st_mode & 0o077
        ):
            raise ValueError("evaluation attempt root is unsafe")
        descriptor = os.open(
            root / f"{identity}.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w") as stream:
            stream.write(approval.model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError:
        raise ValueError("evaluation attempt already consumed") from None


def run_evaluation(
    *,
    plan: EvaluationPlan | None,
    approval: EvaluationApproval | None,
    provider_factory: Callable[[], EvaluationProvider],
    store: JobStore,
    paths: PathSettings,
) -> EvaluationBundle:
    """Execute only separately approved evaluation; never refine, resume or render."""
    from video_editor.analysis.evaluation_provider import (
        estimate_editorial_cost,
        estimate_grounding_cost,
    )

    require_evaluation_approval(plan, approval)
    assert plan is not None and approval is not None
    plan = EvaluationPlan.model_validate(plan.model_dump(mode="json"))
    pricing = pricing_for_model(plan.model)
    if plan.video_costs != tuple(
        estimate_grounding_cost(i.proposal, pricing) for i in plan.inputs
    ) or plan.editorial_cost != estimate_editorial_cost(pricing):
        raise ValueError("evaluation request cost plan differs from current pricing")
    ledger = EvaluationLedger(store)
    cached: dict[str, GroundingReceipt] = {}
    missing_cost = Decimal(0)
    for item, cost in zip(plan.inputs, plan.video_costs, strict=True):
        _registered_input(item, store)
        with validate_upload_candidate(item.manifest_id, plan.job_id, store, paths):
            pass
        key = _grounding_key(item)
        row = ledger.find_result(plan.job_id, key)
        if row is not None:
            if row["status"] != "completed" or row["result"] is None:
                raise ValueError("occupied grounding result cannot dispatch")
            if (
                row["actual_cost_usd"] is None
                or row["actual_cost_usd"] > row["maximum_cost_usd"]
            ):
                raise ValueError("cached grounding billing exceeds approved maximum")
            receipt = GroundingReceipt.model_validate(row["result"])
            _check_grounding(receipt, item)
            _persisted_receipt(ledger, plan.job_id, key, "video_grounding", receipt)
            cached[key] = receipt
        else:
            occupied = store.connection.execute(
                "SELECT 1 FROM evaluation_requests WHERE job_id = ? AND cache_key = ?",
                (plan.job_id, key),
            ).fetchone()
            if occupied:
                raise ValueError("occupied grounding request cannot dispatch")
            missing_cost += cost
    if missing_cost:
        missing_cost += plan.editorial_cost
    else:
        receipts = tuple(cached[_grounding_key(i)] for i in plan.inputs)
        if any(g.result.factual_support == "supported" for g in receipts):
            key = _editorial_key(plan.inputs, receipts)
            row = ledger.find_result(plan.job_id, key)
            if row is None:
                missing_cost += plan.editorial_cost
            elif row["status"] != "completed" or row["result"] is None:
                raise ValueError("occupied editorial result cannot dispatch")
            elif (
                row["actual_cost_usd"] is None
                or row["actual_cost_usd"] > row["maximum_cost_usd"]
            ):
                raise ValueError("cached editorial billing exceeds approved maximum")
    if store.budget_state(plan.job_id).remaining_usd < missing_cost:
        raise VideoEditorError(
            ErrorCategory.BUDGET,
            "evaluation plan exceeds available budget",
            code="budget_exhausted",
        )
    _claim_attempt(paths, approval)
    provider: EvaluationProvider | None = None
    groundings: list[GroundingReceipt] = []
    for index, (item, cost) in enumerate(
        zip(plan.inputs, plan.video_costs, strict=True)
    ):
        key = _grounding_key(item)
        if key in cached:
            groundings.append(cached[key])
            continue
        request_id = _digest_payload([selection_digest(approval), "ground", index])
        _registered_input(item, store)
        with validate_upload_candidate(
            item.manifest_id, plan.job_id, store, paths
        ) as authorization:
            ledger.reserve(
                request_id=request_id,
                job_id=plan.job_id,
                cache_key=key,
                phase="video_grounding",
                maximum_cost_usd=cost,
                payload={
                    "version": EVALUATION_VERSION,
                    "approval": approval.model_dump(mode="json"),
                    "input": item.model_dump(mode="json"),
                },
            )
            ledger.mark_dispatched(request_id)
            upload = None
            stop: (
                Literal["provider_failure", "billing_unknown", "cleanup_failure"] | None
            ) = None
            returned_usage: ProviderUsage | None = None
            try:
                if provider is None:
                    provider = provider_factory()
                upload = provider.upload(authorization)
                if upload.manifest_id != item.manifest_id or upload.state != "ACTIVE":
                    raise ValueError("grounding upload identity differs")
                response = provider.ground(
                    upload, request_id=request_id, proposal=item.proposal
                )
                _check_usage(response.usage, request_id)
                returned_usage = response.usage
                receipt = GroundingReceipt(
                    input=item,
                    cache_key=key,
                    request_id=request_id,
                    upload=upload,
                    result=response.response,
                    usage=response.usage,
                )
                _check_grounding(receipt, item)
                _check_usage(receipt.usage, request_id)
            except Exception as error:  # noqa: BLE001 - transport failures must retain holds
                _finish_request(
                    ledger,
                    request_id,
                    None,
                    returned_usage or _failed_usage(error, request_id),
                )
                stop = "provider_failure"
            else:
                _finish_request(ledger, request_id, receipt, receipt.usage)
                groundings.append(receipt)
                if (
                    receipt.usage.has_unknown_billing
                    or receipt.usage.actual_cost_usd is None
                ):
                    stop = "billing_unknown"
            finally:
                if upload is not None and provider is not None:
                    try:
                        provider.delete_upload(upload)
                    except Exception:  # noqa: BLE001 - cleanup cannot hide billing state
                        stop = stop or "cleanup_failure"
            if stop is not None:
                return _bundle(plan, approval, groundings, None, stop)
    if not any(g.result.factual_support == "supported" for g in groundings):
        return _bundle(plan, approval, groundings, None, None)
    key = _editorial_key(plan.inputs, tuple(groundings))
    row = ledger.find_result(plan.job_id, key)
    if row is not None:
        if (
            row["actual_cost_usd"] is None
            or row["actual_cost_usd"] > row["maximum_cost_usd"]
        ):
            raise ValueError("cached editorial billing exceeds approved maximum")
        editorial = EditorialReceipt.model_validate(row["result"])
        _persisted_receipt(ledger, plan.job_id, key, "editorial_comparison", editorial)
    else:
        request_id = _digest_payload([selection_digest(approval), "editorial"])
        ledger.reserve(
            request_id=request_id,
            job_id=plan.job_id,
            cache_key=key,
            phase="editorial_comparison",
            maximum_cost_usd=plan.editorial_cost,
            payload={
                "version": EVALUATION_VERSION,
                "approval": approval.model_dump(mode="json"),
                "grounding_digests": [selection_digest(g.result) for g in groundings],
            },
        )
        ledger.mark_dispatched(request_id)
        comparison_usage: ProviderUsage | None = None
        try:
            if provider is None:
                provider = provider_factory()
            comparison = provider.compare(
                request_id=request_id,
                set_digest=evaluation_set_digest(plan.inputs),
                groundings=tuple(g.result for g in groundings),
            )
            _check_usage(comparison.usage, request_id)
            comparison_usage = comparison.usage
            editorial = EditorialReceipt(
                cache_key=key,
                request_id=request_id,
                result=comparison.response,
                usage=comparison.usage,
            )
            validate_editorial(
                editorial.result,
                evaluation_set_digest(plan.inputs),
                {
                    g.result.proposal_digest: (
                        g.result.factual_support,
                        {o.observation_id for o in g.result.observations},
                    )
                    for g in groundings
                },
            )
            _check_usage(editorial.usage, request_id)
        except Exception as error:  # noqa: BLE001 - transport failures must retain holds
            _finish_request(
                ledger,
                request_id,
                None,
                comparison_usage or _failed_usage(error, request_id),
            )
            return _bundle(plan, approval, groundings, None, "provider_failure")
        _finish_request(ledger, request_id, editorial, editorial.usage)
        if (
            editorial.usage.has_unknown_billing
            or editorial.usage.actual_cost_usd is None
        ):
            return _bundle(plan, approval, groundings, editorial, "billing_unknown")
    return _bundle(plan, approval, groundings, editorial, None)


def _verified_audit(
    bundle: object, store: JobStore | None, paths: PathSettings | None
) -> EvaluationAudit:
    if (
        not isinstance(bundle, EvaluationBundle)
        or not bundle.complete
        or bundle.audit.stop_reason is not None
    ):
        raise ValueError("complete evaluation bundle required")
    bundle = EvaluationBundle.model_validate(bundle.model_dump(mode="json"))
    audit = bundle.audit
    if bundle.audit_digest != selection_digest(audit):
        raise ValueError("evaluation audit integrity differs")
    if store is None or paths is None:
        raise ValueError("registered persistence required for complete bundle")
    require_evaluation_approval(audit.plan, audit.approval)
    if len(audit.groundings) != len(audit.plan.inputs):
        raise ValueError("complete grounding set required")
    ledger = EvaluationLedger(store)
    for item, receipt in zip(audit.plan.inputs, audit.groundings, strict=True):
        _registered_input(item, store)
        with validate_upload_candidate(
            item.manifest_id, audit.plan.job_id, store, paths
        ) as authorization:
            digest = hashlib.sha256()
            while block := authorization.stream.read(1024 * 1024):
                digest.update(block)
            if digest.hexdigest() != item.manifest.file_digest_sha256:
                raise ValueError("registered evidence digest differs")
        _check_grounding(receipt, item)
        _persisted_receipt(
            ledger, audit.plan.job_id, _grounding_key(item), "video_grounding", receipt
        )
    supported = any(g.result.factual_support == "supported" for g in audit.groundings)
    if supported != (audit.editorial is not None):
        raise ValueError("complete editorial phase required exactly for supported set")
    if audit.editorial is not None:
        key = _editorial_key(audit.plan.inputs, audit.groundings)
        if audit.editorial.cache_key != key:
            raise ValueError("editorial cache identity differs")
        _persisted_receipt(
            ledger, audit.plan.job_id, key, "editorial_comparison", audit.editorial
        )
        validate_editorial(
            audit.editorial.result,
            evaluation_set_digest(audit.plan.inputs),
            {
                g.result.proposal_digest: (
                    g.result.factual_support,
                    {o.observation_id for o in g.result.observations},
                )
                for g in audit.groundings
            },
        )
    return audit


def evaluated_reviews(
    bundle: object, *, store: JobStore, paths: PathSettings
) -> tuple[SelectionReview, ...]:
    """Bridge only registered persisted receipts into legacy pure gate mechanics."""
    audit = _verified_audit(bundle, store, paths)
    decisions = (
        {d.proposal_digest: d for d in audit.editorial.result.decisions}
        if audit.editorial
        else {}
    )
    reviews = []
    for receipt in audit.groundings:
        p, result = receipt.input.proposal, receipt.result
        decision = decisions.get(result.proposal_digest)
        reviews.append(
            SelectionReview(
                proposal_digest=result.proposal_digest,
                evaluator=audit.plan.model,
                evaluator_version=EVALUATION_VERSION,
                observations=tuple(o.description for o in result.observations),
                factual_support=result.factual_support,
                editorial=decision.disposition
                if decision
                else (
                    "reject" if result.factual_support == "contradicted" else "primary"
                ),
                reason=decision.reason
                if decision
                else (
                    "description_contradicted"
                    if result.factual_support == "contradicted"
                    else "factual_support_unknown"
                ),
                alternative_to=decision.alternative_to if decision else (),
                evidence=(
                    SelectionEvidence(
                        path=Path(receipt.input.manifest.artifact_path),
                        sha256=receipt.input.manifest.file_digest_sha256,
                        scope="video",
                        source_start=p.source_start,
                        source_end=p.source_end,
                    ),
                ),
            )
        )
    return tuple(reviews)


def admit_completed_bundle(
    bundle: object, *, store: JobStore | None = None, paths: PathSettings | None = None
) -> SelectionReport:
    """Admit only complete bundles after current bytes and persisted provenance checks."""
    _verified_audit(bundle, store, paths)
    assert (
        isinstance(bundle, EvaluationBundle) and store is not None and paths is not None
    )
    return evaluate_selection(
        tuple(i.proposal for i in bundle.audit.plan.inputs),
        evaluated_reviews(bundle, store=store, paths=paths),
    )


def exact_offset(value: Decimal) -> str:
    """Refuse provider timestamp rounding at the nanosecond boundary."""
    if not value.is_finite() or value < 0:
        raise ValueError("invalid provider interval")
    normalized = value.normalize()
    if int(normalized.as_tuple().exponent) < -9:
        raise ValueError(
            "interval is not exactly representable at nanosecond precision"
        )
    return f"{format(value, 'f')}s"
