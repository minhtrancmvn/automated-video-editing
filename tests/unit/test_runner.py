import signal
import threading
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


def test_runner_uses_shell_free_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    monkeypatch.setattr("video_editor.rendering.runner.validate_output", lambda *_args: None)

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
    monkeypatch.setattr("video_editor.rendering.runner.validate_output", lambda *_args: None)
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
