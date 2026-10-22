# Vertical Shorts Implementation Handoff

Snapshot updated **2026-10-12** after acceleration-headroom gate. Saved real-footage comparison remains the v3 run verified at2026-10-12T05:03:08Z; no v4 footage rerun. Audience: any Claude session continuing this project. This document supersedes earlier running-worker checkpoints for the tracking comparison. Historical context remains in [Phase 2 progress](phase-2-progress.md).

## Start here

**Current step is complete:** the standalone real-footage `crop-tracking-v3` comparison finished for all three selected candidates. No worker remains. It produced measurements, not videos. All three crops still fail short eligibility. Do not restart the completed comparison.

Work exclusively from the existing feature worktree:

```text
/Users/coffeemug/Programming/automated-video-editing/.claude/worktrees/feature-phase2-automatic-highlights
branch: feature/phase2-automatic-highlights
HEAD at snapshot: b8743d44af96baa53189549a03e134a8e3bee448
```

Latest implementation fixes are **uncommitted** here and absent from the main checkout's runtime. Use this worktree's `.venv/bin/python` and explicitly set `PYTHONPATH` to its `src` when invoking private scripts. Do not run them with main's Python environment. Do not remove, reset, stash, clean, or blindly stage this worktree. `AGENTS.md` and `CLAUDE.md` contain separate instruction changes of uncertain provenance.

## Completed sandbox

| Item | Verified state |
|---|---|
| Job | `ebc41996-05a2-476c-acc4-14fb7ebcd22c` |
| Job/stages | Completed, all nine stages |
| DB schema | v3 |
| Cached analysis | Two validated broad results; four validated candidate-v4 results |
| Historical requests | Eight rows, including a billed invalid v3 response and a released nonbillable v4 attempt |
| Recorded spend | USD 0.127634, usage-derived; not independently verified invoice |
| Reserved / remaining | USD 0 / USD 0.622393 |
| Long output | H.264, 1920×1080, 30 FPS, 180.5 s, 70,577,509 bytes |
| Long validation | FFprobe and full-file FFmpeg decode passed |
| Human feedback | User says long video is okay and no black bars visible |
| Raw warning | `persistent_black_bars` retained; do not silently clear it |
| Vertical output | None; quality/crop/theme gates prevented shorts |
| Release | Not release-ready; Task 14 independent evaluation incomplete |

Private state root:

```text
/Users/coffeemug/Library/Application Support/video-editor/phase2-ssd-sandbox-20261006
```

- DB: `<state-root>/state/jobs.sqlite3`
- Config: `<state-root>/config.toml`
- Evidence: `<state-root>/evidence/`
- Input: `/Volumes/ssd/video-editor/phase2-sandbox-20261006/input/fifteen-minute-excerpt.mp4`
- Proxy: `/Volumes/ssd/video-editor/phase2-sandbox-20261006/cache/ebc41996-05a2-476c-acc4-14fb7ebcd22c/71473a8dd942731e.proxy.mp4`
- Original output/report/plan: `/Volumes/ssd/video-editor/phase2-sandbox-20261006/output/ebc41996-05a2-476c-acc4-14fb7ebcd22c/`

Camera originals remain immutable. This sandbox input is already downscaled; successful processing does not establish full-resolution quality.

## Implementation plan and progress

| Step | Status | Implementation/evidence |
|---|---|---|
| Explain zero shorts | Complete | Overall-score, crop-flag and theme gates identified; raw vertical and completeness separately reported |
| Add observational diagnostics | Complete | `short_eligibility_diagnostics` in `planning/highlight_plan.py`; persisted in new plan/report results; legacy results say `not_recorded` |
| Fix instantaneous stop on tracking loss | Complete offline | Hold fallback decelerates within existing acceleration limit rather than abruptly resetting velocity |
| Preserve exact crop precision | Complete offline | `_plain_track` no longer quantizes to six decimals; plan `CropKeyframe` JSON serializes with `format(value, "f")` |
| Correct observation time origin | Complete offline | New `CropTrack.source_start` records candidate origin; retention lookup and static fallback use it; legacy `None` retains old behavior |
| Version changed tracking | Complete | Current `TRACKING_VERSION = "crop-tracking-v4"`; saved footage comparison is v3, not rerun. Future plan caches distinguish changed arithmetic |
| Run real saved-seed comparison | **Complete** | Three selected windows reprocessed locally; separate tracks/results saved; no DB writes, network, or renders |
| Resolve remaining Decimal acceleration residue | **Verified offline** | RED12overshoot cases,conservative1e-24smootherheadroom added; strictvalidator unchanged. Full926passed,review/build passed; no real-footage v4 rerun |
| Resolve poor subject retention | **Not solved** | New measured retention 66.7%, 0%, 0%; minimum remains 95% |
| Change short scoring/theme strategy | **Not approved/implemented** | Scores and event-first grouping remain independent blockers |
| Render/accept vertical short | **Not started** | No eligible short plan exists; do not force an output |

