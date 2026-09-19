# Task Plan: Fix Task 12 Findings

## Goal
Fix four remaining Task 12 findings without regressing prior recovery/storage fixes, add required regressions, pass all requested quality gates, and commit changes.

## Phases
- [x] Phase 1: Read current code, tests, and repository state
- [x] Phase 2: Implement interruption, disk-scope, GoPro grouping, and formatting fixes
- [x] Phase 3: Run targeted and full verification commands
- [ ] Phase 4: Review diff, confirm evidence, and commit

## Key Questions
1. Where does render interruption polling and process termination happen, and how can bounded SIGTERM/kill waiting be tested?
2. What exact paths are included by estimated peak storage, report wording, and benchmark sampling?
3. How does GoPro discovery assign chapters across sessions, and where should ambiguity warnings/grouping be preserved?
4. Which changed files fail Ruff or formatting, and what build/help/diff checks exist?

## Decisions Made
- Work only in current writable worktree; no agents.
- Preserve existing recovery/storage behavior outside required fixes.
- Use render-output-only scope consistently because current estimate models only encoded render outputs; sampling all generated workspace/cache/output bytes would falsely compare broader actual usage against a narrower estimate.
- Use source parent path as session metadata when it uniquely identifies repeated GoPro sessions; otherwise retain discovery-order session chunks and warn explicitly.

## Errors Encountered
- Initial worktree branch pointed at pre-implementation commit `bf52e01`; reset current isolated worktree to requested Task 12 baseline `df7f470` before edits.
- Initial broad `rg` command used unmatched zsh glob `README*`; reran searches against explicit project paths.
- Direct `pytest` and `python3 -m pytest` lacked installed test tooling; used locked `uv run` environment.
- First targeted test run exposed missing fake `terminate()` and one incorrect wait-call assertion; fixed test double and assertion, then reran successfully.
- Full-project `ruff format .` touched unrelated pre-existing unformatted files; restored those paths and retained formatting only on requested changed files.

## Status
**Currently in Phase 4** - Final evidence complete; commit pending
