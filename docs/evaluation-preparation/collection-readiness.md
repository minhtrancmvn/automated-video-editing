# Collection Readiness

Checked 2026-10-22 against existing project documentation and preparation records. Scope excludes unrelated personal directories and new media discovery. This is an evidence-availability finding, not proof that no other footage exists.

## Finding

**Only one known real collection, `trip-1`, can currently be registered from project evidence.** Its registered subset is exposed train/development material. No independent calibration or holdout collection is established. Do not generate additional batch IDs from files, GoPro chronology groups, excerpts, jobs, or repeated attempts.

| Project evidence | What it establishes | What it does not establish |
|---|---|---|
| [Benchmark record](../benchmark-records/2026-09-28/benchmark-record.md), authorization and input inspection | One authorized 18-file HERO12 batch, 10 chronology groups, 41,600.725834 seconds; scope limited to tested files/environment. | Independent collections, human labels, or release evaluation. Ten chronology groups are not ten evaluation batches. |
| [Phase 2 progress](../phase-2-progress.md), archived sandbox section | Full 18-file trip belongs to `trip-1`; technical subsets are not independent train/calibration/holdout batches. | Three-batch independence from processing subsets. |
| `source-manifest-attempt03.json` | Three persisted source records from current trip subset and historical hash references. | Fresh media integrity scan or a complete inventory of every collection. |
| `review-queue-attempt03-provenance.json` | All 16 saved model-proposed intervals assigned to exposed development batch. | Representative whole-source recall population, blind annotation, or pristine holdout. |
| [Synthetic evaluation fixtures](../../tests/evaluation/) | Offline formula/schema/guard tests. | Real collections, actual human judgments, or release-eligible thresholds. |

Prior Phase 1 vertical renders used first-eight-second sample planning. Their existence does not establish Phase 2 automatic-short quality or supply current short-diversity/crop evaluation evidence.

## DCIM expansion and partial owner feedback (2026-10-22)

User authorized expanding video pool using remaining DCIM footage. Metadata-only inventory at `/Volumes/ssd/DCIM/100GOPRO` found 18 MP4s totaling 41,600.725834 seconds and 152,659,041,281 bytes, including 15 filenames outside attempt03 subset. Count/bytes/duration match historical full-trip benchmark; no new trip independence inferred and no media content hash scan performed. See [development pool inventory](dcim-development-pool-20261022.json). Additional footage is available for development preparation, not registered calibration/holdout evidence or paid execution.

User also accepts portions included in prior `long.mp4`. Seven queue intervals match exact saved-plan spans and chunk-qualified identity, with plan digest linked to saved render report; [partial feedback](acceptable-feedback-attempt03.csv) records these judgments. Other required label fields and nine omitted queue judgments remain unknown. This is real output-exposed acceptance feedback, not complete or blinded release labels.

## Required external input

1. Supply at least two additional genuinely independent collections, with collection context, source locations, ownership/authorization, and independence rationale. If more collections are available, choose a justified split rather than minimizing to the numerical precondition.
2. Keep already exposed `trip-1` in development. Assign separate entire collections to calibration and untouched holdout before access or tuning; document actual exposure and assignment times.
3. Collect genuine human annotations across at least 100 intervals total using a recorded, full-source sampling protocol. Current 16 model-proposed windows are development review material only. Do not invent weak or must-include labels to fill metric denominators.
4. Preserve threshold freeze and holdout sequencing described in [labeling guide](README.md). No holdout footage should be examined during development preparation. Collection registration alone does not authorize paid analysis.

No additional independent batches were registered. Labels and annotation audit remain empty. Release stays blocked on labels, independent provenance, holdout-only scoring, and metric-threshold verification. This checkpoint does not authorize provider calls, threshold changes, commits, pushes, or media mutation.
