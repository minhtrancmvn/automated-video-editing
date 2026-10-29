# Task 14 Local Evaluation Preparation

Status: **preparation only; release blocked**. Prepared 2026-10-22 for project owner and future evaluators. No real labels, new model calls, threshold changes, or release attestation supplied. Accepted attempt03 long video is one technical acceptance sample from one trip, not a release dataset.

## Files and contract

- `labels.csv`: header-only human-annotation worksheet. Its 15 columns match `EvaluationLabel` fields exactly. It is not directly accepted by evaluation CLI.
- `batches.csv`: collection and partition registry. Contains one known `trip-1` development/train registration, recorded after prior prediction/output exposure; reviewer and review-start fields remain blank. This is preparation metadata, not evaluator input.
- `source-manifest-attempt03.json`: three persisted source identities, durations, and historical whole-file hashes. Bounded-v1 source IDs are not whole-file SHA-256 values. No fresh media scan.
- `review-queue-attempt03.csv`: 16 unlabeled source-local intervals from all saved broad candidates, without scores, descriptions, ranking, or selected flags. This is a biased model-proposed development queue, not human labels or pristine holdout.
- `review-queue-attempt03-provenance.json`: exact candidate/chunk linkage and explicit exposure/sampling limits. `review-queue-checksums.sha256` protects queue, source export, provenance, and current batch registry against later drift.
- `annotation-audit.csv`: header-only review/adjudication log. Keep separate from strict label objects; extra fields are rejected.
- `evidence-inventory.json`: seven local result-summary pointers with freshly computed digests, supported claims, and missing attestations. It is not a trusted gate input. Source footage/media were not rescanned.
- `evidence-checksums.sha256`: detached SHA-256 manifest for summaries, contract files, and inventory. Absolute local paths require mounted evidence volume and this checkout. Matching checksums detect drift, not trusted authorship or release approval.
- Existing input schema: [evaluation schema](../../tests/evaluation/schema.json). Runtime models and cross-field validation: [evaluation.py](../../src/video_editor/evaluation.py).
- Existing synthetic fixtures under [tests/evaluation](../../tests/evaluation/) test plumbing only. Never copy their judgments or thresholds into real evidence.

Label and annotation-audit worksheets intentionally contain no placeholder judgments. Blank means unreviewed, not false, zero, or no subject. Batch registry now contains factual development registration only; it records no human review or new independence claim.

## Current development queue

Open `review-queue-attempt03.csv` to locate existing source windows, then review source context before entering any judgments in `labels.csv`. Use queue interval IDs unchanged when recording labels and audit entries; do not copy `label_status` or `source_file` into strict label JSON. All 16 saved broad candidates are included, including ones never refined or selected; no ranking-based filtering occurred. However, model-proposed interval boundaries and already exposed footage remain selection-biased. This queue cannot measure missed must-include events across the whole source. Genuine full-source sampling and independent collections remain required.

Only `trip-1` is currently registered. Existing-project inventory search found no evidence establishing further independent collections; see [collection readiness and required input](collection-readiness.md). No calibration or holdout batch exists. Train assignment was recorded during this preparation, not backdated before prior analysis. Existing evidence inventory describes the preceding preparation snapshot; its `not_assigned` field is historical and superseded for this train registration only. It remains valid that no release partition set or threshold freeze is established.

## Expanded development pool and owner feedback (2026-10-22)

User requested remaining DCIM footage be added to video pool. [DCIM metadata inventory](dcim-development-pool-20261022.json) now lists all 18 MP4s under `/Volumes/ssd/DCIM/100GOPRO`: 152,659,041,281 bytes and 41,600.725834 seconds (11h 33m). Fifteen filenames are outside attempt03's three-file subset, totaling 34,004.537234 seconds (9h 27m). LRV/THM companions are excluded from primary footage count. This expands local development inventory only; no files were copied, analyzed by a provider, or registered into a new job. Names/count/bytes match historical trip inventory, but source content hashes were not rescanned. No new independent collection or holdout assignment established.

Owner states portions included in accepted `long.mp4` are acceptable. [Verbatim feedback](user-feedback-attempt03.json), [exact saved-plan mapping](accepted-intervals-attempt03.json), and [seven partial judgments](acceptable-feedback-attempt03.csv) record `acceptable=true` for queue IDs **001, 002, 004, 006, 007, 008, 010** only. Each matches exact source endpoints and chunk-qualified candidate identity; saved plan SHA-256 matches render report's plan digest. Video bytes and rendered boundary fidelity were not revalidated here. Remaining nine queue intervals stay unreviewed, not rejected.

