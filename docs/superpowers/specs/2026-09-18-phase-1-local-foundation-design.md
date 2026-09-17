# Phase 1 Local Foundation Design

## Goal

Build a local-first video-editing foundation that discovers and inspects source media, creates analysis proxies, persists resumable jobs, validates versioned edit plans, renders deterministic horizontal and vertical MP4 outputs from original media, and reports results without requiring manual editor operation.

Phase 1 proves media handling and rendering. It does not claim automatic highlight selection, storytelling intelligence, transcription, or content-aware subject tracking.

## Scope

### Included

- Generic `video-editor` CLI and `video_editor` Python package.
- Recursive media discovery and stable source identification.
- ffprobe-based media inspection and warnings.
- Configurable storage roots, including external SSD volumes.
- Lightweight proxy and extracted-audio generation.
- Persistent SQLite job and stage state.
- Versioned, validated edit-decision schema.
- Deterministic sample plans producing one horizontal and one vertical output.
- FFmpeg command compilation and execution.
- Post-render technical validation.
- JSON and Markdown reports.
- Automated tests using generated media fixtures.
- Apple Silicon setup and benchmark instructions.

### Deferred

- Speech transcription.
- Cloud visual analysis.
- Automatic highlight ranking and deduplication.
- Automatic travel-story planning.
- Face, subject, and action tracking.
- Automatic captions and music ducking.
- Cloud provider adapters and spending controls beyond disabled configuration boundaries.
- Watched folders and graphical UI.

## Architecture

Use Python for orchestration, configuration, schemas, job state, and command compilation. Use FFmpeg and ffprobe as subprocesses for media operations. Use SQLite from Python's standard library for persistent state.

Core units have narrow interfaces:

- `config`: load and validate user settings and storage roots.
- `media.discover`: find candidate files without changing originals.
- `media.probe`: convert ffprobe JSON into typed source metadata.
- `media.capabilities`: detect installed FFmpeg encoders and host resources.
- `media.proxies`: generate bounded analysis media with explicit timestamp mappings.
- `persistence.database`: store jobs, stages, sources, artifacts, and cache records.
- `models.edit_plan`: validate planner output independently of rendering.
- `planning.sample_plan`: create deterministic Phase 1 plans from inspected media.
- `rendering.compiler`: convert validated plan data into owned FFmpeg arguments.
- `rendering.ffmpeg`: execute commands without accepting shell text from plans.
- `validation.outputs`: inspect rendered artifacts and compare them with plan requirements.
- `reporting`: emit machine-readable JSON and human-readable Markdown.

Provider-specific analysis will later consume inspected sources and produce data matching the same plan boundary. It will not bypass deterministic validation.

## Project Structure

```text
.
├── pyproject.toml
├── README.md
├── .env.example
├── config.example.toml
├── schemas/
│   └── edit-plan-v1.json
├── src/video_editor/
│   ├── cli.py
│   ├── config.py
│   ├── models/
│   ├── media/
│   ├── persistence/
│   ├── planning/
│   ├── rendering/
│   ├── validation/
│   └── reporting/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── docs/
    ├── architecture.md
    └── benchmark.md
```

Generated fixture media remains small and reproducible. Tests may generate it in temporary directories rather than checking binary files into Git.

## CLI

Phase 1 exposes:

```text
video-editor inspect INPUT
video-editor plan INPUT --output PLAN
video-editor render-from-plan PLAN
video-editor run INPUT
video-editor status JOB_ID
video-editor resume JOB_ID
```

- `inspect` discovers files, records source identity, probes streams, checks storage, and emits an inventory with warnings.
- `plan` validates inspected sources and writes deterministic sample horizontal and vertical plans.
- `render-from-plan` validates a reusable plan, renders outputs, validates artifacts, and writes a report.
- `run` performs inspect, proxy, plan, render, validate, and report stages under one persistent job.
- `status` reads stage and artifact state without accessing paid services.
- `resume` restarts at the first incomplete or invalidated stage and reuses matching cached work.

Commands return nonzero status for invalid configuration, unreadable required inputs, missing storage volumes, failed validation, and failed renders. Per-source inspection problems can remain warnings when other valid sources allow work to continue.

## Configuration and External Storage

Large and durable paths are independently configurable:

