# Phase 2 Automatic Highlights Design

## Goal

Build an automatic, resumable pipeline that identifies key moments in authorized footage and assembles:

- one chronological 16:9 long edit capped at 30 minutes; and
- zero to five distinct 9:16 short edits, each capped at 3 minutes.

Output duration follows available quality. The pipeline omits weak footage instead of padding an edit to its cap. Scene boundaries use context-appropriate cuts, dissolves, and fades rather than decorative effects or unjustified abrupt changes.

Phase 2 extends the Phase 1 local workflow. Originals remain local and immutable. Only reduced proxy chunks with compressed audio may be uploaded to Gemini. All AI output passes through provider-neutral schemas, deterministic validation, versioned edit plans, local FFmpeg rendering, output validation, persistence, and reporting.

## Product Behavior

### Long edit

The long edit:

- uses a 16:9 output;
- preserves chronological story order;
- forms understandable chapters with a beginning, progression, and ending;
- balances action, scenic views, human moments, and story milestones;
- removes weak, technically poor, repetitive, or narratively redundant material;
- stops when strong material runs out; and
- never exceeds 1,800 seconds.

Chronology is the structural default. Highlight scores decide what survives within that structure; they do not reorder the trip or event into a best-first montage.

### Short edits

Short edits:

- use 9:16 output;
- act as independent highlight reels rather than mandatory trailers for the long edit;
- prioritize strong, vertically suitable moments across the full source batch;
- may focus on different themes, events, locations, or energy profiles;
- are generated only when distinct, high-quality material supports them;
- never exceed 180 seconds each; and
- use an automatic count with a configurable cap, defaulting to five shorts per batch.

Different shorts may not reuse source intervals with more than 10% temporal overlap relative to the shorter interval, and may not contain candidates in the same semantic deduplication group. The only exception is an explicitly configured anchor moment used at most once in two shorts; its overlap, semantic group, and planner rationale must be recorded. A source interval may appear in both the long edit and one short because the formats serve different purposes. Quality thresholds may produce fewer shorts or no shorts.

### Audio

Phase 2 uses source speech and ambience only. It does not select, license, generate, or mix music. Speech is interpreted only inside shortlisted candidate windows; the pipeline does not produce or retain a full searchable transcript.

## Architecture

```text
inspect → proxy → segment → analyze → rank → plan → render → validate → report
```

Phase 1 stages remain authoritative for discovery, source identity, proxy generation, rendering safety, output validation, persistence, and reporting. Phase 2 adds four analysis and planning stages:

1. `segment`: extract local scene, audio, motion, and technical-quality evidence.
2. `analyze`: run Gemini broad scanning and candidate refinement.
3. `rank`: fuse semantic and technical scores, enforce diversity, and remove duplicates.
4. `plan`: produce the chronological long edit, independent shorts, crop tracks, and transition decisions.

Each stage persists its inputs, settings, implementation version, status, evidence, cost, and artifacts. Resume reruns only incomplete, invalidated, or missing work.

## Local Segmentation

Local segmentation runs before cloud analysis and uses proxy video plus extracted analysis audio. It produces source-mapped candidate evidence for:

- shot and scene boundaries;
- silence and speech-presence ranges;
- audio intensity and transient peaks;
- motion magnitude and motion continuity;
- blur, shake, exposure, and obstruction penalties;
- duration and boundary suitability; and
- source-to-proxy timestamp mapping.

Local segmentation does not decide final highlight quality. It narrows the search space, supplies deterministic technical evidence, and creates safe chunk boundaries for cloud analysis.

Every interval maps back to original source timestamps. Rendering continues to read originals, never analysis proxies.

## Gemini Analysis

### Upload boundary

The cloud adapter may upload only registered generated proxy chunks with compressed audio. Path containment alone is insufficient. Before upload, the adapter requires a persisted proxy-manifest record whose job ID, source fingerprint, proxy digest, source-time mapping, generated-root location, video dimensions/frame rate, audio codec/bitrate, and artifact path match the file on disk. It rejects unregistered files, manifest mismatches, paths outside configured generated roots, and paths that resolve to an original source or input-root descendant. Originals never leave configured local storage.

Proxy chunks target 10–15 minutes and split at scene-safe or chronology-safe boundaries. A chunk carries an explicit mapping between proxy time and source time. The upload audit records the manifest ID and digest rather than treating a filesystem path as proof of provenance.

### Broad scan

The first pass uses Gemini static video processing at 0.5 FPS. It returns schema-constrained timestamped records for:

- scene boundaries and scene summaries;
- actions and motion;
- setting and location character;
- scenic interest;
- people and human interaction without identity recognition;
- speech-presence ranges without interpreting speech meaning;
- story milestones;
- visible technical problems;
- candidate highlight intervals;
- vertical suitability; and
- confidence.

Static sampling is the Phase 2 baseline because configurable FPS, clip offsets, predictable billing, stable cache keys, and repeatable output are more important than agentic navigation during initial production validation.

### Candidate refinement

Local evidence and the broad scan shortlist candidate windows. The second pass analyzes only those windows at a bounded 2–5 FPS selected from local motion intensity:

- 2 FPS for static scenery or conversation;
- 3 FPS for ordinary movement; and
- up to 5 FPS for fast action or short events.

Candidate refinement determines precise boundaries, action completeness, visual composition, novelty, semantic importance, duplicate similarity, vertical subject priority, crop intent, adjacent-scene compatibility, and candidate speech meaning.

Gemini supplies semantic subject priority and approximate visual evidence. It does not generate pixel-precise crop paths.

### Provider boundary

Gemini-specific request and response types remain behind an adapter. Provider-neutral validated records feed ranking and planning. This keeps the edit-plan boundary independent of one cloud API and permits later provider adapters without changing rendering.

Malformed JSON, invalid timestamps, missing required evidence, out-of-range intervals, or mismatched chunk identities fail validation and cannot affect planning.

## Cost Control

The hard cloud-analysis cap is USD 1.00 per source hour. The cap scales with total source duration and applies to all Gemini requests for one job.

Before any cloud request, the adapter computes a fail-closed maximum billable cost from pinned model pricing, maximum media tokens for the selected processing settings, maximum prompt tokens, and maximum output tokens. Before the first request, the sum of worst-case broad-scan reservations for every uncached valid chunk must fit the job budget; otherwise analysis does not start.

For each request, one database transaction:

1. checks for a reusable validated result;
2. reads current spent and reserved amounts;
3. verifies `spent + reserved + request_maximum <= job_budget`; and
4. atomically reserves the request maximum before dispatch.

Concurrent workers cannot spend the same remaining budget. On completion, the reservation is replaced by actual billed cost. On confirmed non-billable failure, it is released. When billing outcome is unknown, the reservation remains charged until reconciled. The adapter refuses requests whose provider pricing or token maximum cannot produce a conservative upper bound.

Cost degradation order:

1. reuse cache;
2. lower candidate sampling FPS within the approved 2–5 FPS range;
3. reduce repeated analysis of low-priority candidates; and
4. stop cloud analysis.

Broad-scan completeness is a hard gate: every valid proxy chunk representing every usable source interval must have one validated broad-scan result. Coverage is `validated broad-scan source seconds / total usable source seconds`; excluded seconds are allowed only for intervals already persisted as unreadable or unsupported during inspection, and those exclusions remain report warnings. Required coverage is 100% of the resulting denominator.

Ranking, candidate refinement, planning, and rendering cannot start below 100% broad coverage. When budget or service availability prevents complete coverage, the job records `budget_exhausted` or `analysis_incomplete`, emits no edit plans or media outputs, and reports exact missing ranges. Before the first request, broad-scan worst-case reservations must prove the full coverage gate can fit the budget.

The job stores estimated and actual token use, cost, model, sampling settings, and request identifiers.

## Caching and Persistence

A Gemini analysis cache key includes:

- source identity and fingerprint;
- proxy digest and mapping version;
- chunk source range;
- processing mode;
- FPS and media resolution;
- provider and exact model ID;
- prompt version;
- response schema version; and
- analysis implementation version.

Persisted analysis data includes:

- chunk identity and source-time mapping;
- Gemini file URI and expiry;
- upload metadata;
- validated request settings;
- token use and cost;
- raw validated response;
- normalized scenes and candidates;
- semantic feature values;
- local technical features;
- score breakdowns;
- deduplication groups;
- selected intervals;
- crop tracks;
- transition decisions; and
- plan/output relationships.

Gemini Files API expiry does not invalidate a completed validated response. Expired uploads are recreated only when additional cloud analysis is required.

## Highlight Ranking

Ranking combines normalized semantic and deterministic technical evidence. It produces an auditable score breakdown rather than one opaque model score.

Core dimensions:

- action and motion;
- scenic interest;
- human interaction and emotion;
- story milestone importance;
- technical quality;
- novelty;
- completeness of the event;
- long-story usefulness;
- short-form suitability; and
- vertical framing suitability.

Penalties include:

