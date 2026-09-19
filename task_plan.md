# Task Plan: Close Verified Whole-Branch Findings

## Goal
Fix all ten verified findings, add regressions including real FFmpeg interruption recovery, pass every requested quality gate, and commit focused changes.

## Phases
- [x] Phase 1: Read spec, implementation, tests, and map each finding
- [x] Phase 2: Add regression tests for safety, schema, cache, finalization, and interruption
- [x] Phase 3: Implement source preflight, root/state safety, codec/schema, cache, and signal fixes
- [x] Phase 4: Add and pass real FFmpeg workflow interruption/resume integration
- [x] Phase 5: Run full quality gate, review diff, commit, and report

## Key Questions
1. Where do render-from-plan preflight, source identity, root safety, codec validation, and state initialization live?
2. What metadata and probes define valid proxy/audio cache reuse?
3. How do validation/finalization signals and persisted workflow recovery interact?
4. Which checked-in schema constraints must change to match Pydantic?
5. Can resume reuse valid outputs after real FFmpeg interruption without rerunning them?

## Decisions Made
- Restrict Phase 1 `OutputSpec.codec` to `libx264`; unsupported values fail plan validation with stable plan category.
- Preflight every plan source as readable regular file and recompute `bounded-v1` identity before job/artifact creation.
- Compare every configured generated/state root against all source parents before opening SQLite or creating directories.
- Validate reusable proxy/audio artifacts through ffprobe and persisted source/settings/tool/mapping identity; regenerate invalid cache entries.
- Block SIGINT/SIGTERM around final rename and persistence callback so publication and artifact commit form one deferred-signal section.
- Preserve no-cloud behavior, external SSD checks, and pending real HERO12 validation.

## Errors Encountered
- Initial worktree started before implementation history; reset isolated branch to current implementation commit `9bb4d26` before edits.
- Baseline tests created untracked bytecode/render output because repository lacks ignore rules; removed generated files without editing `.gitignore`.

## Status
**Currently in Phase 5** - Quality gates passed; reviewing final diff before commit.
