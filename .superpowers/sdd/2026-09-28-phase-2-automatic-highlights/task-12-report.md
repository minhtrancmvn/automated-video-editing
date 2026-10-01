# Task 12 Report: Nine-Stage Workflow, Resume, Reporting, and CLI

Status: BLOCKED (partial). Phase 2 stage wiring (segment, analyze, rank, highlight plan, v2 render, phase 2 validate) is not implemented. The 45-minute cutoff was reached. The contract tests are committed RED. Cloud-mode runs fail closed with `phase2_not_implemented`, so a cloud job never produces Phase 1 sample edits.

Base: 28a91fa. Branch: worktree-agent-a7f93fd5055037521.

## Commits
- a3c36c7 `test: define phase 2 workflow contract`
- 91df3a5 `feat: add phase 2 stage contract, safe reports, and cloud provider wiring`

## Impact (upstream)
- The user pre-approved these: WorkflowService LOW, _execute LOW, _ensure_render HIGH, write_json_report CRITICAL, cli._invoke CRITICAL.
- Run here: `_run_stage` MEDIUM (8 direct, 0 processes); `_stage_reusable` MEDIUM (5 direct; not edited in the end); `write_markdown_report` LOW (1 direct); `cli._service` LOW (1 direct).
- `detect_changes` (worktree, scope all, before the slice commit): risk low, 4 files, 0 affected processes.

## What landed
- workflow.py:
  - Adds `PHASE1_STAGES` (`STAGES` is kept as an alias), `PHASE2_STAGES` and `STAGE_IMPLEMENTATION_VERSIONS`.
  - `WorkflowService(..., *, provider=None)` and `self.stages` are selected by `cloud_enabled`. Progress `[n/N]` uses the active stage tuple.
  - After `complete_job`, the JSON/Markdown reports are rewritten from the authoritative completed job.
  - Cloud mode fails closed.
- reporting.py:
  - `snapshot_status` is `final` when the status is completed, otherwise `pre_completion`.
  - Secret keys are dropped recursively.
  - `source_path`/`artifact_path`/`generated_root` are stripped from the `analysis` section.
  - Phase 2 adds an Analysis section and an AI quality disclaimer.
  - Phase 1 disclosures are unchanged.
- cli.py:
  - Cloud mode builds `GeminiAdapter.from_environment()`, which reads the runtime key only.
  - Errors print `category [code]: message`.
  - No new flags.

## RED (a3c36c7)
- Focused run: 5 failed, 19 passed.
  - The Phase 1 tests passed.
  - The 5 failures were the new workflow/reporting/CLI tests.
- `test_phase2_workflow.py` failed at collection with `ImportError` (PHASE2_STAGES), which was intended.

## GREEN (91df3a5)
- Focused run: 25 passed, 8 failed.
- The 8 failures are all in `test_phase2_workflow.py` and need the Phase 2 stage wiring. They are: stage order/outputs, incomplete coverage, budget exhaustion, report safety/final, resume reuse, prompt invalidation, ranking invalidation, and missing-short rerender.
- `test_provider_auth_failure_is_redacted_and_key_never_persisted` passes, but only because cloud mode fails closed before any provider call. It must be re-verified once wiring lands.

## Gates
- Full offline (`-m 'not gemini_live'`): 763 passed, 8 failed (the same 8), 1 deselected.
- `mypy src`: no issues (33 files).
- `ruff check src tests`: all checks passed. Scope format: clean.
- `uv build`: ok. `video-editor --help`: ok. `git diff --check`: clean.
- No docs plan, task_plan.md, CLAUDE.md or AGENTS.md drift.

## Phase 1 test edits
- No existing assertions were changed. Only new tests were appended.
- `test_phase2_workflow.py` needed one fix after the RED commit: B008 (module-level default cap).

## Remaining work and concerns
1. Phase 2 stage wiring still needs these pieces, which have no settled design yet:
   - `_ensure_segment`: the cloud chunk ranges via `plan_chunk_ranges`, `create_cloud_proxy_chunk`/`register_proxy_manifest`, and `segment_media`.
   - `_ensure_analyze`: initialize the budget, build the broad/candidate requests, and raise `analysis_incomplete`/`budget_exhausted` with `safe_details.job_id` and missing ranges.
   - `_ensure_rank`: convert refinement responses (chunk proxy time) to `RankingCandidateEvidence` (source time).
   - `_ensure_plan` (Phase 2): `track_subject` plus `create_highlight_plans`.
   - Per-output render keyed by `plan.output.filename`.
   - `validate_phase2_output`.
2. Interface gaps that need a design decision before wiring:
   - The `AnalysisProvider` protocol has no cost estimator. `estimate_broad_request_maximum` exists only on `GeminiAdapter`, so the workflow cannot derive reservation maximums for a fake provider.
   - `CandidateRefinementResponse` carries no subject boxes, so `track_subject` has no `SubjectObservation` seeds.
   - No refinement-to-ranking-evidence mapping (scores, penalties, `evidence_ids`) is specified.
   - No transition-evidence source is specified.
3. The report and CLI behavior changes affect all six CLI commands through `_invoke` (CRITICAL, user-approved). Phase 1 output is unchanged except for the added `Snapshot:` line and the final-status rewrite.

## Round 2: Glue Rulings 1-4 and Stage Wiring

Final status: DONE. All 9 tests in `test_phase2_workflow.py` pass and Phase 1 stays green. Round 3 below resolved the blocker that this round originally ended on.

### Commits
- 0a36601 `feat: add cost bounds, candidate-v2 subject seeds, and evidence/transition maps`
- `feat: orchestrate resumable phase 2 workflow`. This squashes the round-2 WIP commit with the round-3 fixes.

