# Task 5 Report

Status: Complete

Base implementation: `79f8c75` (`feat: inspect media and host capabilities`)

Review fixes: `cf3b158` (`fix: harden media inspection warnings`)

Implemented typed Pydantic stream/probe/warning models, safe ffprobe JSON parsing, rational frame-rate parsing, HEVC/10-bit/rotation/HDR/VFR/missing-audio/unknown-color warnings, batch warning deduplication, argument-vector subprocess calls with `shell=False`, FFmpeg/ffprobe version detection, encoder/hardware detection, Apple Silicon architecture and macOS memory detection. VideoToolbox is marked available only; no capability proof claimed.

Review remediation:

- `_encoders` now accepts real six-flag FFmpeg video rows, including `V....D h264_videotoolbox` and `V....D libx264`; fixture test added.
- Canonical non-informative color values `unknown`, `unspecified`, and `reserved` produce `unknown_color` warnings while retaining raw stream metadata.
- Batch-friendly stable warnings now report incomplete successful ffprobe payloads: `no_video`, `missing_format`, and `missing_video_metadata`. Empty stream and audio-only payloads remain inspectable and preserve their applicable missing-audio warning behavior.

Tests/checks:

- Focused: `uv run pytest tests/unit/test_probe.py tests/unit/test_capabilities.py -v` — 14 passed.
- Full: `uv run pytest -v` — 52 passed.
- Lint: `uv run ruff check src tests` — passed.
- Types: `uv run mypy src` — passed.
- Whitespace: `git diff --check` — passed before commit.
