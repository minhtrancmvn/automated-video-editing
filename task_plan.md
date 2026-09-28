# Task Plan: Fix Vertical Crop and Resume Guidance

## Goal
Fill vertical output with center-cropped footage and document same-job resume/cache behavior.

## Phases
- [x] Phase 1: Reproduce and trace vertical framing and cache reuse
- [x] Phase 2: Confirm bounded design
- [x] Phase 3: Add failing framing and invalidation tests
- [x] Phase 4: Implement vertical crop and workflow invalidation
- [x] Phase 5: Update README resume guidance
- [x] Phase 6: Verify full quality gate and representative render

## Key Questions
1. Why does vertical output preserve large black regions?
2. Which version key invalidates stale plans and renders on resume?
3. Can same-job resume reuse valid proxy files after planner behavior changes?

## Decisions Made
- Default vertical sample plans to `center_crop`.
- Keep horizontal sample plans on `fit_background`.
- Bump planner version/settings so stale plans regenerate while completed proxy stages stay reusable.
- Bind render artifacts to plan digests and reject recovery when output predates its plan.
- Preserve valid same-job proxy media through existing content validation.
- Document `status`, `resume`, and new-run cache boundaries in README.

## Errors Encountered
- Version bump alone would invalidate proxy stage reuse; changed only planner version/settings instead.
- Existing render recovery accepted stale output after plan replacement; bound artifacts to plan digests and required recovered output to be newer than its plan.
- Existing recovery test expected old metadata shape; updated it to include the new plan digest.
- Full quality gate first stopped on three Ruff formatting differences; formatted those files and restarted the complete gate.
- First real center-crop integration render exposed a trailing comma before FFmpeg output labels (`No such filter: ''`); added a multi-clip compiler regression and joined labels directly to the final filter.

## Status
**Complete** - Vertical plan v2 uses center crop, real 144-second short fills 1080×1920, same-job resume reused all 72 proxy/audio files unchanged, and 216 tests plus Ruff, formatting, mypy, build, CLI help, and diff checks passed.
