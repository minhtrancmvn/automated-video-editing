"""Run compiled renders with safe interruption and finalization."""

from __future__ import annotations

import fcntl
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.rendering.compiler import RenderCommand
from video_editor.validation.outputs import validate_output


@contextmanager
def _render_lease(path: Path) -> Iterator[None]:
    """Hold an OS lease so separate editor processes cannot render same output."""
    lease_path = path.with_name(path.name + ".lock")
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    with lease_path.open("a+") as lease:
        try:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise VideoEditorError(
                ErrorCategory.RENDER,
                f"render already in progress for {path}",
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lease.fileno(), fcntl.LOCK_UN)


_POLL_INTERVAL_SECONDS = 0.1
_INTERRUPT_GRACE_SECONDS = 10.0
_PUBLICATION_SIGNALS = {signal.SIGINT, signal.SIGTERM}


@contextmanager
def _defer_publication_signals() -> Iterator[None]:
    """Defer process interruption through rename and artifact persistence."""
    if threading.current_thread() is threading.main_thread() and hasattr(
        signal, "pthread_sigmask"
    ):
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _PUBLICATION_SIGNALS)
        try:
            yield
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            # Python delivers deferred signals immediately after unmasking. The
            # installed handler only records interruption, so caller checks it
            # after this context exits and before restoring handlers.
    else:
        yield


def run_render(
    command: RenderCommand,
    on_interrupt: Callable[[], None],
    on_publish: Callable[[Path], None] | None = None,
) -> None:
    """Execute render, preserving partial output when interrupted or failed."""
    command.partial_path.parent.mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen[bytes] | None = None
    interrupted = False
    interrupt_notified = False
    previous: dict[signal.Signals, object] = {}

    def handle_signal(_signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    def raise_if_interrupted() -> None:
        nonlocal interrupt_notified
        if interrupted:
            if not interrupt_notified:
                interrupt_notified = True
                on_interrupt()
            raise VideoEditorError(
                ErrorCategory.RENDER,
                f"render interrupted; partial output retained at {command.partial_path}",
                interrupted=True,
            )

    def wait_for_exit() -> int:
        assert process is not None
        terminated = False
        deadline: float | None = None
        while True:
            if interrupted and not terminated:
                process.terminate()
                terminated = True
                deadline = time.monotonic() + _INTERRUPT_GRACE_SECONDS
            try:
                return_code = process.wait(timeout=_POLL_INTERVAL_SECONDS)
            except subprocess.TimeoutExpired:
                if deadline is not None and time.monotonic() >= deadline:
                    process.kill()
                    return process.wait()
                continue
            if interrupted and not terminated:
                # Signal arrived while wait was blocked. Send SIGTERM before
                # accepting process exit so stubborn children still get bounded
                # shutdown handling.
                continue
            return return_code

    try:
        with _render_lease(command.final_path):
            # Python only permits signal handlers in main thread. Worker-thread callers
            # still execute shell-free and retain partial artifacts, without mutating
            # process-global signal state.
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous[signum] = signal.getsignal(signum)
                    signal.signal(signum, handle_signal)
            try:
                try:
                    process = subprocess.Popen(command.args, shell=False)
                except OSError as exc:
                    raise VideoEditorError(
                        ErrorCategory.RENDER, f"cannot start ffmpeg: {exc}"
                    ) from exc
                return_code = wait_for_exit()
                raise_if_interrupted()
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
                with _defer_publication_signals():
                    raise_if_interrupted()
                    command.partial_path.replace(command.final_path)
                    if on_publish is not None:
                        on_publish(command.final_path)
                # Unmasking delivers any deferred SIGINT/SIGTERM while our
                # interruption handler remains installed. Persist interrupted
                # state before outer finally restores previous handlers.
                raise_if_interrupted()
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, cast(signal.Handlers, handler))
    finally:
        # Lease context owns unlock on every success, failure, and interruption path.
        pass
