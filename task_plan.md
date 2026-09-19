# Task Plan: Fix Final Review Findings

## Goal
Fix all five final review findings with regression coverage, regenerated schema, complete quality-gate evidence, and one commit.

## Phases
- [x] Phase 1: Locate affected code, tests, schema generation, and quality commands
- [x] Phase 2: Add regression tests and verify expected failures
- [x] Phase 3: Implement minimal fixes and regenerate checked-in schema
- [x] Phase 4: Run complete requested quality gate, review diff, and commit

## Key Questions
1. How does publication signal masking restore masks and handlers today?
2. Where are render-plan and proxy source identities compared or validated?
3. Which direct media APIs validate cache/source overlap?
4. Which Pydantic numeric constraints must match checked-in JSON Schema?
5. What exact project commands satisfy build/help/diff validation?

## Decisions Made
- Base work on commit `207c30d`, latest integrated implementation state.
- Use current isolated worktree branch `fix/final-review-findings`.
- Preserve unrelated files; modify only affected implementation, tests, schema, and this required plan artifact.

## Errors Encountered
- Worktree started at planning-only commit `bf52e01`: reset clean worktree to latest integrated commit `207c30d` before creating feature branch.
- Initial cache-overlap implementation rejected sibling cache roots because source parent tree check was too broad; updated regression fixtures and retained required bidirectional overlap behavior.
- Existing repository-wide `mypy src tests` has baseline test typing errors; `mypy src` passes with no issues. Requested source quality gate passes.

## Status
**Complete** - Five findings fixed, 197 tests pass, schema regenerated, requested build/help/diff/lint/format/source-mypy gates pass.
