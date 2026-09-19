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

Planner writes two versioned JSON plans with `phase1-sample-v1` provenance:

- horizontal 1920×1080 `long.mp4`
- vertical 1080×1920 `short-01.mp4`

Planner chooses bounded first intervals with clean cuts. It is deterministic fixture coverage, not quality assessment or automatic narrative selection. Model validation rejects missing sources, invalid intervals, gaps, invalid transitions, unsupported output semantics, and long-form duration at or above one hour.

## Rendering and validation

Compiler accepts validated plan data and builds FFmpeg argument vectors; it does not execute shell text from paths or plans. Render writes `.partial` on output volume, then validates readable video, dimensions, duration tolerance, and requested audio policy with ffprobe before same-volume rename. Software `libx264` is portable baseline. Hardware encoder requests only survive capability checks; otherwise workflow records fallback. Phase 1 validates `render_concurrency` but serializes renders with one process at a time; configured values above one do not enable parallel work.

## Persistence and reports

`jobs.sqlite3` records jobs, stages, sources, probes, chronology, artifacts, and stage results. Every stage stores input/settings hashes and implementation version. Success is reusable only with matching data and valid artifacts.

Report JSON and Markdown include selected source intervals, output probes, skipped inputs, warnings, fallbacks, stage timing, storage roots, estimated peak space, and cloud usage `0`. Reports state deterministic-selection limitation.
