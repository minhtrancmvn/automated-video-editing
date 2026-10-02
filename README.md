# Local Video Editor

Phase 1 is local-first workflow foundation. It discovers and inspects video, builds deterministic sample plans, renders validated horizontal and vertical MP4 files, persists job state, and emits reports. It does not provide automatic highlight intelligence, storytelling, transcription, captions, subject tracking, or content-aware reframing.

Real-camera workflow validation passed for one authorized 18-file HERO12 batch under the recorded environment. This scoped result does not establish compatibility with every HERO12 setting; camera firmware/settings, manual chronology confirmation, independent peak-disk sampling, and disconnected-volume recovery remain unverified. See [`docs/benchmark-records/2026-09-28/benchmark-record.md`](docs/benchmark-records/2026-09-28/benchmark-record.md).

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

With `cloud_enabled = false`, Phase 1 stays local-only and uses no runtime environment variables. With `cloud_enabled = true`, Phase 2 is selected, but production Gemini pricing is intentionally unset: analysis fails closed before upload. Do not treat the cloud setting as permission to run paid analysis. Exact custom-FPS video tokenization and timestamp/metadata charges still need provider verification before pricing can be pinned. `GEMINI_API_KEY` is runtime-only; never put it in TOML, SQLite, logs, reports, or shell history. `.env.example` is a variable-name reference, not a request to save credentials.

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

`run` executes `inspect`, `proxy`, `plan`, `render`, `validate`, and `report`. It prints job and stage progress to stderr while keeping the final result on stdout:

```text
job 76dabac4-31db-451d-bf15-1ccb26575403 started
job 76dabac4-31db-451d-bf15-1ccb26575403: [1/6] inspect started
job 76dabac4-31db-451d-bf15-1ccb26575403: [1/6] inspect completed
job 76dabac4-31db-451d-bf15-1ccb26575403: [2/6] proxy started
```

Progress reports stage activity, not FFmpeg frame percentages. `inspect`, `plan`, `render-from-plan`, and `resume` use the same progress output when they execute stages.

`run` creates `<output_dir>/<job_id>/`, with:

```text
edit-plan-horizontal.json
edit-plan-vertical.json
long.mp4
short-01.mp4
report.json
report.md
```

Plans use `phase1-sample-v2` provenance and select first bounded samples, not highlights. Horizontal output is 1920×1080. Vertical output is 1080×1920 and center-crops wide footage to fill the vertical frame. Long-form timelines must stay strictly below 60 minutes.

Use the job ID printed by `run` to inspect or resume the same job:

```bash
video-editor status JOB_ID --config config.toml
video-editor resume JOB_ID --config config.toml
```

`resume` validates persisted artifacts and reuses matching completed stages. Valid proxies and extracted audio remain in `<cache_dir>/<job_id>/`; changed plans rerender affected outputs without regenerating those same-job proxies. A new `run` creates a new job ID and job-specific cache directory, so it does not reuse another job's proxy files.

Supported recursive extensions: `.3gp`, `.avi`, `.m2ts`, `.m4v`, `.mkv`, `.mov`, `.mp4`, `.mts`, `.webm`. Unsupported or unreadable candidates are warned or skipped when other valid media remains; inspect report records skipped inputs. GoPro `GOPR####` and `G[A-Z]CCFFFF` names receive conservative chapter/session chronology evidence. Filename ordering is deterministic, not semantic understanding.

## Phase 2 automatic highlights — offline evaluation only

Phase 2 adds nine resumable stages: `inspect → proxy → segment → analyze → rank → plan → render → validate → report`. Analysis uses generated, registered H.264 proxy chunks with compressed audio. Each upload is checked against its persisted manifest, source fingerprint, mapping, generated root, and file digest; original media is never uploaded. Broad Gemini scan is fixed at 0.5 FPS, detailed candidate sampling uses 2, 3, or 5 FPS, and the exact model is `gemini-3.8-flash`. Broad coverage must reach 100% before refinement, planning, or rendering.

