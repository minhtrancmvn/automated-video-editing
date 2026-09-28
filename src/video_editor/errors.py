"""Stable error types for video editor operations."""

from enum import StrEnum


class ErrorCategory(StrEnum):
    """Top-level category for user-visible editor failures."""

    CONFIGURATION = "configuration"
    STORAGE = "storage"
    INSPECTION = "inspection"
    PLAN = "plan_validation"
    RENDER = "rendering"
    OUTPUT = "output_validation"
    STATE = "state"
    ANALYSIS = "analysis"
    PROVIDER = "provider"
    BUDGET = "budget"


class VideoEditorError(Exception):
    """Expected video editor failure with stable category metadata."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        code: str | None = None,
        safe_details: dict[str, str] | None = None,
        interrupted: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.code = code
        self.safe_details = dict(safe_details or {})
        self.interrupted = interrupted