```toml
[paths]
input_dir = "/Volumes/TravelSSD/footage"
workspace_dir = "/Volumes/TravelSSD/video-editor/work"
cache_dir = "/Volumes/TravelSSD/video-editor/cache"
output_dir = "/Volumes/TravelSSD/video-editor/output"
state_dir = "~/Library/Application Support/video-editor"
```

Defaults keep small SQLite state and structured logs on the internal disk. Proxies, extracted audio, cache entries, temporary renders, and final outputs use configured workspace/cache/output roots. Originals can reside on any mounted volume and are never modified.

Each job records resolved paths plus available volume identity information. A missing configured external volume is an error; the application must not silently fall back to a similarly named internal directory. Before each write-heavy stage, the application checks that the expected root still resolves to the recorded volume and that free space exceeds the estimated stage requirement plus a configurable reserve.

Phase 1 supports APFS and exFAT paths. It does not rely on cross-volume atomic renames. Temporary render files are created on the output volume, validated there, then renamed within that volume. SSD disconnection marks the running stage failed or interrupted while preserving committed database state. `resume` continues after the expected volume returns.

Cleanup is explicit and limited to descendants of the configured cache or workspace roots. Original paths are excluded from cleanup. Phase 1 documents cleanup but does not add automatic eviction unless needed to complete the tested flow.

## Source Discovery and Identity

Discovery recursively checks configured extensions and records rejected or unreadable candidates. It does not infer chronology from filesystem modification time.

A source identity combines stable content evidence and media facts. The initial implementation uses file size, a bounded content fingerprint, and relevant probe metadata; absolute path is stored as a location but is not the sole identity. This allows moved media to be recognized while avoiding a mandatory full-file hash over hours of footage. Identity algorithm and version are stored so cache invalidation remains explicit.

Embedded creation metadata may contribute to chronological ordering when present and trustworthy. Otherwise Phase 1 preserves discovery order and reports chronology uncertainty. GoPro split recordings remain separate source records; grouping them automatically is deferred until camera metadata behavior is verified against real fixtures.

## Inspection

ffprobe JSON is parsed into typed metadata:

- duration and stream time bases;
- video codec, profile, pixel format, dimensions, and frame-rate fields;
- color primaries, transfer, matrix, and range when available;
- rotation and display-matrix metadata;
- audio codec, layout, channels, and sample rate;
- container tags useful for creation time and camera provenance.

Inspection reports, rather than hides:

- mixed dimensions or frame rates;
- variable-frame-rate indicators;
- rotation metadata;
- HEVC and 10-bit video;
- HDR, log, or unknown color interpretation;
- missing or damaged audio;
- corrupt, incomplete, or unreadable files;
- unsupported codecs or filters in the installed FFmpeg build.

Color metadata is never converted into an assumed LUT. Unknown color remains a warning unless the user supplies an explicit override.

## Proxy and Timestamp Mapping

Analysis proxies are derived artifacts. Each mapping records source ID, source start and end, proxy start and end, settings hash, and tool version. Phase 1 can use identity mappings for full-length constant-rate proxies but stores the mapping explicitly so later chunked analysis does not depend on implicit assumptions.

Proxy settings favor bounded storage and decode cost. Extracted speech-analysis audio uses 16-bit PCM WAV compatible with the planned local whisper.cpp adapter. Rendering always references original media, not proxies.

## Edit-Decision Schema

The versioned JSON schema represents:

- schema and planner versions;
- source IDs, source locations, identities, and source intervals;
- timeline placement and track assignment;
- crop, fit, background, and safe-margin framing instructions;
- speed changes;
- transitions and overlap durations;
- caption and overlay data;
- music references, gain, and ducking instructions;
- color treatment declarations;
- output container, dimensions, frame rate, codec preferences, and duration limits;
- selection reasons, confidence, and provenance.

Phase 1 implements only supported primitives and rejects declared primitives it cannot execute. Plans contain data, never command strings.

Validation rejects unknown source IDs, missing files, changed source identities, negative or out-of-range source intervals, invalid speed values, impossible transition overlaps, unintended timeline gaps, unsupported operations, missing output dimensions, and long-form timelines at or above 60 minutes. Music paths, when later supported, must resolve under an approved music directory.

Timeline duration is computed from source duration divided by playback speed, then adjusted for explicit transition overlaps. The same computed timeline drives video, audio, captions, and output validation.

## Deterministic Sample Planning