Original queue and its checksums remain unchanged as preparation snapshot. Apply feedback CSV as an overlay when reviewing; its seven judgments are output-exposed development feedback, not complete `EvaluationLabel` rows or blind annotation. Strict `labels.csv` and `annotation-audit.csv` remain header-only because required fields were not supplied. Do not default unknown must-include, duplicate, chronology, box, or transition fields. [Pool/feedback checksums](pool-feedback-checksums.sha256) cover new records separately without rewriting earlier evidence manifests.

## Human labeling workflow

1. Register genuinely independent collections in `batches.csv` before labeling. Record collection description, independence rationale, source manifest path and SHA-256, and a reviewer. Files or excerpts from the same trip remain one batch. Current trip cannot become three batches by renaming files.
2. Assign whole batches to `train`, `calibration`, or `holdout`; record assignment before evaluation access. Every row in a batch must use the same partition. At least three independent batches and 100 candidate intervals total are release preconditions, not sufficient proof. Each partition must be populated. Decide a justified sampling protocol and record excluded intervals; do not choose only model-selected highlights.
3. Keep current accepted trip out of pristine holdout: model predictions/output and user feedback have already been seen. Record contamination/exploratory status. Do not backdate holdout access, threshold freezes, or reviewer activity.
4. Review source intervals with surrounding context, hiding model scores, selected IDs, and generated edits while assigning labels. Use explicit source IDs tied to the source manifest and source-local decimal-second offsets, not chunk-relative offsets. Record reviewer, reviewed interval, time, confidence, and adjudication in the audit log. Do not use model boxes or choices as human ground truth.
5. Fill every required label field after review. Resolve disagreements and duplicate references before export. Labeling may legitimately produce no must-include or weak intervals in a subset; do not invent them to satisfy metric denominators. Report unsupported metrics as unavailable.
6. Use training data for development and calibration data for choosing thresholds. Record exact model/prompt/config/dataset versions and calibration evidence digest. Threshold freeze needs a real commit and timestamp before holdout access. No commit is authorized by this preparation task; freeze remains pending.
7. Only after a separately verified freeze should designated evaluators access untouched holdout footage, annotate it without model predictions, and record actual first access. Preserve holdout labels from developers during tuning. Once seen or used to change behavior, that holdout is not pristine for another release attempt.
8. Export completed, adjudicated labels into a new JSON dataset matching current schema. Keep `release_eligible=false` and `predictions=null` during preparation. No exporter is provided here; never pass raw CSV to existing CLI. Before any future exporter or release-capable evaluator implementation, add tests for parsing booleans/nulls, decimal precision, partition isolation, and provenance.

## Label field guide

| Fields | Required meaning and encoding |
|---|---|
| `interval_id` | Unique nonempty ID across dataset. Not a bare provider candidate ID, which can repeat across chunks. |
| `batch_id`, `source_id` | Nonempty references to independent collection and immutable source identity. |
| `start`, `end` | Finite source-local decimal seconds; start >= 0 and end > start. Export as decimal strings without float round trips. |
| `partition` | Exactly `train`, `calibration`, or `holdout`; fixed per batch. |
| `must_include`, `acceptable`, `weak` | Explicit lowercase `true`/`false` in worksheet. Must-include requires acceptable=true and weak=false. Do not default blanks. |
| `duplicate`, `duplicate_of` | Boolean and another interval ID in same batch, or literal `null`. Duplicate must equal whether duplicate_of is non-null; no self-reference. |
| `chapter`, `order` | Nonempty chapter identifier and nonnegative integer chronology order. |
| `subject_boxes` | JSON array of normalized `{x,y,w,h}` objects, each inside full source frame; not runtime `{x_min,y_min,x_max,y_max}` boxes. Empty array only after review concludes no boxes annotated. Schema has no timestamps/subject IDs, so this field alone cannot establish framewise crop retention. |
| `acceptable_transitions` | JSON array containing any of `cut`, `dissolve`, `fade`, `fade_black`; labels describe entry into this interval. Empty array is a reviewed decision, not a missing default. |

Use a CSV-aware editor: JSON arrays with commas or quotes require CSV quoting. Audit and batch-registry columns are never inserted into strict label JSON. Candidate intervals and box judgments require human input; preparation does not synthesize either.

## Dataset envelope and validation

A completed dataset needs `schema_version` equal to `evaluation-v1`, nonempty `dataset_version`, exact `model`, `prompt_version`, `config_version`, `release_eligible`, nullable `holdout_started_at`, `rows`, and optional/null `predictions`. Use genuine version identifiers; no placeholder strings promoted to provenance. Current dataset model permits empty rows, but an empty validated envelope is not labeled evidence.

