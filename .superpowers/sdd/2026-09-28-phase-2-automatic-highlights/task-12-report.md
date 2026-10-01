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