Phase 1 selects bounded source intervals through deterministic rules intended only to exercise the pipeline. It labels selection reasons as `phase1_sample` and does not report quality confidence.

The planner produces:

- one 1920×1080 horizontal plan;
- one 1080×1920 vertical plan;
- clean cuts only unless a fixture explicitly tests a transition;
- source audio when available;
- silence-compatible rendering when audio is absent.

For vertical output, Phase 1 supports a center crop when the plan explicitly selects it and a fit-over-background fallback for wide content. Default planning chooses the safe fit mode when crop suitability is unknown. Content-aware tracking is deferred.

## Rendering Safety

The compiler builds an argument vector passed directly to the FFmpeg executable. It never invokes a shell and never evaluates strings from filenames, metadata, transcripts, or plans.

Runtime capability detection checks the installed FFmpeg build. Hardware encoding is used only when the configured encoder is present and passes a small capability probe; otherwise software encoding is used. One render runs at a time by default.

Each output is written to a partial path on its final volume. After FFmpeg exits successfully, ffprobe verifies readability, streams, dimensions, and duration tolerance. Only validated output is renamed to its final name. Failed partial artifacts remain identified in job state for diagnosis or safe cleanup.

## Persistence and Resumption

SQLite tables cover:

- jobs;
- job stages;
- sources;
- source probes;
- proxy mappings;
- artifacts;
- cache entries;
- render attempts.

Stage records include input fingerprint, relevant settings hash, implementation version, status, timestamps, structured error data, and artifact references. Writes use transactions and parameterized SQL.

A cached result is reusable only when source identity, relevant settings, schema or implementation version, and artifact validation all match. `resume` never assumes a recorded success is valid when its output disappeared with an external volume.

## Resource Safety

Startup detection records architecture, available memory when macOS exposes it, free disk space per configured root, installed FFmpeg/ffprobe versions, and candidate encoders.

Peak-storage estimates cover expected proxy output, extracted audio, partial render, final output, and reserve space. Estimates are conservative and displayed as estimates, not guarantees. A stage fails before writing when known free space is insufficient. The current machine has roughly 30 GiB free internally, so examples keep large intermediates on an external SSD.

Throughput is not promised. Benchmark documentation measures representative real footage before giving local performance expectations.

## Reporting

Every completed or failed job emits available machine-readable JSON state. Successful runs also emit a Markdown report containing:

- source files and selected intervals;
- output paths, durations, dimensions, and codecs;
- skipped or problematic sources;
- warnings and fallback decisions;
- processing time by stage;
- storage roots and peak-space estimates;
- cloud usage and cost as zero in Phase 1;
- explicit statement that deterministic sample selection is not automatic highlight intelligence.

## Error Handling

Errors have stable categories for configuration, storage, source inspection, plan validation, rendering, output validation, and internal state. CLI output gives a concise message and points to structured job logs.

Batch inspection isolates source-level failures. Required stage failures stop dependent stages but retain completed records. Interrupt signals cause the active process to terminate safely, retain partial artifact metadata, and mark the stage interrupted when possible.

## Testing

Unit tests cover:

- source interval and identity validation;
- timeline duration with speed changes;
- transition overlap calculations;
- gaps and conflicting placements;
- output dimension and hard-duration checks;
- missing-audio plans;
- cache keys and invalidation;
- path containment for cleanup and future music input;
- volume mismatch and missing-volume behavior;
- interrupted-stage recovery;
- cloud configuration disabled by default.

Integration fixtures are generated with FFmpeg using color sources, test patterns, tones, and silent video. Integration tests execute inspect, plan, render, validate, status, and resume paths and assert:

- readable MP4 outputs;
- 1920×1080 horizontal and 1080×1920 vertical dimensions;
- expected duration tolerance;
- valid audio behavior with and without source audio;
- report and reusable validated plans;
- no changes to original fixture hashes;
- no external API calls.

Hardware encoder tests are capability-gated. Software rendering remains the portable required path.

## Acceptance Boundary

Phase 1 is accepted when one local command processes authorized fixture media through persistent stages and produces validated horizontal and vertical MP4 files, edit plans, and a useful report; interruption can resume without repeating valid completed stages; external storage is supported without silent internal fallback; and originals remain unchanged.

Real-world storytelling quality, automatic moment selection, captions, and content-aware reframing remain unimplemented and are reported as such.