### Rulings
1. `AnalysisProvider.maximum_request_cost(manifest_id, chunk, *, prompt_version)`.
   - The Gemini adapter delegates to `estimate_broad_request_maximum`. Candidate requests are bounded at x10, from the 5 FPS vs 0.5 FPS ratio.
   - With no pricing configured, the adapter raises `provider_pricing_unavailable`. Production pricing for gemini-2.5-flash is currently empty, so real cloud runs fail closed at the reservation step.
2. Refinement schema is now `candidate-v2`, which adds `subject_boxes` (time, x, y, w, h, priority).
   - Boxes must lie inside the frame and inside the candidate interval.
   - `candidate-v1` is rejected by the schema Literal and by the prompt allow-list. `test_gemini` keeps a v1 case as the "unsupported" proof.
   - Boxes are used only as `gemini_seed` tracking observations.
3. `analysis/evidence.py` holds `EVIDENCE_MAP_VERSION = "evidence-map-v1"` (the fixed table is in the docstring). It is recorded in the rank-stage settings fingerprint.
4. `derive_transition_evidence` (`transition-map-v1`) applies the relation rules in the ruled precedence order. Relation-to-kind choices stay inside the planner's `ALLOWED_TRANSITIONS`.
- Tests: new `tests/unit/test_evidence.py` (9 passed). The v1 to v2 bump touched `test_gemini.py`, `test_analysis_orchestrator.py` and the refinement fixture. Unit suite: 741 passed, 1 skipped.

### Wiring (workflow.py)
- New stages:
  - `_ensure_segment`: `segment_media`, plus cloud chunks via `plan_chunk_ranges`, `create_cloud_proxy_chunk` and `register_proxy_manifest`.
  - `_ensure_analyze`: budget init, then reservations from `maximum_request_cost`, then `run_broad_analysis` and `run_candidate_refinement`. Incomplete coverage or exhausted budget raises a structured gate error.
  - `_ensure_rank`: evidence-map-v1, then `rank_candidates`.
  - `_ensure_highlight_plan`: `track_subject` seeded by subject boxes, then `create_highlight_plans` with derived transitions.
  - `_ensure_render_v2`: one output per plan, named by `plan.output.filename`, expected final persisted before render, valid outputs kept.
  - `_ensure_validate_v2`: `validate_phase2_output`.
- Stage plumbing:
  - Each stage stores an `upstream` hash so downstream fingerprints chain.
  - Per-stage versions apply only in cloud mode. Phase 1 keeps `phase1-workflow-v2`.
- Gate failures:
  - They write an `analysis-gate.json` artifact holding `{code, missing}`.
  - `status()` merges that record into `stages.analyze.error`, because `JobStore.fail_stage` cannot store a code and database.py is outside the allowed files.

### Round 3: Segment Blocker Ruling and Completion
- Root cause, found by the controller: the Phase 1 proxy re-encodes at 15 FPS and emits 2 trailing frames. Decoded duration is 8.133s against an 8.0s mapping.
- Ruling: `segment_media` now accepts a positive decoded overshoot of up to `max(0.05s, 2/fps)`. The negative tolerance stays at 0.05s.
  - Samples at or after `proxy_end` are still dropped, so evidence stays anchored to the mapping.
  - Frame rate comes from a new private `_frame_rate(proxy)` helper. The `_read_video` signature is unchanged, so the existing drift monkeypatch test still works.
  - Impact (controller): `segment_media` LOW, 0 callers or processes.
- RED, then GREEN, in `test_segmentation.py`:
  - `test_segmentation_accepts_two_frame_positive_decoder_overshoot` failed before the fix and passes after it.
  - `test_segmentation_rejects_three_frame_decoder_overshoot` passes both before and after the fix.
- Two workflow fixes were needed to get the integration tests green:
  - `_ensure_segment` sets the job's own cache directory `cache/<job_id>` to mode 0700. `create_cloud_proxy_chunk` requires a private, job-owned generated root, and the Phase 1 proxy stage creates that directory with default permissions.
  - Crop-track keyframes (`time`, `center_x`, `center_y`) are quantized to 1e-6 by `_plain_track` before planning. Tracking Decimal arithmetic produced `0E-29`, and the v2 plan loader rejects exponent notation. The same quantized tracks feed both planning and validation.
- The auth-redaction test now asserts that the provider was really called (`broad_calls >= 1`). The fake raises `provider_auth`, the job ends `analysis_incomplete`, and `stages.analyze.error.code` is `analysis_incomplete`. The secret is absent from the status, the error and the SQLite bytes.

### Gates (final)
- `test_phase2_workflow.py`: 9 passed (about 120s).
- Full offline suite (`-m 'not gemini_live'`): 782 passed, 1 deselected.
- `mypy src`: clean (34 files). `ruff check`: clean. `ruff format --check src tests`: clean.
- `uv build`: ok. `video-editor --help`: lists 6 commands. `git diff --check`: clean.
- GitNexus `detect_changes` (worktree, all): low risk, 0 affected processes.
- The GitNexus index does not contain gemini.py symbols, so impact on `GeminiAdapter`/`_prompt` returned "not found".

### Concerns
- Candidate cache identities suffix `implementation_version` with the candidate ID. Without this, Task 7's candidate cache key would collide when several candidates share one chunk.
- `_artifact_valid` now accepts v2 plans and also checks the render's plan digest.
- `status()` merges the `analysis-gate.json` record (`code`, `missing`) into the failed stage's error. This side file stays as directed.
- Real cloud runs still fail closed at the reservation step until gemini-2.5-flash pricing is pinned.
- The Phase 2 integration tests are slow, about 120s for 9 real-ffmpeg tests.
