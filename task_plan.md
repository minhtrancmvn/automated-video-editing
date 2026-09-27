# Task Plan: Add Console Stage Progress

## Goal
Show immediate job and stage activity in CLI stderr while preserving final command results on stdout.

## Phases
- [x] Phase 1: Inspect CLI and workflow output paths
- [x] Phase 2: Confirm bounded stage-progress design
- [x] Phase 3: Add failing callback and CLI stream tests
- [x] Phase 4: Implement minimal progress reporting
- [x] Phase 5: Run full quality gate and smoke test
- [x] Phase 6: Document CLI progress and reverify

## Key Questions
1. How can progress remain separate from machine-readable final output?
2. Where can every executed workflow stage report start and completion consistently?
3. How should failed stages avoid false completion messages?

## Decisions Made
- Emit progress through an optional workflow callback.
- Print callback messages to stderr; retain final result on stdout.
- Report job creation and stage start/completion only; no FFmpeg percentage parsing.
- Preserve current behavior when no callback is supplied.

## Errors Encountered
- Initial CLI test used unsupported `CliRunner(mix_stderr=False)`; switched to result stdout/stderr properties.
- Existing CLI service mocks accepted one argument; updated them for the progress callback parameter.
- Ruff found import ordering in `cli.py`; auto-fix moved `Callable` before `Path`.
- README update was requested during merge preparation; added CLI progress examples before commit.

## Status
**Complete** - Stage progress appears on stderr, final results remain on stdout, README documents CLI behavior, real GoPro inspection passed, and 213 tests plus Ruff, formatting, mypy, build, CLI help, and diff checks passed.
