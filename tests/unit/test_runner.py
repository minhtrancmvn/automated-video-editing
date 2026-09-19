import signal
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

from video_editor.errors import ErrorCategory, VideoEditorError
from video_editor.models.edit_plan import OutputSpec
from video_editor.rendering.compiler import RenderCommand
from video_editor.rendering.runner import run_render


def _command(tmp_path: Path) -> RenderCommand:
    return RenderCommand(
        args=("ffmpeg", "-version"),
        partial_path=tmp_path / "short.mp4.partial",
        final_path=tmp_path / "short.mp4",
        expected_duration=Decimal(1),
        output=OutputSpec(
            kind="short",
            width=16,
            height=16,
            frame_rate=Decimal(16),
            codec="h264",
            audio="none",
        ),
    )


def test_runner_uses_shell_free_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = _command(tmp_path)
    captured: dict[str, object] = {}

    class Process:
        def wait(self, timeout: float | None = None) -> int:
            return 0

        def poll(self) -> None:
            return None

    def popen(args: tuple[str, ...], *, shell: bool) -> Process:
        captured["args"] = args
        captured["shell"] = shell
        command.partial_path.write_bytes(b"partial")
        return Process()

    monkeypatch.setattr("video_editor.rendering.runner.subprocess.Popen", popen)
    monkeypatch.setattr(
        "video_editor.rendering.runner.validate_output", lambda *_args: None
    )

    run_render(command, lambda: None)

    assert captured == {"args": command.args, "shell": False}
    assert command.final_path.exists()
    assert not command.partial_path.exists()


def test_runner_keeps_partial_when_ffmpeg_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = _command(tmp_path)

    class Process:
        def wait(self, timeout: float | None = None) -> int:
            return 1

        def poll(self) -> None:
            return None

    def popen(*_args: object, **_kwargs: object) -> Process:
        command.partial_path.write_bytes(b"partial")
        return Process()

    monkeypatch.setattr("video_editor.rendering.runner.subprocess.Popen", popen)

    with pytest.raises(VideoEditorError, match="partial output retained") as exc:
        run_render(command, lambda: None)

    assert exc.value.category == ErrorCategory.RENDER
    assert command.partial_path.exists()
    assert not command.final_path.exists()


def test_runner_terminates_on_main_thread_interrupt_and_keeps_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = _command(tmp_path)
    handlers: dict[signal.Signals, object] = {}
    callback_called = False

    class Process:
        def __init__(self) -> None:
            self.terminated = False

        def wait(self, timeout: float | None = None) -> int:
            handlers[signal.SIGINT](signal.SIGINT, None)
            return 0

        def poll(self) -> int | None:
            return None if not self.terminated else 0

        def terminate(self) -> None:
            self.terminated = True

    process = Process()

    def popen(*_args: object, **_kwargs: object) -> Process:
        command.partial_path.write_bytes(b"partial")
        return process

    def install(signum: signal.Signals, handler: object) -> object:
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    def on_interrupt() -> None:
        nonlocal callback_called
        callback_called = True

    monkeypatch.setattr("video_editor.rendering.runner.subprocess.Popen", popen)
    monkeypatch.setattr("video_editor.rendering.runner.signal.signal", install)
    monkeypatch.setattr(
        "video_editor.rendering.runner.signal.getsignal", lambda _signum: None
    )

    with pytest.raises(VideoEditorError, match="partial output retained") as caught:
        run_render(command, on_interrupt)

    assert caught.value.interrupted
    assert callback_called
    assert process.terminated
    assert command.partial_path.exists()
    assert not command.final_path.exists()


def test_runner_rejects_second_process_for_same_output(
    tmp_path: Path,
) -> None:
    script = """
import time
from pathlib import Path
from video_editor.rendering.compiler import RenderCommand
from video_editor.rendering.runner import run_render
from decimal import Decimal
command = RenderCommand(("python", "-c", "import time; time.sleep(0.4)"), Path(r"%s"), Path(r"%s"), Decimal(1), None)
try:
    run_render(command, lambda: None)
except Exception as exc:
    print(type(exc).__name__, str(exc), flush=True)
""" % (tmp_path / "same.mp4.partial", tmp_path / "same.mp4")
    first = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    time.sleep(0.1)
    second = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    first.wait(timeout=3)
    assert "render already in progress" in second.stdout


def test_runner_interrupt_after_validation_keeps_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = _command(tmp_path)
    handlers: dict[signal.Signals, object] = {}

    class Process:
        def wait(self, timeout: float | None = None) -> int:
            command.partial_path.write_bytes(b"partial")
            return 0

        def poll(self) -> None:
            return None

    monkeypatch.setattr("video_editor.rendering.runner.subprocess.Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr("video_editor.rendering.runner.validate_output", lambda *_args: None)
    monkeypatch.setattr("video_editor.rendering.runner.signal.getsignal", lambda _signum: None)

    def install(signum: signal.Signals, handler: object) -> object:
        handlers[signum] = handler
        if signum == signal.SIGINT and callable(handler):
            handler(signal.SIGINT, None)
        return None

    monkeypatch.setattr("video_editor.rendering.runner.signal.signal", install)

    with pytest.raises(VideoEditorError, match="interrupted") as caught:
        run_render(command, lambda: None)

    interrupted = caught.value.interrupted
    assert interrupted
    assert command.partial_path.exists()
    assert not command.final_path.exists()


def test_runner_skips_signal_registration_outside_main_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = _command(tmp_path)
    signal_calls: list[signal.Signals] = []

    class Process:
        def wait(self, timeout: float | None = None) -> int:
            return 0

        def poll(self) -> None:
            return None

    def popen(*_args: object, **_kwargs: object) -> Process:
        command.partial_path.write_bytes(b"partial")
        return Process()

    monkeypatch.setattr("video_editor.rendering.runner.subprocess.Popen", popen)
    monkeypatch.setattr(
        "video_editor.rendering.runner.validate_output", lambda *_args: None
    )
    monkeypatch.setattr(
        "video_editor.rendering.runner.signal.signal",
        lambda signum, _handler: signal_calls.append(signum),
    )

    thread = threading.Thread(target=run_render, args=(command, lambda: None))
    thread.start()
    thread.join()

    assert not thread.is_alive()
    assert signal_calls == []
    assert command.final_path.exists()