Latest offline gate after acceleration-headroom correction:

```text
926 passed, 1 deselected, 1 warning in 401.02s
All checks passed!
80 files already formatted
Success: no issues found in 36 source files
Successfully built dist/video_editor-0.1.0.tar.gz
Successfully built dist/video_editor-0.1.0-py3-none-any.whl
```

Known warning: fork in a multithreaded process in `test_forked_child_cannot_replay_parent_generation_authority`. Targeted precision/origin tests: 79 passed. Bounded static review passed; it did not independently rerun tests.

## Current comparison results

Comparison evidence directory:

```text
<state-root>/evidence/tracking-comparison-v3-20261012-attempt02/
```

Contains `preflight.json`, three `*.track.json`, three `*.comparison.json`, `result.json`, and `handoff-verification.json`. `result.json` completed at **2026-10-12T05:01:38Z**; task `buwgx8k0y` exited 0. Final verification checked source, proxy, completed output/report hashes, DB integrity/budget and retained request count. No worker remained at the final snapshot. Task IDs are session-local hints, not reliable cross-session process controls.

| Candidate / source window | Old retention | New retention | Old max velocity | New max velocity | Old max acceleration | New max acceleration | New eligible |
|---|---:|---:|---:|---:|---:|---:|---|
| `candidate-01`, 770–782 s | 5/9 (55.6%) | 6/9 (66.7%) | 0.25002917 | ≈0.25, below limit | 1.50045195 | 0.5000000000000000000000000345 | No |
| `candidate_1`, 490–585 s | 0/104 | 0/104 | 0.25002917 | ≈0.25, below limit | 3.75041875 | 0.5000000000000000000000000345 | No |
| `candidate_2`, 245–320 s | 0/37 | 0/37 | 0.25002917 | ≈0.25, below limit | 3.74970764 | 0.5000000000000000000000000345 | No |

Identity, bounds and monotonic timing pass. Limits unchanged: retention ≥0.95, velocity ≤0.25 crop widths/s, acceleration ≤0.50 crop widths/s², safe margin 0.05.

**Do not round these measurements to claim eligibility.** New acceleration remains slightly above the strict limit. Retention alone would still reject all three even if the numerical residue is fixed.

Scores also remain unchanged: 0.545902, 0.517428, 0.500349 versus short minimum 0.60. All three have completeness 0.80 and raw vertical 0.70–0.75, which pass. Fourth candidate `candidate-02` at 858–873 s is unselected (score 0.373554 < ranking minimum 0.45), with no generated crop track.

Each candidate has a distinct event theme. Planner groups event first, then location/category only when no event exists, and requires two eligible moments per theme. Do not invent a shared event or drop this requirement merely to obtain a short.

## Exact next implementation step

**Updated after numerical fix:** the acceleration-residue work described in steps2–4 below is now verified offline (926tests). Do not repeat it. Next investigate retention from saved observations and keyframes only: calculate whether any bounded9:16crop can contain each box with5%margin, then separate geometry impossibility from fallback/tracking failures. No v4real-footage rerun occurred, and low score/singleton themes remain independent blockers. Seek separate approval before rendering or policy changes. Following procedure remains historical reference for the completed arithmetic fix.

1. Read `result.json` and candidate comparison/track files from the completed evidence directory. Confirm feature runtime/version and inspect current Git state before edits.
2. Build a **small synthetic regression** from the 15 FPS keyframe segment exhibiting `max_acceleration = 0.5000000000000000000000000345`. Avoid decoding the entire proxy just to reproduce arithmetic. Trace `smooth_crop_track` and `_kinematics`; identify whether smoothing arithmetic leaves insufficient headroom. Do not modify validation limits or add acceptance tolerance without a separately justified decision.
3. Run required GitNexus upstream impact before editing symbols. MCP was intermittent; installed CLI works as fallback, for example:

   ```bash
   gitnexus impact smooth_crop_track --direction upstream \
     --file src/video_editor/analysis/tracking.py --summary-only \
     --repo /Users/coffeemug/Programming/automated-video-editing/.claude/worktrees/feature-phase2-automatic-highlights
   ```

   Prior impacts: smoothing CRITICAL (one direct caller, six flows); observation lookup CRITICAL (one caller, eight flows); `_plain_track` CRITICAL (one caller, ten flows). Warn before edits. Index can miss new/uncommitted symbols; report uncertainty instead of treating missing results as safe.
