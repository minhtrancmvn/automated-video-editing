# Video Editor Architecture

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

The USD 3.00 per source-hour reservation cap (the release evaluation still requires actual spend of at most USD 1.00 per source-hour) uses persisted reservations before dispatch and retains unknown-billing reservations. Documented static video rates inform the estimator, which includes capped output and total retry/repair attempts. The production catalog pins Gemini 3.8 Flash Standard pricing (USD 0.75 per million input tokens of any modality and USD 3.75 per million output tokens, including thinking) from the official pricing page through 2026-12-31. Price lookup fails closed after that date; the scheduled 2027 increase requires a newly verified pin. Custom-FPS metadata tokenization and candidate-refinement costs remain uncertain despite live broad-scan measurements on generated 1-, 60-, and 900-second proxies. `GEMINI_API_KEY` is runtime-only. Cloud-enabled `run` and `resume` can incur charges with a runtime key; obtain explicit batch approval before use. One historical attempt03 real-footage long edit completed and received owner acceptance; no vertical-short acceptance or independent release-quality evaluation exists. See [current progress](phase-2-progress.md) for dated evidence and scope.

`video_editor.evaluation` holds strict label/prediction rows, exploratory fixed-formula metrics from supplied JSON, and a fail-closed release check. CLI reports `metrics_status: unverified_input`; it does not attest measured outputs, billing, sources, or human preference. Labeled intervals record batch/source/range, train/calibration/holdout partition, inclusion/weak/duplicate labels, chronology, subject boxes, and acceptable transitions. Batch partitions cannot mix. Must-include recall is recovered must-include / labeled must-include; weak rejection is rejected weak / labeled weak; duplicate rate is duplicate selected pairs / all selected pairs; abrupt-cut rate is unrelated plain cuts / all boundaries. It also reports chronology, crop retention, duration compliance, short diversity, and human preference against Phase 1. Empty denominators are not treated as success. Duration, budget, source safety, and schema validity are hard gates.

Release requires ≥100 labeled intervals from ≥3 real batches, isolated train/calibration/holdout, committed calibration thresholds frozen before holdout, matching model/prompt/config/dataset versions, and passing holdout-only metrics. Current `check_release_gate` **always blocks** because independent artifact/ledger provenance, committed threshold/calibration digest verification, and holdout-only scoring are not implemented. The committed synthetic fixture is marked release-ineligible and demonstrates plumbing only. No real labeled holdout or general Phase 2 quality claim exists; limited live broad-scan cost evidence does not establish edited-story quality. Evaluation applies only to actual tested footage categories and recorded settings when such evidence is later produced. Independent candidate-refinement billing attestation and holdout evaluation remain open.

## Grounded selection and bounded evaluator

Ordinary Phase 2 analysis now creates immutable proposals from persisted broad results and exact source/proxy lineage. Selection checks evaluator evidence integrity, factual support, editorial disposition and interval coverage. Supported primary proposals may become eligible for **separately approved paid refinement**. Alternatives, contradictions, uncertain support and sparse-only evidence cannot be silently promoted. `render_ready`, `portrait_ready` and `paid_dispatch_authorized` remain literal false. The ordinary final-plan gate rejects even a caller-supplied readiness claim; ordinary `run`/`resume` is not an approval bypass.

`bounded-evaluation` prices an exported finite plan without credentials or database access by default. Dispatch requires an exact independent evaluation approval, persisted generated-proxy provenance and fresh budget admission. Video-aware grounding supplies attributed model assertions at 3 FPS with audio supplied; conditional text-only comparison cites those assertions and cannot add visual evidence. Each uncached request permits one generation, with fixed input/output bounds; no semantic repair or automatic retry. Receipts use additive schema version 4 and the existing budget. Unknown reservations remain held in full. No release, reset, rewrite, or transfer. No secret logging or persistence.

`EvaluatedWorkflowService` revalidates the full bundle against current registered media and persisted evaluator receipts on every review access. Construction grants neither refinement nor render authority. Model observations are not independent truth, sampled frames are not continuous audiovisual playback, and schema validation does not establish editorial appeal or final-edit acceptance.

## Reviewed draft adapter (offline integration verified)

The additive local adapter consumes an already completed evaluator bundle and matching **existing** refinement evidence. It does not invoke ordinary analysis/resume, create a provider, load credentials, dispatch paid requests or migrate/write the job ledger. Its CLI opens only an existing SQLite ledger using `mode=ro`, `query_only` and a read transaction. `prepare-reviewed` prepares reviewable V2 drafts, not accepted final edits. `render-reviewed` requires a separate trusted local approval bound to the canonical SHA-256 digest of the entire typed draft, including every plan, crop track, setting and provenance record; selection-only approval and timestamp-based output reuse are insufficient. Generated-media service and CLI lifecycle tests verify this integration offline; they do not establish live semantic accuracy, real-footage quality or release readiness.

