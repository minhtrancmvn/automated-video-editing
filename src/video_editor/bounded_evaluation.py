"""Explicit evaluator preflight and dispatch; never run ordinary workflow stages."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from video_editor.analysis.evaluation import (
    EvaluationApproval,
    EvaluationPlan,
    require_evaluation_approval,
    run_evaluation,
)
from video_editor.analysis.evaluation_provider import (
    GeminiEvaluationProvider,
    estimate_editorial_cost,
    estimate_grounding_cost,
)
from video_editor.analysis.pricing import pricing_for_model
from video_editor.analysis.selection import selection_digest
from video_editor.config import load_gemini_api_key, resolve_config
from video_editor.persistence.database import JobStore


def evaluation_preflight(plan: EvaluationPlan) -> dict[str, Any]:
    """Price exported data only: no config, DB, media, state or credential access."""
    pricing = pricing_for_model(plan.model)
    if plan.video_costs != tuple(
        estimate_grounding_cost(item.proposal, pricing) for item in plan.inputs
    ) or plan.editorial_cost != estimate_editorial_cost(pricing):
        raise ValueError("evaluation request cost plan differs from current pricing")
    return {
        "dispatch": False,
        "version": plan.version,
        "job_id": plan.job_id,
        "plan_digest": selection_digest(plan),
        "model": plan.model,
        "sampled_fps": plan.fps,
        "continuous_playback_verified": False,
        "audio_supplied": True,
        "output_tokens": plan.output_tokens,
        "input_bytes": plan.input_bytes,
        "maximum_generations": plan.maximum_generations,
        "maximum_cost_usd": str(plan.maximum_cost_usd),
        "editorial_maximum_cost_usd": str(plan.editorial_cost),
        "cache_checked": False,
        "registered_media_checked": False,
        "requests": [
            {
                "proposal_digest": selection_digest(item.proposal),
                "manifest_id": item.manifest_id,
                "proxy_sha256": item.manifest.file_digest_sha256,
                "upload_proxy_interval": [
                    str(item.manifest.proxy_start),
                    str(item.manifest.proxy_end),
                ],
                "upload_source_interval": [
                    str(item.manifest.source_start),
                    str(item.manifest.source_end),
                ],
                "review_proxy_interval": [
                    str(item.proposal.broad.start),
                    str(item.proposal.broad.end),
                ],
                "review_source_interval": [
                    str(item.proposal.source_start),
                    str(item.proposal.source_end),
                ],
                "maximum_cost_usd": str(cost),
            }
            for item, cost in zip(plan.inputs, plan.video_costs, strict=True)
        ],
        "render_ready": False,
        "paid_dispatch_authorized": False,
    }


def bounded_evaluation(
    *,
    plan_path: Path,
    dispatch: bool = False,
    approval_path: Path | None = None,
    config_path: Path | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Dispatch requires exact approval plus existing job and exclusive audit output."""
    plan = EvaluationPlan.model_validate_json(plan_path.read_bytes())
    preflight = evaluation_preflight(plan)
    if not dispatch:
        return preflight
    if approval_path is None:
        raise ValueError("exact evaluation approval is required for dispatch")
    approval = EvaluationApproval.model_validate_json(approval_path.read_bytes())
    require_evaluation_approval(plan, approval)
    if config_path is None or output_path is None:
        raise ValueError("dispatch requires --config and new --output audit path")
    api_key = load_gemini_api_key(os.environ)
    config = resolve_config(config_path)
    if not config.cloud_enabled or config.gemini.model != plan.model:
        raise ValueError("evaluation requires cloud mode and exact approved model")
    database = config.paths.state_dir / "jobs.sqlite3"
    if not database.is_file():
        raise ValueError("existing registered evaluation job database required")

    def provider_factory() -> GeminiEvaluationProvider:
        return GeminiEvaluationProvider(
            pricing=pricing_for_model(plan.model), api_key=api_key
        )

    # Reserve and test audit storage before any claim or provider dispatch. A
    # pending marker is not a result; on failure it must not trigger a rerun.
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write('{"complete":false,"status":"pending"}\n')
        stream.flush()
        os.fsync(stream.fileno())
        with JobStore(database) as store:
            bundle = run_evaluation(
                plan=plan,
                approval=approval,
                provider_factory=provider_factory,
                store=store,
                paths=config.paths,
            )
        stream.seek(0)
        stream.write(bundle.model_dump_json())
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "dispatch": True,
        "complete": bundle.complete,
        "stop_reason": bundle.audit.stop_reason,
        "audit_digest": bundle.audit_digest,
        "render_ready": False,
        "paid_dispatch_authorized": False,
    }