4. Follow RED → minimal GREEN. Keep strict acceleration/velocity/retention checks. Add a test demonstrating no false eligibility when actual motion exceeds limits. Run focused tests, Ruff, format check, source MyPy, full offline suite, build and bounded review. No automatic commit/push.
5. Investigate retention independently using saved boxes and keyframes: determine physically uncroppable boxes versus optical-flow loss, sparse seeds, fallback centers and safe-margin constraints. Report evidence, not speculation. Scores and theme grouping remain separate product-policy work.
6. Ask user before any new live request, threshold/theme policy change, actual short render, or rewriting completed job/report/cache state. Current authorization covers offline fixes and separate tracking comparison, not forced output.

## Resume / interruption procedure

- First inspect evidence `result.json`. If it exists and is valid, **do not rerun comparison**.
- Check actual worker processes before starting anything. The comparison runs via Python stdin, so filename-only `pgrep` can miss its child. Inspect process args, parent PID and the log together; recorded PID values can be reused by unrelated processes after exit.
- Completed attempt had shell PID 2901 / Python PID 2902; both exited by final verification. Never signal a recorded PID without verifying current identity.
- If a future comparison is interrupted, retain its folder and log. Reuse completed candidate results only after input/version/settings hashes match; otherwise use a fresh evidence folder. Existing runner refuses an existing output folder.
- SSD can disconnect. On reconnect, transient device number may change. **Standalone read-only content comparison is not a supported job resume or upload authorization.** Its script records device changes, checks mount/filesystem and source/proxy/content hashes without rewriting recorded job identities. Never bypass production resume/upload identity checks.
- Tracking currently decodes the full 900 s proxy per selected candidate and retains frames per call. Work is finite but slow/memory-heavy; repeated session exits caused earlier attempts to stop before saving the first result. No performance fix was implemented.

## Runner and evidence map

| File | Purpose |
|---|---|
| `<state-root>/evidence/tracking-comparison-20261008.py` | Original read-only runner; corrected comparison-specific proxy-duration check |
| `<state-root>/evidence/tracking-comparison-20261012.py` | Dated runner adds input-hash comparison to earlier verified preflight |
| `<state-root>/evidence/tracking-comparison-20261012-attempt02.log` | Successful three-candidate run log |
| `<state-root>/evidence/tracking-comparison-v3-20261012-attempt02/result.json` | Complete old/new metrics and short rejection diagnostics |
| `.../handoff-verification.json` | Independently rechecked hashes/ledger, result SHA and no remaining worker |
| `tracking-comparison-v3-20261008/`, `tracking-comparison-v3-20261012/` | Preserved interrupted attempts; preflight only, not completion evidence |
| `.superpowers/sdd/2026-09-28-phase-2-automatic-highlights/exfat/` | Focused implementation task plans and point-in-time reasoning |

Proxy fact: actual duration 900.133333 s at 15 FPS, mapping end 900.032467 s. The initial comparison added an unsupported ±50 ms duration test and stopped before tracking; corrected only that private diagnostic check to recorded codec/FPS/geometry and selected-window coverage. Difference is recorded in preflight. Production policy was not relaxed.

## Safety and unresolved project work

- No current paid scope. Existing paid approvals were consumed. Keychain service is `automated-video-editing-gemini`; do not retrieve key for offline work, print it, or persist it.
- Old archived job `8acede66-a717-4d53-b0e8-9686cb0fa167` retains unknown-billing hold USD 0.203670. Do not release or fabricate settlement.
- Keep storage reserve 10 GiB; never delete media/cache, reduce reserve or import job-owned artifacts to another job without authorization.
- Dirty feature work is not backed up to GitHub. Do not automatically stage unknown-provenance instruction changes. Latest work is not on main merely because branch HEAD equals main.
- Historical agent worktree archives: `/Users/coffeemug/Programming/automated-video-editing/resume-archive/worktrees-20261007/`. Branch refs and recovery patches retained; prior patch-equivalence audit found archived committed changes already represented in main. This does not imply dirty archive edits were integrated.
- Release evaluation still lacks independent holdout batches, linked quality/billing provenance and calibrated human labels. Long-video acceptance by user does not fulfill vertical-short or release acceptance.
- No Python TODO/FIXME/PLACEHOLDER/TBD/XXX/HACK markers found in modified/untracked Python files at snapshot. This is a marker scan, not proof all project tasks are finished.

## Entry points for another session

Read this document first, then [Phase 2 progress](phase-2-progress.md), [human handoff](../.continue-here.md), and [machine handoff](../HANDOFF.json). `/resume-work` should be run from the feature worktree. Main's checkout/runtime is intentionally older; the main-root pointer directs sessions here.
