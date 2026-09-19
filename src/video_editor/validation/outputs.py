"""Validate rendered media before final artifact publication."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.media.probe import MediaProbe, probe_media
from video_editor.models.edit_plan import OutputSpec


def validate_output(
    path: Path,
    output: OutputSpec,
    expected_duration: Decimal,
    tolerance: Decimal = Decimal("0.20"),
) -> MediaProbe:
    """Probe output and enforce dimensions, duration, and audio policy."""
    if not path.is_file():
        raise VideoEditorError(ErrorCategory.OUTPUT, f"output does not exist: {path}")
    try:
        probe = probe_media(path)
    except VideoEditorError as exc:
        raise VideoEditorError(ErrorCategory.OUTPUT, str(exc)) from exc
    if probe.video is None:
        raise VideoEditorError(ErrorCategory.OUTPUT, f"output has no video stream: {path}")
    if (probe.video.width, probe.video.height) != (output.width, output.height):
        raise VideoEditorError(
            ErrorCategory.OUTPUT,
            f"output dimensions are {probe.video.width}x{probe.video.height}; "
            f"expected {output.width}x{output.height}",
        )
    if probe.duration is None or abs(Decimal(str(probe.duration)) - expected_duration) > tolerance:
        raise VideoEditorError(
            ErrorCategory.OUTPUT,
            f"output duration is {probe.duration}; expected {expected_duration} ± {tolerance}",
        )
    if output.audio == "source" and probe.audio is None:
        raise VideoEditorError(ErrorCategory.OUTPUT, "output must contain source audio")
    if output.audio == "none" and probe.audio is not None:
        raise VideoEditorError(ErrorCategory.OUTPUT, "output must not contain audio")
    return probe
