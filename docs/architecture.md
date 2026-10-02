# Phase 1 Architecture

## Boundary

Phase 1 is a local workflow for deterministic sample editing. Python orchestrates configuration, persistence, validation, and argument compilation. FFmpeg and ffprobe run as local subprocesses. SQLite persists each job. No cloud provider, remote analysis, paid API, or automatic highlight model is in this implementation.

## Workflow

```text
inspect → proxy → plan → render → validate → report
```

`run` starts one job and executes each stage. `status` returns stored job, stage, source, chronology, and artifact records. `resume` reruns only incomplete or invalidated stages after validating recorded artifacts, source fingerprints, settings hashes, implementation version, and volume identity.

Each job owns child directories below configured workspace, cache, and output roots. Roots may not overlap input/source root. Missing or changed volume fails without internal fallback.

## Source and chronology

Discovery recursively finds supported video extensions in deterministic relative-path order and calculates bounded content fingerprints. Source paths remain local evidence, not executable input. ffprobe inspection records container, stream, timing, color, rotation, and audio metadata with warnings for conditions such as missing audio, HEVC, high bit depth, HDR, VFR indicators, and unreadable inputs.

Sequencing recognizes conservative GoPro forms `GOPR####.MP4` and `G[A-Z]CCFFFF.MP4`. It groups chapter files by file number, orders chapters numerically, then orders groups by creation metadata, numeric continuity, or discovery order. Duplicate chapters, resets, conflicts, and chronology uncertainty are preserved as warnings. Filename parsing is not real HERO12 validation.

## Derived media and plans

Proxy generation creates muted H.264 analysis MP4 at no more than 960px width and 15 fps. Audio-bearing source also yields mono 16 kHz PCM WAV. Both outputs stay beneath cache root and have explicit timestamp mapping. Rendering always returns to original inputs rather than proxies.

Planner writes two versioned JSON plans with `phase1-sample-v2` provenance:

- horizontal 1920×1080 `long.mp4`
- vertical 1080×1920 center-cropped `short-01.mp4`

Planner chooses bounded first intervals with clean cuts. It is deterministic fixture coverage, not quality assessment or automatic narrative selection. Model validation rejects missing sources, invalid intervals, gaps, invalid transitions, unsupported output semantics, and long-form duration at or above one hour.

## Rendering and validation

Compiler accepts validated plan data and builds FFmpeg argument vectors; it does not execute shell text from paths or plans. Render writes `.partial` on output volume, then validates readable video, dimensions, duration tolerance, and requested audio policy with ffprobe before same-volume rename. Software `libx264` is portable baseline. Hardware encoder requests only survive capability checks; otherwise workflow records fallback. Phase 1 validates `render_concurrency` but serializes renders with one process at a time; configured values above one do not enable parallel work.

## Persistence and reports

`jobs.sqlite3` records jobs, stages, sources, probes, chronology, artifacts, and stage results. Every stage stores input/settings hashes and implementation version. Success is reusable only with matching data and valid artifacts.

Report JSON and Markdown include selected source intervals, output probes, skipped inputs, warnings, fallbacks, stage timing, storage roots, estimated peak space, and cloud usage `0`. Reports state deterministic-selection limitation.

## Phase 2 automatic highlights and evaluation boundary

The optional cloud path adds `segment → analyze → rank` between proxy and plan, making nine stages: `inspect → proxy → segment → analyze → rank → plan → render → validate → report`. Existing Phase 1 six-stage mode remains local and deterministic. Phase 2 uses `gemini-3.8-flash` for schema-constrained 0.5 FPS broad scan and 2/3/5 FPS candidate refinement. Original sources never cross the provider boundary: only generated H.264 proxy chunks with compressed audio may be uploaded after manifest registration, digest, source identity, mapping, and generated-root checks. Rendering still reads originals. A 100% broad-coverage gate blocks refinement and outputs when a source range is missing.

Ranking combines versioned score dimensions and penalties, semantic/temporal deduplication, and diversity quotas. Planner produces chronological 16:9 `long.mp4` at ≤1,800 seconds and zero to five distinct 9:16 shorts (`short-01.mp4` onward) at ≤180 seconds each. Shorts cannot share semantic groups or overlap more than 10% of the shorter source interval. Audio is source speech/ambience only. Local tracking validates vertical crop bounds, 5% margin, 95% subject retention, velocity/acceleration limits, and ≤2-second fallback holds. Transition relation matrix restricts cut, dissolve, fade, and fade-black to compatible scene relationships. Output validation probes files and crop evidence. Job fingerprints and persisted artifacts allow resume without repeating matching provider requests; changed model/prompt/ranking/plans invalidate dependent stages.

The USD 3.00 per source-hour reservation cap (the release evaluation still requires actual spend of at most USD 1.00 per source-hour) uses persisted reservations before dispatch and retains unknown-billing reservations. Documented static video rates inform the estimator, which includes capped output and total retry/repair attempts. Exact custom-FPS and metadata tokenization is not provider-verified, so production pricing catalog remains empty and real cloud analysis fails closed. `GEMINI_API_KEY` is runtime-only. The live contract test requires explicit opt-in and ≤USD 0.01 reservation; it was not run as part of offline verification.

`video_editor.evaluation` holds strict label/prediction rows, exploratory fixed-formula metrics from supplied JSON, and a fail-closed release check. CLI reports `metrics_status: unverified_input`; it does not attest measured outputs, billing, sources, or human preference. Labeled intervals record batch/source/range, train/calibration/holdout partition, inclusion/weak/duplicate labels, chronology, subject boxes, and acceptable transitions. Batch partitions cannot mix. Must-include recall is recovered must-include / labeled must-include; weak rejection is rejected weak / labeled weak; duplicate rate is duplicate selected pairs / all selected pairs; abrupt-cut rate is unrelated plain cuts / all boundaries. It also reports chronology, crop retention, duration compliance, short diversity, and human preference against Phase 1. Empty denominators are not treated as success. Duration, budget, source safety, and schema validity are hard gates.

Release requires ≥100 labeled intervals from ≥3 real batches, isolated train/calibration/holdout, committed calibration thresholds frozen before holdout, matching model/prompt/config/dataset versions, and passing holdout-only metrics. Current `check_release_gate` **always blocks** because independent artifact/ledger provenance, committed threshold/calibration digest verification, and holdout-only scoring are not implemented. The committed synthetic fixture is marked release-ineligible and demonstrates plumbing only. No real labeled holdout, live Gemini evidence, or general Phase 2 quality claim exists. Evaluation applies only to actual tested footage categories and recorded settings when such evidence is later produced. The pre-live tokenization/pricing gate and pre-existing Phase 1 render-attestation follow-up remain open.