The adapter flow is:

1. Revalidate evaluator receipts and registered proxy bytes; require completed current proxy/segmentation stages and exact cached candidate-refinement lineage.
2. Reuse deterministic evidence mapping, ranking, local subject tracking, chronology, transition derivation and V2 planner validation. Do not manufacture scores, subject boxes or semantic relations. Existing duration, crop and short-eligibility thresholds remain unchanged.
3. Save a draft for review in a new local file. Draft preparation may decode generated local proxies for tracking, but makes no provider calls. Missing or stale evidence fails instead of triggering refinement.
4. Admit rendering only after exact draft approval and fresh provenance/source checks, into a new safe directory. Compile shell-free FFmpeg arguments, render originals locally, validate V2 outputs and crop evidence, and save local audit results without modifying workflow stages or budget state.

Broad and candidate cache admission requires exact current identities and completed, known billing within the original reservation. A validated result with unknown billing is not enough. Preparation binds evaluator receipts, current registered media, proxy/segmentation bytes, source fingerprints/stat/probes, persisted stage settings and current planner/tracker configuration. Original identity retains the repository's bounded fingerprint contract; this is not a fresh full-original-file SHA-256 attestation. Render rederives the whole draft and rejects any mismatch before creating output. Sources, caches, workspace and state trees are protected destinations. Output uses a new directory, private draft/audit JSON, staged media and V2 validation before no-replace publication. macOS publication uses exclusive `renamex_np` rather than hardlinks, so the publication primitive does not require hardlink support on exFAT. Other platforms use no-replace hardlinks and fail closed when unsupported; no overwriting rename fallback exists. The Darwin primitive and collision preservation are tested on the current filesystem, not an actual exFAT mount. The non-Darwin hardlink path has not been executed in current verification; on Linux, the render output directory must sit on a filesystem that supports hardlinks (exFAT and FAT do not). Failed local attempts retain partials and a sanitized failure record; they do not authorize automatic retries or rewrite workflow stages/budget.

For a separately authorized job, the command contract is:

```sh
video-editor prepare-reviewed evaluation-bundle.json \
  --config config.toml --output /absolute/existing-parent/reviewed-draft.json
```

The JSON response contains `draft_digest`; do not substitute a hash of the indented file bytes. After actual review and owner approval of that exact draft, a trusted operator supplies an approval file containing only:

```json
{"approval_id": "owner-approved-local-render-id", "draft_digest": "<exact draft_digest from preparation>"}
```

```sh
video-editor render-reviewed /absolute/existing-parent/reviewed-draft.json \
  --evaluation-bundle evaluation-bundle.json --config config.toml \
  --approval reviewed-render-approval.json \
  --output /absolute/existing-parent/new-reviewed-render
```

Use existing, non-symlink parent directories outside protected trees; both draft file and render directory must be new. Preparation is local processing, not a no-work pricing preflight: it probes sources and may decode generated proxies for tracking. Rendering reads originals. Missing cached refinement fails; these commands cannot purchase it. A successful local render writes `draft.json`, published media and `audit.json` in its fresh destination, without registering completion in the original job ledger.

Both commands hold one SQLite read transaction for their entire run, including proxy decoding and FFmpeg rendering. The ledger uses SQLite's default rollback journal, so any concurrent writer to the same `jobs.sqlite3` (ordinary `run`/`resume`, `bounded-broad`, `bounded-evaluation`) waits its 30-second busy timeout and then fails with `database is locked`. A paid command failing this way after dispatch cannot record its settlement. Run reviewed commands only while no other `video-editor` command uses the same state directory. Reviewed commands need no new dependencies, configuration keys, credentials or schema migration; they never migrate the ledger, so it must already be at the schema written by the evaluator run that produced the bundle.

Current verification and any remaining gaps are recorded in [Phase 2 progress](phase-2-progress.md). This development request does not authorize real-source processing or rendering. Approval JSON is a trusted caller scope assertion, not cryptographic proof of consent; operators must create it only after actual owner approval. No synthetic output presented as live evidence. No sampled frames presented as continuous audiovisual playback. No final-edit acceptance labels. No external publication of private evidence. Never upload source originals; use generated, registered proxies with full provenance checks. Remaining budget is not permission to spend.
