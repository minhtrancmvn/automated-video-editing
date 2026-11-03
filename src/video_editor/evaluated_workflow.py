"""Explicit bundle-backed workflow admission; never invoked by ordinary resume."""

from __future__ import annotations

from collections.abc import Callable

from video_editor.analysis.evaluation import EvaluationBundle, evaluated_reviews
from video_editor.analysis.models import AnalysisProvider
from video_editor.analysis.selection import RefinementApproval, SelectionReview
from video_editor.config import AppConfig
from video_editor.persistence.database import JobStore
from video_editor.workflow import WorkflowService


class EvaluatedWorkflowService(WorkflowService):
    """Revalidate registered evaluator provenance before each selection access.

    Construction grants no paid refinement or render permission. Inherited final
    selection denial and separate refinement approval remain unchanged.
    """

    def __init__(
        self,
        config: AppConfig,
        store: JobStore,
        progress: Callable[[str], None] | None = None,
        *,
        evaluation_bundle: EvaluationBundle,
        provider: AnalysisProvider | None = None,
        refinement_approval: RefinementApproval | None = None,
    ) -> None:
        # Base constructor assigns its default reviews through the override setter.
        self._reviews_initialized = False
        super().__init__(
            config,
            store,
            progress,
            provider=provider,
            selection_reviews=(),
            refinement_approval=refinement_approval,
        )
        self._evaluation_bundle = evaluation_bundle
        self._reviews_initialized = True
        # Provenance validation waits until selection_reviews is accessed, after
        # callers open JobStore. Every access revalidates ledger and media state.

    @property
    def selection_reviews(self) -> tuple[SelectionReview, ...]:
        return evaluated_reviews(
            self._evaluation_bundle, store=self.store, paths=self.config.paths
        )

    @selection_reviews.setter
    def selection_reviews(self, value: tuple[SelectionReview, ...]) -> None:
        # The base constructor assigns its empty default before config/store.
        if value or self._reviews_initialized:
            raise ValueError(
                "legacy review override is forbidden for evaluated workflow"
            )
        self._reviews_initialized = True