- blur or unusable exposure;
- accidental shake or obstruction;
- incomplete action;
- weak scene boundaries;
- repetition;
- near-duplicate visuals;
- near-duplicate speech or event meaning; and
- excessive overlap with a stronger candidate.

A diversity selector prevents one category, source file, event, or location from dominating. Deduplication uses temporal overlap, visual similarity, semantic similarity, and event identity. Every rejection retains a machine-readable reason.

## Story Planning

### Long planner

The long planner groups candidates into chronological chapters. It selects strong coverage inside each chapter and creates progression across the batch. It may omit whole chapters that contain no qualifying material, but it does not reorder surviving chapters.

The planner favors complete moments over isolated peaks. It preserves enough lead-in and resolution for actions and human events to make sense. It balances pacing without using weak filler to approach 30 minutes.

### Short planner

The short planner clusters strong vertical-friendly candidates into distinct reels. It automatically chooses zero to the configured maximum count. A new short is created only when enough non-duplicate material forms a coherent reel above the quality threshold.

Different shorts must pass the cross-short temporal-overlap and semantic-deduplication rules. Each short also stores a theme summary and selected-category distribution. The report exposes why each short exists, how it differs from the others, and any configured anchor-moment exception.

## Edit-Plan Extension

Each output remains an independently validated plan:

- `edit-plan-long.json`; and
- `edit-plan-short-01.json` through the configured short cap.

One plan contains one output specification. The workflow renders plans serially and reports relationships among outputs.

Each selected clip records:

- overall highlight score;
- per-dimension score breakdown;
- selection reason;
- Gemini analysis reference;
- source/proxy timestamp mapping;
- deduplication group;
- chapter or event identity;
- vertical suitability;
- confidence; and
- planner and analysis versions.

Plan validation enforces the 1,800-second long cap and 180-second cap for every short. It also enforces short-count limits and cross-short duplicate policy.

## Dynamic Vertical Reframing

Phase 2 adds a `tracked_crop` framing mode.

Gemini identifies the semantically important subject or region. A local detector/tracker generates timestamped crop-center keyframes from proxy frames. The crop path maps to original-source time and compiles into deterministic FFmpeg expressions.

Validation requires:

- crop bounds remain inside the source frame;
- crop dimensions match output aspect requirements;
- timestamps increase monotonically;
- crop-center velocity is no more than 0.25 source-frame widths per second;
- crop-center acceleration is no more than 0.50 source-frame widths per second squared;
- prioritized subject bounding regions remain inside the crop with a 5% frame-dimension safe margin for at least 95% of sampled frames;
- no fallback hold lasts longer than 2 seconds;
- path identity matches clip/source identity; and
- every tracked interval has a defined fallback.

The subject region comes from the validated local detector/tracker record linked to the clip. These numeric defaults are configuration-versioned and calibrated against the Phase 2 framing evaluation set; a release may tighten them but may not loosen them without a new evaluation. When tracking is lost, the path briefly holds the last valid position and then eases toward center. It never jumps immediately between unrelated positions. A candidate with unreliable tracking may use a validated best static crop or be excluded from short-form output.

## Transition Planning

Transitions describe semantic relationships, not visual decoration.

Every boundary has one enumerated semantic relation:

- `continuous_action`;
- `matched_motion`;
- `same_event`;
- `same_place_time_shift`;
- `chapter_boundary`;
- `time_jump`;
- `location_change`; or
- `story_open_close`.

Allowed transition mapping:

- `cut`: `continuous_action`, `matched_motion`, or `same_event`;
- `dissolve`: `same_event` or `same_place_time_shift`;
- `fade`: `chapter_boundary` or `story_open_close`; and
- `fade_black`: `chapter_boundary`, `time_jump`, or `location_change`.

A transition stores type, duration, enumerated semantic relation, reason, confidence, and audio-crossfade policy. Plan validation rejects every type/relation combination outside this table.

Rules:

- continuous action may use a clean hard cut;
- an unrelated scene may not use a plain cut;
- variety alone is not a valid transition reason;
- durations are bounded by transition type and adjacent clip lengths;
- visual overlap contributes to timeline duration calculations;
- audio crossfades prevent clicks and abrupt ambience changes; and
- fades must not hide an important action boundary.

The compiler extends current cut/dissolve support with deterministic fade and fade-through-black behavior. Invalid or unsupported transition primitives fail plan validation before FFmpeg execution.

## Error Handling

Local-stage failures retain prior completed stages and structured errors.

Gemini handling distinguishes:

- authentication/configuration errors: fail without retry;
- invalid request or schema errors: fail without retry;
- rate limits and transient server/network errors: bounded retry with backoff;
- upload expiry: recreate upload when analysis is still needed;
- malformed model output: bounded schema-repair retry within budget;
- budget exhaustion: explicit terminal analysis state; and
- partial chunk failure: report missing coverage and block planning unless completeness threshold passes.

No error path edits or deletes originals. Cleanup remains restricted to configured generated roots.

## Reporting

Phase 2 reports include:

- Gemini provider, exact model, and sampling settings;
- proxy chunks uploaded and proof that original paths were not uploaded;
- analysis coverage by source and duration;
- estimated and actual cloud usage/cost;
- candidate count and rejection reasons;
- score breakdowns;
- chapter and story structure;
- selected source intervals;
- deduplication decisions;
- crop strategy and tracking confidence per clip;
- transition type and reason per boundary;
- long output and zero-to-five short outputs;
- cache hits and resumed work;
- warnings, partial coverage, and fallbacks; and
- an explicit AI-selection quality disclaimer.

Report status uses authoritative final job state or clearly labels pre-completion snapshots so readers do not confuse a successfully reported job with a still-running job.

## Testing and Evaluation

### Automated tests

Unit and integration tests cover:

- proxy-only upload containment;
- source/proxy timestamp mapping;
- chunking and overlap rules;
- Gemini request settings and cache keys;
- strict response validation;
- worst-case cost calculation, atomic reservations, unknown-billing reconciliation, and hard-cap enforcement;
- transient retry and non-retryable failures;
- score fusion and rejection reasons;
- diversity and deduplication;
- chronological long planning;
- automatic short count and cap;
- cross-short temporal-overlap, semantic-duplicate, and anchor-exception validation;
- tracked-crop retention, safe margin, velocity, acceleration, hold-duration, and smoothing limits;
- lost-tracking fallback;
- transition selection and semantic constraints;
- audio/video transition compilation;
- 30-minute and 3-minute limits;
- 100% broad-coverage calculation, valid exclusions, and below-threshold plan/render blocking;
- resume and cache invalidation; and
- source immutability.

Cloud contract tests use recorded, sanitized provider responses. Live Gemini tests are explicit, budget-capped, and excluded from the default offline suite.

### Real-footage evaluation

A labeled evaluation set records:

- must-include highlights;
- acceptable highlights;
- weak or rejected moments;
- duplicate moments;
- chapter boundaries;
- preferred chronological order;
- vertical framing subjects; and
- acceptable transition classes.

Evaluation measures:

- must-include recall;
- weak-moment rejection;
- duplicate rate;
- chapter-order correctness;
- crop subject retention;
- abrupt unrelated-cut count;
- duration-limit compliance;
- short-to-short diversity; and
- human preference against the deterministic Phase 1 output.

Release evaluation uses a versioned labeled set containing at least 100 candidate intervals across at least three source batches, with separate train, calibration, and holdout partitions. Metrics use fixed formulas: must-include recall is recovered must-include intervals divided by labeled must-include intervals; weak rejection is rejected weak intervals divided by labeled weak intervals; duplicate rate is duplicate selected pairs divided by selected pairs; and abrupt-cut rate is unrelated plain cuts divided by all boundaries. Threshold configuration is committed before holdout evaluation, selected from calibration results to meet product targets, and recorded with the model/prompt/config versions. A release requires holdout results at or above configured thresholds; missing labels or insufficient holdout coverage blocks release. Duration, budget, source-safety, and schema constraints are hard gates regardless of subjective quality thresholds.

## Acceptance Boundary

Phase 2 is accepted when:

1. One automatic command processes authorized footage end to end.
2. The long edit is coherent, chronological, quality-first, 16:9, and no longer than 30 minutes.
3. The pipeline creates zero to five distinct 9:16 shorts, each no longer than 3 minutes.
4. Strong labeled moments meet the release-gate recall threshold.
5. Weak and duplicate moments stay within release-gate error thresholds.
6. Unrelated scenes never use an unjustified plain cut.
7. Dynamic crops retain designated subjects without erratic movement.
8. Cloud spend never exceeds USD 1.00 per source hour.
9. Resume reuses valid local and Gemini analysis artifacts.
10. Originals remain local and unchanged.

A passing evaluation applies only to tested footage categories and recorded Gemini model/settings. Reports must not generalize automatic editing quality beyond that evidence.

## Deferred

Phase 2 does not include:

- full-batch transcription or searchable transcripts;
- music selection, licensing, generation, or ducking;
- captions;
- synthetic voice, video, or images;
- face identification;
- a graphical interface;
- provider adapters other than Gemini; or
- automatic publishing.