The intended long output is chronological 16:9 `long.mp4` capped at 1,800 seconds. Zero to five independent 9:16 shorts receive `short-01.mp4`, `short-02.mp4`, and later names only when strong distinct material exists; each is capped at 180 seconds. Cross-short overlap cannot exceed 10% relative to the shorter source interval, and semantic dedup groups cannot repeat. Audio comes only from source speech/ambience, without music or synthetic media. Tracked vertical crops use local subject-retention evidence, bounded movement, and safe margins. Semantic transitions allow cuts for continuous action/matched motion/same event, dissolves for same event/place-time shift, fades for chapter/story boundaries, and fade-to-black for chapter/time/location changes. Resume reuses matching validated analysis and renders; stale plans invalidate downstream artifacts. Offline fake-provider tests verify this wiring, not edited-story quality.

Cost reservation is capped at USD 3.00 per source hour (a worst-case reservation ceiling; the release evaluation still requires actual spend of at most USD 1.00 per source hour), but **production pricing is not pinned**. The estimator handles documented frame/audio token rates, output cap, and retries, yet exact custom-FPS and metadata token accounting remains unverified. Cloud runs therefore fail closed rather than risk an underestimated charge. A live Gemini contract test is separately opted in, excluded from default tests, and requires explicit approval plus a pre-reserved maximum of at most USD 0.01. No live test or real-footage Phase 2 release evaluation has been run here.

Offline evaluation plumbing can be inspected without a provider call:

```bash
uv run python -m video_editor.evaluation --labels tests/evaluation/synthetic-labels.json --thresholds tests/evaluation/thresholds-v1.json --expect-blocked-release
```

Synthetic rows demonstrate formulas only and are marked `release_eligible=false`; reported metrics are tagged `unverified_input`. This harness **cannot pass a release gate**: supplied prediction JSON is not independently linked to rendered outputs, billing ledger, source/upload proof, human study, or committed calibration thresholds, and holdout-only scoring is not implemented. A later release-capable evaluator must verify those artifacts, score only holdout batches, require at least 100 real labeled candidate intervals across three authorized batches with isolated train/calibration/holdout, and enforce hard safety/quality thresholds frozen before holdout. No Phase 2 quality or release-readiness claim is supported until that work and the pre-live pricing/tokenization gate are complete. See [architecture](docs/architecture.md) for evaluation contracts and remaining limitations.

## Privacy, cost, and limits

Media stays on configured local volumes. Phase 1 makes zero cloud calls and reports cloud usage as `0`. No automatic intelligence claim is valid: selection is deterministic sample selection, not highlight ranking or story editing. The benchmark procedure lives in [`docs/benchmark.md`](docs/benchmark.md); the scoped real-camera result and its limitations live in [`docs/benchmark-records/2026-09-28/benchmark-record.md`](docs/benchmark-records/2026-09-28/benchmark-record.md).

Reports expose warnings, fallbacks, stage timing, storage roots, estimated peak space, skipped inputs, and output metadata. Hardware encoding may fall back to software `libx264` after capability probing. Render output is written as a partial file on final volume, validated with ffprobe, then renamed in place.

Stable CLI exit categories map to codes: configuration 10, storage 11, inspection 12, plan 13, rendering 14, output 15, state 16.

## Troubleshooting

- `configuration`: verify TOML syntax, all five `[paths]` values, non-negative reserve, positive concurrency, and compatible `cloud_enabled`/`[gemini]` settings. Production cloud pricing is deliberately unavailable; a pricing/budget failure is expected until provider accounting is verified and pinned.
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

Architecture details live in [`docs/architecture.md`](docs/architecture.md). Real-camera benchmark procedure lives in [`docs/benchmark.md`](docs/benchmark.md), with the completed scoped record in [`docs/benchmark-records/2026-09-28/benchmark-record.md`](docs/benchmark-records/2026-09-28/benchmark-record.md).