From feature checkout, validate a future label file locally (this validates labels, not release):

```bash
.venv/bin/python -c 'from pathlib import Path; import sys; from video_editor.evaluation import EvaluationDataset; d = EvaluationDataset.model_validate_json(Path(sys.argv[1]).read_text()); assert d.release_eligible is False; assert d.predictions is None; print(f"Validated {len(d.rows)} preparation labels; release remains blocked")' /absolute/path/to/reviewed-labels.json
```

Do not set `release_eligible=true` to make a file pass. Checked-in `thresholds-v1.json` is explicitly synthetic, unfrozen, and not usable for release. Existing CLI needs predictions and reports `metrics_status=unverified_input`; it does not ingest these worksheets.

## Evidence and metrics still needed

| Metric/claim | Evidence requirement and current limit |
|---|---|
| Must-include recall / weak rejection | Independently reviewed labels plus selected-ID mapping; denominator is all must-include / all weak intervals, respectively. |
| Duplicate rate | Verified selected duplicate links divided by all selected pairs, not selected interval count. |
| Transition quality / chronology | Plan and rendered-boundary linkage plus human transition/order labels; boundary completeness is not independently attested by current harness. |
| Crop retention | Frame/time-linked sampling policy, source identity, subject evidence, and measured retained/total counts. Interval-level untimed boxes alone are insufficient. No short from attempt03 means no vertical-output acceptance. |
| Short diversity | At least two real short outputs needed for pair denominator; no shorts must remain missing evidence, not perfect diversity. |
| Human preference | Separate blinded baseline-versus-generated comparison protocol and trial/win records. “Video looks good” is acceptance feedback, not a preference win or trial. |
| Cost and source safety | Source/upload/output digests and ownership plus actual linked billing evidence. Token-derived local ledger is not an independent provider invoice. Preserve unknown-billing holds. |
| Threshold/holdout provenance | Matching exact versions, real calibration digest, committed freeze before holdout, independently verified batch isolation and holdout-only scoring. |

Zero denominators yield null metrics, not perfect scores. Existing exploratory evaluator scores supplied rows without holdout-only filtering and does not apply threshold values to computed metrics. Release gate deliberately ends with `verified artifact and threshold provenance required` after metadata preconditions. Preparation must not weaken that guard.

## Preparation verification

Verified 2026-10-22: label and annotation-audit worksheets remain header-only; batch registry contains one exposed train batch with no reviewer or review-start claim. All 16 queue windows match saved source-local intervals and fit recorded source durations; three source-record exports match persisted records. Label columns exactly match runtime model; checked-in JSON schema matches generated schema; relative documentation links resolve; all seven summary digests and both detached checksum manifests match. Evaluator, schema, and synthetic threshold fingerprints are unchanged. Existing evaluation tests passed **11/11**. Synthetic CLI still reports `release_status=blocked` and `metrics_status=unverified_input`. Independent contract review found no required corrections. No production behavior changed, so whole-suite 962-pass closeout remains earlier evidence, not a new full-suite run for this package.

Pre-commit recheck on 2026-10-22 verified local manifest entries, JSON/CSV syntax, header-only labels/audit, one batch, and 16 queue rows. Seven external result summaries under `/Volumes/VideoEditorState20261020Attempt03/` were unavailable, so their digests could not be reverified. The preceding paragraph records the earlier preparation check, not fresh verification of unavailable evidence. Do not reuse those summaries as verified until the volume is available and all hashes match.

From checkout, recheck existing contract tests and local evidence digests without modifying jobs or media:

```bash
.venv/bin/python -m pytest tests/unit/test_evaluation.py -q
shasum -a 256 -c docs/evaluation-preparation/evidence-checksums.sha256
shasum -a 256 -c docs/evaluation-preparation/review-queue-checksums.sha256
.venv/bin/python -m video_editor.evaluation --labels tests/evaluation/synthetic-labels.json --thresholds tests/evaluation/thresholds-v1.json --expect-blocked-release
```

Missing mounted paths or a changed digest must stop evidence reuse; investigate before replacing inventory. Absolute paths and hashes in this package are local recovery pointers, not portable or authenticated attestations.

## Next implementation boundary

Future release-capable work needs independently verified artifact/billing/threshold linkage, holdout-only scoring, applied threshold checks, and real labels/human studies. This package supplies none of those attestations and grants no provider, commit, push, media mutation, or threshold-change authorization. Current local checks can confirm schema alignment and guard behavior only.
