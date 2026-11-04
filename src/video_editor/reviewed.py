"""Explicit local reviewed-draft entry points; no provider or ledger writes."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Self

from video_editor.config import resolve_config
from video_editor.persistence.database import JobStore


class _ReadOnlyStore(JobStore):
    """Read an existing job ledger without schema creation or migration."""

    def __enter__(self) -> Self:
        uri = self.db_path.resolve(strict=True).as_uri() + "?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA query_only = ON")
        self._connection.execute("BEGIN")
        return self


def _fresh_file(path: Path, *, protected_roots: tuple[Path, ...] = ()) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("reviewed output already exists")
    if not path.parent.is_dir():
        raise ValueError("reviewed output parent must exist")
    if any(parent.is_symlink() for parent in path.absolute().parents):
        raise ValueError("reviewed output must not use symlink components")
    resolved = path.resolve()
    if any(resolved.is_relative_to(root.resolve()) for root in protected_roots):
        raise ValueError("reviewed output overlaps protected tree")


def prepare_reviewed(
    *, bundle_path: Path, config_path: Path, output_path: Path
) -> dict[str, Any]:
    """Prepare a local draft from existing evidence; refuse output replacement."""
    _fresh_file(output_path)
    from video_editor.analysis.evaluation import EvaluationBundle
    from video_editor.evaluated_workflow import (
        ReviewedWorkflowService,
        reviewed_draft_digest,
    )

    bundle = EvaluationBundle.model_validate_json(bundle_path.read_text())
    config = resolve_config(config_path)
    _fresh_file(
        output_path,
        protected_roots=(
            config.paths.input_dir,
            config.paths.state_dir,
            config.paths.cache_dir,
            config.paths.workspace_dir,
            *(
                Path(item.manifest.source_path).parent
                for item in bundle.audit.plan.inputs
            ),
        ),
    )
    with _ReadOnlyStore(config.paths.state_dir / "jobs.sqlite3") as store:
        service = ReviewedWorkflowService(config, store, evaluation_bundle=bundle)
        draft = service.prepare_reviewed_plans()
    _fresh_file(
        output_path,
        protected_roots=(
            config.paths.input_dir,
            config.paths.state_dir,
            config.paths.cache_dir,
            config.paths.workspace_dir,
            *(source.path.parent for plan in draft.plans for source in plan.sources),
            *(
                Path(item.manifest.source_path).parent
                for item in bundle.audit.plan.inputs
            ),
        ),
    )
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(draft.model_dump_json(indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "draft": str(output_path),
        "draft_digest": reviewed_draft_digest(draft),
        "render_authorized": False,
        "paid_dispatch_authorized": False,
    }


def render_reviewed(
    *,
    draft_path: Path,
    bundle_path: Path,
    approval_path: Path | None,
    config_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Require exact local render approval before opening configuration or ledger."""
    if approval_path is None:
        raise ValueError("exact draft approval required")
    from video_editor.analysis.evaluation import EvaluationBundle
    from video_editor.evaluated_workflow import (
        ReviewedDraft,
        ReviewedRenderApproval,
        ReviewedWorkflowService,
        reviewed_draft_digest,
    )

    draft = ReviewedDraft.model_validate_json(draft_path.read_text())
    approval = ReviewedRenderApproval.model_validate_json(approval_path.read_text())
    if approval.draft_digest != reviewed_draft_digest(draft):
        raise ValueError("exact draft approval required")
    bundle = EvaluationBundle.model_validate_json(bundle_path.read_text())
    config = resolve_config(config_path)
    with _ReadOnlyStore(config.paths.state_dir / "jobs.sqlite3") as store:
        service = ReviewedWorkflowService(config, store, evaluation_bundle=bundle)
        return service.render_reviewed_plans(draft, approval, output_path)
