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


class VideoEditorError(Exception):
    """Expected video editor failure with stable category metadata."""

    def __init__(
        self, category: ErrorCategory, message: str, *, interrupted: bool = False
    ) -> None:
        super().__init__(message)
        self.category = category
        self.interrupted = interrupted
