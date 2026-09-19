"""Run compiled renders with safe interruption and finalization."""

from __future__ import annotations

import signal
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.rendering.compiler import RenderCommand
from video_editor.validation.outputs import validate_output


def run_render(command: RenderCommand, on_interrupt: Callable[[], None]) -> None:
    """Execute render, preserving partial output when interrupted or failed."""
    command.partial_path.parent.mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen[bytes] | None = None
    interrupted = False
    previous: dict[signal.Signals, object] = {}

    def handle_signal(signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True
        if process is not None and process.poll() is None:
            process.terminate()

    try:
        # Python only permits signal handlers in main thread. Worker-thread callers
        # still execute shell-free and retain partial artifacts, without mutating
        # process-global signal state.
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, handle_signal)
        try:
            process = subprocess.Popen(command.args, shell=False)
        except OSError as exc:
            raise VideoEditorError(
                ErrorCategory.RENDER, f"cannot start ffmpeg: {exc}"
            ) from exc
        try:
            return_code = process.wait(timeout=10 if interrupted else None)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait()
        if interrupted:
            on_interrupt()
            raise VideoEditorError(
                ErrorCategory.RENDER,
                f"render interrupted; partial output retained at {command.partial_path}",
                interrupted=True,
            )
        if return_code != 0:
            raise VideoEditorError(
                ErrorCategory.RENDER,
                f"ffmpeg exited with status {return_code}; partial output retained at "
                f"{command.partial_path}",
            )
        if not command.partial_path.exists():
            raise VideoEditorError(
                ErrorCategory.RENDER,
                f"ffmpeg produced no partial output at {command.partial_path}",
            )
        if command.output is not None:
            validate_output(
                command.partial_path,
                command.output,
                command.expected_duration,
            )
        Path(command.partial_path).replace(command.final_path)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, cast(signal.Handlers, handler))
