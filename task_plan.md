# Task Plan: Task 6 Fix Round 1B

## Goal
Apply specified Task 6 commits, fix reservation identity, aggregate attempt/usage/cost accounting, and live-test safety with observed RED-GREEN evidence.

## Phases
- [x] Phase 1: Move worktree branch to feature HEAD and cherry-pick required commits
- [x] Phase 2: Read review, brief, design, report, and inspect affected code/tests
- [x] Phase 3: Run GitNexus impact checks and focused RED tests
- [x] Phase 4: Implement focused fixes and offline tests
- [x] Phase 5: Run required focused/full offline verification, type/lint/format/build/diff/GitNexus
- [x] Phase 6: Append report, commit, and deliver evidence

## Key Questions
1. Which analysis interfaces and Gemini adapter paths currently allow dispatch without reservation context?
2. How are retries, schema repair, usage, request IDs, and costs currently represented?
3. Which Task 2 pricing functions can calculate aggregate Gemini cost without duplicate catalogs?
4. How must live test ordering guarantee preflight/reservation before upload and cleanup in `finally`?

## Decisions Made
- No subagents, per user request.
- Do not run `gemini_live`; run offline suites with `-m "not gemini_live"`.
- Preserve prior 1A security changes and keep provider implementation Gemini-only.
- Touch budget/pricing/database only if necessary and only after mandatory impact analysis.

## Errors Encountered
- Worktree began at `a6aef56`, not requested `9d9ef30`; reset safely after preserving plan, then cherry-picked required commits.
- First RED run failed collection because `AnalysisRequestContext` did not exist, proving reservation contract absence.
- Initial generation retry refactor exposed stale error context and one indentation regression; fixed by mapping outside handler scope and reran focused suite.
- First computed media-token test mixed float FPS with Decimal duration; converted FPS through decimal text and reran focused suite.
- Full-project Ruff format check targets Markdown code fences in the implementation plan; restored unrelated plan formatting and verified `src tests` formatting separately.

## Status
**Complete** - Offline gates, build, report, and GitNexus scope recorded; live test not run.
