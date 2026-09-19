# Local Video Editor

Phase 1 is local-first workflow foundation. It discovers and inspects video, builds deterministic sample plans, renders validated horizontal and vertical MP4 files, persists job state, and emits reports. It does not provide automatic highlight intelligence, storytelling, transcription, captions, subject tracking, or content-aware reframing.

Real HERO12 validation remains pending. Do not treat synthetic fixture success as real-camera support.

## Prerequisites

Apple Silicon macOS setup:

```bash
brew install ffmpeg uv
uv sync
```

Python 3.12 or newer is required. `ffmpeg` and `ffprobe` must resolve on `PATH`. Run `uv run pytest -v` to verify local dependencies and generated media fixtures.

## Configure storage

Copy example config and edit paths:

```bash
cp config.example.toml config.toml
```

Example external SSD layout:

```toml
[paths]
input_dir = "/Volumes/TravelSSD/footage"
workspace_dir = "/Volumes/TravelSSD/video-editor/workspace"
cache_dir = "/Volumes/TravelSSD/video-editor/cache"
output_dir = "/Volumes/TravelSSD/video-editor/output"
state_dir = "~/Library/Application Support/video-editor"

[settings]
storage_reserve_bytes = 10737418240
cloud_enabled = false
render_concurrency = 1
```

`render_concurrency` remains validated for configuration compatibility, but Phase 1 deliberately ignores values above one and serializes render execution with one process at a time. This preserves output-volume safety; do not expect parallel renders until later phase documentation changes.

Input, workspace, cache, output, and state paths are independent. Keep large proxies, extracted audio, temporary renders, and final outputs on external SSD. Keep SQLite state on internal storage or another durable location. APFS and exFAT paths are supported; workflow does not rely on cross-volume atomic renames.

Missing or changed external volumes fail loudly. No internal fallback directory is created. Write-heavy stages check recorded volume identity and free space. Originals are read-only inputs from workflow perspective and are never edited or deleted. Cleanup, when performed manually, must stay inside configured cache/workspace descendants; never clean input roots.

Phase 1 has no runtime environment variables. `.env.example` records this boundary; TOML is source of configuration. No cloud or network calls occur. `cloud_enabled = true` is rejected.

## CLI

All commands require existing readable config:

```bash
video-editor inspect INPUT_PATH --config CONFIG
video-editor plan INPUT_PATH OUTPUT_PATH --config CONFIG
video-editor render-from-plan PLAN_PATH --config CONFIG
video-editor run INPUT_PATH --config CONFIG
video-editor status JOB_ID --config CONFIG
video-editor resume JOB_ID --config CONFIG
```

`plan` uses positional `OUTPUT_PATH`, and it must equal configured `output_dir`. One-command acceptance path:

```bash
video-editor run /Volumes/TravelSSD/footage --config config.toml
```

`run` executes `inspect`, `proxy`, `plan`, `render`, `validate`, and `report`. It creates `<output_dir>/<job_id>/`, with:

```text
edit-plan-horizontal.json
edit-plan-vertical.json
long.mp4
short-01.mp4
report.json
report.md
```

Plans use `phase1-sample-v1` provenance and select first bounded samples, not highlights. Horizontal output is 1920×1080. Vertical output is 1080×1920. Long-form timelines must stay strictly below 60 minutes. `status` shows stage and artifact state. `resume` reuses matching valid stages and does not duplicate valid renders.

Supported recursive extensions: `.3gp`, `.avi`, `.m2ts`, `.m4v`, `.mkv`, `.mov`, `.mp4`, `.mts`, `.webm`. Unsupported or unreadable candidates are warned or skipped when other valid media remains; inspect report records skipped inputs. GoPro `GOPR####` and `G[A-Z]CCFFFF` names receive conservative chapter/session chronology evidence. Filename ordering is deterministic, not semantic understanding.

## Privacy, cost, and limits

Media stays on configured local volumes. Phase 1 makes zero cloud calls and reports cloud usage as `0`. No automatic intelligence claim is valid: selection is deterministic sample selection, not highlight ranking or story editing. Real performance, chronology, and HERO12 compatibility require authorized representative footage and benchmark procedure in [`docs/benchmark.md`](docs/benchmark.md).

Reports expose warnings, fallbacks, stage timing, storage roots, estimated peak space, skipped inputs, and output metadata. Hardware encoding may fall back to software `libx264` after capability probing. Render output is written as a partial file on final volume, validated with ffprobe, then renamed in place.

Stable CLI exit categories map to codes: configuration 10, storage 11, inspection 12, plan 13, rendering 14, output 15, state 16.

## Troubleshooting

- `configuration`: verify TOML syntax, all five `[paths]` values, non-negative reserve, positive concurrency, and `cloud_enabled = false`.
- Missing volume or `volume changed`: mount SSD at exact configured path. Do not create a same-named internal directory as workaround.
- `ffprobe failed` or skipped input: inspect codec/container with `ffprobe`; keep unsupported media out of acceptance batch and retain warning in report.
- Insufficient space: free space on configured workspace/cache/output volume or reduce batch size; reserve is deliberate.
- Render failure: inspect `status JOB_ID`, preserve report/database state, check ffmpeg build and source readability, then retry `resume` after fixing storage/media.
- Stale or missing output: `resume` validates recorded artifacts and reruns invalid stages without trusting missing files.
- Permission errors: verify SSD ownership, writable workspace/cache/output roots, and readable input files.

## Development quality gate

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -v
uv build
uv run video-editor --help
```

Architecture details live in [`docs/architecture.md`](docs/architecture.md). Real-camera benchmark gate lives in [`docs/benchmark.md`](docs/benchmark.md).
