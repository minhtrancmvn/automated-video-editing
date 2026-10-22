# Phase 2 Progress and Resume Guide

Last verified: **2026-10-22**. Audience: project owner and the next coding session. This is the canonical current-status document; update it after meaningful milestones, failures, paid attempts, or budget decisions. Historical implementation reports remain in the SDD ledger.

## Current checkpoint: accepted attempt03 long edit

User reviewed attempt03 export on 2026-10-22 and said “the video looks good.” This records acceptance of this long edit, not release approval or acceptance of vertical shorts.

- Job `1b4de182-583e-4978-911b-46cdc708b5f8` completed all nine stages. State: `/Volumes/VideoEditorState20261020Attempt03/phase2-trip-batch-20261020-attempt03/state/jobs.sqlite3`. Read-only closeout check confirms database integrity, no foreign-key violations, completed status, and unchanged ledger.
- Output: `/Volumes/ssd/video-editor/phase2-trip-batch-20261020-attempt03/output/1b4de182-583e-4978-911b-46cdc708b5f8/long.mp4`. FFprobe confirms H.264/AAC, 1920×1080 at 30 FPS, 260.766667 seconds, 160,255,231 bytes. Seven of eight refined moments selected; final shoulder stop rejected below ranking threshold. Reports and typed JSON plan sit beside output; do not substitute a manual EDL.
- No vertical short produced. All refined scores fall below the 0.60 short threshold, and selected crop tracks fail 95% retention. Thresholds remain unchanged. Accepted long output does not establish vertical-output quality.
- Validator warning `persistent_black_bars` remains recorded. Sampled frames suggest dark night footage affects detection; neither samples nor user acceptance prove the warning false over every frame.
- Billing: 11 completed broad requests plus 8 completed refinements, USD 0.515086 spent, USD 0 reserved, USD 0.422183 remaining under USD 0.937269 cap. Local rank/plan/render made zero provider calls. Remaining funds do not authorize new calls. Prior archived unknown-billing holds remain separate and untouched.
- Preservation evidence: `post-render-preservation-20261022.json` records 40 protected hashes checked with no mismatch at 2026-10-22T04:51:58.471937+00:00. This is the render-time scan, not a new closeout hash scan.
- Pending code includes chunk-qualified candidate identity and streaming crop tracking. Real planning previously exhausted memory by decoding an entire proxy; streaming completed the plan in 52 seconds with about 207 MB peak RSS. These changes remain uncommitted on `feature/phase2-automatic-highlights`; main lacks them. Preserve unrelated pending edits.
- October 12 acceleration and frame-index follow-ups were subsequently implemented with strict-limit regression tests and tracking-v4/v5 provenance. Do not restart those completed investigations from stale handoff text. Streaming review found no verified defect.
- Current offline gate: **962 passed, 1 live test deselected, 1 known multiprocessing/fork deprecation warning in 398.69 seconds**. Ruff lint passed; all 85 source/test files pass formatting after a formatting-only duplicate-ID test fix; source MyPy passed for 36 files. Duplicate-ID regression rerun passed. Offline wheel/sdist build, supported `video-editor --help`, patch whitespace, checkpoint/ledger validation, and synthetic blocked-release CLI passed. No provider, Keychain, media/DB mutation, commit, push, or worktree cleanup during closeout.

### Next task and release boundary

Task 14 local preparation is complete: [labeling workflow and worksheets](evaluation-preparation/README.md), seven-summary evidence inventory, and detached checksums. Follow-on registration adds one exposed `trip-1` train/development batch, three persisted source records with historical hashes, and all 16 saved broad-candidate intervals as an explicitly biased unlabeled review queue. Label and audit sheets remain empty. No calibration/holdout batch, human labels, or new release attestations created. Schema/worksheet/link/digest checks passed; existing evaluation tests passed 11/11; synthetic CLI still blocked. Evaluator and threshold files unchanged. Preparation records current trip as already exposed to predictions/output feedback, not pristine holdout.

Local acceptance closeout and current offline quality gate are complete. Keep Task 14 release evaluation blocked until independent artifact/billing/threshold provenance, holdout-only scoring, and real labels from at least three independent batches and 100 candidate intervals exist. Current footage belongs to one trip; additional files from that trip are not independent batches. Synthetic evaluation CLI still returns `release_status=blocked` and `metrics_status=unverified_input`. Do not fabricate labels, relax score/crop thresholds, run paid analysis, rerender accepted output, or commit/push without separate authorization.

## Historical checkpoint: October 12 sandbox and crop comparison

Following snapshot predates attempt03 and is retained as historical evidence. Its old next-action, paid-scope, running-state, and manual-review statements do not authorize new execution.

**Historical vertical-short step:** saved-seed tracking-v3 comparison completed on2026-10-12, separate evidence only. Read [Vertical shorts implementation handoff](vertical-shorts-handoff.md) for implementation plan, exact progress, resume procedure and next safe change. It supersedes older running-worker statements below. Three crops remain ineligible: retention66.7%/0%/0%, strict acceleration0.5000000000000000000000000345 slightly over0.50, scores below0.60 and singleton event themes. Velocity now passes. Input/proxy/completed output hashes and DB rows independently rechecked unchanged. No provider calls or renders. Latest source gate913passed/1live deselected/1forkwarning470.75s; build/lint/type/review passed. No worker remains; do not restart completed comparison.

**First sandbox long edit rendered and technically validated; local job complete. No vertical short produced. Manual quality review and release evaluation pending. Not release-ready.**

- Current job: `ebc41996-05a2-476c-acc4-14fb7ebcd22c`, direct exFAT SSD storage, schema v3. Two validated broad caches and four validated candidate-v4 caches; eight historical request rows, including one released nonbillable attempt and one billed invalid response.
- Budget: **USD 0.127634 spent, USD 0 reserved, USD 0.622393 remaining**. Previous request/cache/retry-link history and source SHA-256 preserved. Latest three refinements cost USD 0.065339; their upload-delete calls returned successfully, but remote absence was not independently queried.
- All nine persisted stages and job status are **completed**. Approved local resume used cache-only provider guard: **zero network method calls**, no extra request rows or billing. Interrupted plan restarted on same job; original log preserved. Read-only SQLite verifies8 request rows,schema3,spentUSD0.127634,reserved0,remainingUSD0.622393; source SHA and current volume identity match.
- Verified output: `/Volumes/ssd/video-editor/phase2-sandbox-20261006/output/ebc41996-05a2-476c-acc4-14fb7ebcd22c/long.mp4`, **1920×1080, H.264, 30 FPS, 180.5 s**, AAC stereo48kHz,70,577,509 bytes. Independent FFprobe and full FFmpeg decode passed. Plan SHA matches recorded artifact; source SHA unchanged. Report JSON/Markdown exist and reflect completed status.
- Three moments selected: source245–320,490–585,770–782s. No9:16short: each selected candidate fails score.total≥0.60 and has `crop_track.short_eligible=false`; completeness0.80 and raw vertical0.70–0.75 pass. Their event themes are distinct, so no group reaches2 moments. Candidate-02 was unselected at0.37355<ranking floor0.45 and has no track. Original saved tracks omit CropValidation submetrics. Fresh read-only saved-track recomputation identifies retention55.6%/0%/0% below95%,velocity0.25003above0.25,andacceleration1.50–3.75above0.50;identity/bounds/timingpass. These are fresh measurements,not original-run persisted metrics. No thresholds relaxed. Technical validator warns `persistent_black_bars`; user manually reports no black bars visible and long video is okay. Keep validator warning intact as recorded evidence, note user observation as disagreement, not a revalidation. Other warnings: unknown color metadata, uncertain chronology, skipped macOS `._` sidecar. User also requested offline diagnosis of short eligibility; results: short fail gates above, crop subreason unavailable. No project thresholds or artifacts changed by that investigation.
- Report caveats: `request_count:6` describes current logical2broad+4candidate workload, not8 historical ledger rows; `estimated_peak_space_bytes:1` is an uninformative reporting floor, not measured usage. Neither is evidence of quality/release completion.
- Durable evidence: `<fresh-state-root>/evidence/local-complete-result-20261007.json`, `local-final-verification-20261007.json`, `local-complete-resume-20261007.log`; final report beside video. SSD free about313GiB;10GiB reserve unchanged. Independent FFprobe+full decode passed; frame preview at5s verified, but complete human quality review remains. Original plan/report warning `persistent_black_bars`; preview confirms very dark night-driving scene, inspect entire 3-minute export before any publication. No vertical output was created; three selected candidates failed short quality score threshold0.60, and no thresholds were loosened. Supplementary warnings: skipped metadata `._` sidecar, unknown color metadata and uncertain chronology. Report fields are not production invoice/full request-count: `request_count=6` is two broad + four candidate logical requests, ledger retains eight historical rows incl released/invalid attempts; `estimated_peak_space_bytes=1` is a floor placeholder, not measured actual peak. Output audit includes complete artifact digests. Manual full-video review and vertical-output acceptance remain unfinished.
- Short diagnostics are additive observations in future plan/report results: per-candidate primary-gate reasons, theme counts, effective thresholds and recomputed crop metrics. Stored crop eligibility remains authoritative; legacy plans say `not_recorded`. Separate evidence `short-diagnostics-recomputed-20261008.json` came from read-only completed-stage data; original report/video not rewritten.
- Latest real-footage crop comparison is complete; see [vertical-shorts handoff](vertical-shorts-handoff.md). It used cached seeds, versioned tracking-v3 and separate evidence only. Source/proxy/completed-output hashes, DB rows, budget and original report/video remained unchanged. Exact velocity/acceleration decimal residue still misses limits slightly; retention remains low for two windows. No release/short eligibility claim.
- Latest offline source gate: **908 passed, 1 live deselected, 1 fork warning in799.52s**; lint/format77files, source typing36files, wheel/sdist build anddiffcheck passed; boundedreviewPASS. No production code edit during local resume. No new paid scope, commit or push.

### Earlier checkpoints

Following entries are historical, not current active execution state.

- Phase 1 local sample editor is complete.
- Phase 2 Tasks 0–13 are complete at the implementation/offline-test level. Task 14 is partial.
- Gemini broad scans succeeded on generated 1-, 60-, and 900-second clips. Standalone candidate refinement also succeeded.
- Production pricing and earlier Phase 2 implementation were committed, pushed, and merged to main.
- Additional integration fixes remain **uncommitted in the feature worktree**. Do not delete or clean that worktree.
- Old sandbox job remains `failed` in preserved ledger but is **archived, not locally resumable**. Its oldUSD0.203670 hold unchanged. Active SSD job **b9f89c3b-464e-4da5-a283-c61ea11ca1a8** runs directly on exFAT; inspect/proxy/segment completed locally, two generated chunks, no cloud requests or reservations yet. Source SHA-256 matched at last mounted verification; cannot recheck while SSD disconnected. Final publication/sampling gate passed. On2026-10-07 SSD initially absent, then reconnected with313.56GiB free. Source SHA matched but recorded device16777239 vs current16777240 blocks same-job resume. No identity rewriting or guard bypass. User explicitly selected separate fresh job with same two-broad-only cap. New job `ebc41996-05a2-476c-acc4-14fb7ebcd22c` completed local inspect/proxy/segment via distinct `broad-only-reconnected-20261007.py`; actual two-chunk maxima0.087351+0.047772=USD0.135123 and sourceSHA match verified. Approved one-shot broad-only dispatch completed: two requests settled USD0.049672 from returned usage, two validated broad caches,100% coverage over900.032467s, zero reservation, USD0.700355 remaining. Source SHA and previous ledgers unchanged. Upload-delete calls returned successfully through orchestrator finally path; remote absence not independently queried. Exclusive dispatch marker prohibits repeat. Four candidate windows identified; user approved5FPS/8192cap/one-attempt refinements up toUSD0.216075. Batch stopped on first candidate (tail candidate-01, proxy50–62/source770–782s): provider_response_invalid, schema_validation fieldsubject_boxes, finishSTOP. Returned usage settledUSD0.006257; one upload-delete call returned successfully; zero validated candidate caches. Remaining three calls not dispatched. New job total spendUSD0.055929, reserved0, remainingUSD0.694098. No retry or rendered output; old ledgers and broad caches unchanged. Request statuscompleted reflects billing settlement, not valid candidate semantics.
- Latest geometry hardening: explicit box rules in genuine `candidate-v4` prompt; historicalv3 unchanged, strictcandidate-v2 schema unchanged, broad-v1 identity unchanged. Pydantic error-type/index paths sanitized and bounded before durable persistence; no provider values/msg/context/unknown keys. RED8 failures then focused115passed/1live deselected, source lint/format73files,mypy36files; privacy review found no concrete defects. Full gatebc4dtmuy1 passed884tests/1live deselected/1fork warning in1127.26s; wheel/sdist build and diffcheck passed, artifacts verified. No paid retry. Exact historical box value cannot be reconstructed. Old ledgers/cache byte-row unchanged.
- User subsequently approved ONEcandidate-v4diagnostic source770–782/proxy50–62,5FPS8192cap1attempt,maxUSD0.038709. It stopped locally before SDK model generation:actual1133byte prompt exceeded1024reservedallowance. One upload/delete returned successfully; requestce4db4ce released with0recordedcost,jobspend0.055929 unchanged,no candidatecache. Wrappercountergeneration_calls1 reflects methodentry,not actual SDK generation. Never reset releasedkey/marker. Compactequivalentv4wording now993actualbytes,RED2real-IDfailures/GREEN117focused,allowance/costpolicy unchanged; offlinefullgatebbf047gv1 passed886tests/1live deselected/1forkwarning400.02s;Ruff73files,mypy36files,wheel/sdistbuild,diffcheckpassed. No paid retry currently authorized.
- Explicit confirmed-nonbillable retry lifecycle now implemented offline: schema3 preserves attempt rows, partial unique index prevents simultaneous nonreleased same-key attempts, predecessor/successor link preserves provenance. `reserve_retry_request` requires releasedactual0 predecessor,samejob/key/mode,newID and freshbudget underBEGINIMMEDIATE; normalreserve never auto-retries. Fake-provider test proves pre-reserved successor dispatch through existingorchestrator. Focused131passed,lint74files,mypy36files,diffpassed; boundedreviewclean aftercache-hit failclosed fix. Fullgatebuqps0mf3 passed902tests/1live deselected/1forkwarning465.51s;wheel/sdistbuildpassed. After separate userapproval, sandboxDBbackupjobs-before-retry-v3-20261007.sqlite3 integrity/row/SHA verified,then migratedv2to3 with allpriorrows preserved. Explicitsamekeysuccessor db9629a5 to releasedce4db4ce succeeded:exactcandidate-01proxy50–62/source770–782,3validsubjectboxes,candidate-v4cachepersisted. ReturnedusageUSD0.006366settled;jobspend0.062295,reserved0,remaining0.687732. Upload/delete-returned-success1each,sourceSHA/history/broadcaches/oldledgerspreserved. Nootherrefinement/render/commit/push. Remaining3v4oneattempt5FPS8192capmaxUSD0.177366 preflighted offline; no approval or dispatch yet.
- Pre-geometry completed full offline gate **875 passed,1 live deselected,1 fork warning in663.41s** (`/tmp/exfat-final-full2.log`, taskbs3yauq2r exit0). Render-lock test now uses explicit readiness/release synchronization (LOW impact,no production change); preceding full run failed that test with874 passed and remains historical. Focused final segmentation/proxy gate194 passed; runner9 passed. Ruff lint, source/tests formatting (71 files), mypy (35 source files), wheel/sdist build and diff check passed; CLI help passed at preceding checkpoint. Whole-repo formatting fails on unchanged historical-plan Markdown Python examples; that plan was not reformatted. One known multiprocessing/fork warning remains. Prior full runs intermittently failed the render-lock timing test; an isolated pass is not a full-suite pass.
- Latest paid diagnostic retry succeeded with exact interval and IDs, but its full response was **not imported into the batch cache**. Success does not mean the sandbox job completed.
- **Storage blocks execution:** latest post-cleanup snapshot is about 1.40 GiB free, below unchanged 10 GiB reserve by about 8.60 GiB before working/output space. User approved deleting only `.venv`, `.mypy_cache`, `.gitnexus`, `.pytest_cache` and `.ruff_cache` inside 25 inspected `agent-*` worktrees. All 88 existing generated directories removed; source hashes/status/HEADs, branches, main/active feature and worktree registry verified unchanged. Eight agent worktrees contain uncommitted files; whole worktrees were not removed. Estimated allocated cleanup was 7.41 GiB, but free space did not improve (1.54 GiB before, 1.42 GiB immediately after). APFS sharing/snapshots or concurrent writes are possibilities, not established causes. Active sandbox, input and media/job caches preserved. Evidence: `<sandbox>/evidence/worktree-cleanup-audit-20261006.json`, `worktree-generated-cleanup-summary-20261006.json`, `worktree-generated-cleanup-result-20261006.json`, `worktree-cleanup-storage-recheck-20261006.json`.
- SSD feasibility checked: `/Volumes/ssd` mounted exFAT, about314.51GiB free; existing `/Volumes/ssd/video-editor/{workspace,cache,output}` roots found. **Existing-job relocation is not supported**: recorded destination volume identity and exact manifest/stage paths block config-only cross-volume changes. No files moved, copied or remapped. Supported SSD option is a separate job with fresh generated artifacts and separately approved budget; old caches cannot directly transfer. Preserve current job/hold. Evidence: `<sandbox>/evidence/ssd-storage-preflight-20261006.json`; focused plan `.superpowers/sdd/2026-09-28-phase-2-automatic-highlights/ssd-migration/task_plan.md`.
- Exact-echo candidate-v3 provenance and CLI/SDK retry wiring implemented and verified offline. Scoped gate: **149 passed, 1 live deselected**; full gate **867 passed, 1 live deselected**. Bounded review returned no verified findings. Sandbox config now explicitly sets `max_transient_attempts = 1`; parsed config and budget validated. No paid execution authorized.

## Original plan status

Plan: [Phase 2 implementation plan](superpowers/plans/2026-09-28-phase-2-automatic-highlights.md).

| Tasks | Scope | Status |
|---|---|---|
| 0 | Closure state and execution gates | Complete |
| 1–3 | Config/secret boundary, migrations/budget ledger, edit-plan v2 | Complete |
| 4–7 | Registered proxies, segmentation, Gemini adapter, analysis/coverage gate | Implemented and offline-tested; live integration fixes below remain uncommitted |
| 8–11 | Ranking/dedup/diversity, tracking, long/short planning, rendering | Complete at offline-test level |
| 12 | Nine-stage CLI, resume, reporting | Complete at offline-test level; sandbox acceptance still pending |
| 13 | Offline end-to-end safety and acceptance fixtures | Complete |
| 14 | Evaluation harness, release gate, documentation | **Partial**: exploratory metrics exist; independently verified release evaluation is missing |

Do not convert this task count into a release-completion percentage. Integration and output-quality acceptance are unfinished.

## Repository state and where to work

- Main checkout: `/Users/coffeemug/Programming/automated-video-editing`.
- Active feature worktree: `/Users/coffeemug/Programming/automated-video-editing/.claude/worktrees/feature-phase2-automatic-highlights`.
- Feature branch: `feature/phase2-automatic-highlights`.
- Last verified main and pushed feature head: **`b8743d4`**. Recheck before acting; other sessions can move branches.
- Pricing commit: `849c165`; main integration: `b8743d4`.
- Earlier milestones: schema wire fix `708f1ef`, model switch `f4466b1`, timestamp guidance `61395a5`, duration precision `8242dca`, estimator calibration `a9ad5d8`, reservation ceiling `362eca9`.
- Main does **not** contain the latest uncommitted fixes listed below. Run sandbox tests from the feature worktree, not from main's environment.
- Root `task_plan.md` describes the completed Phase 1 HERO12 benchmark. It is historical, not the active Phase 2 status. Start here and in `HANDOFF.json` instead.

### Uncommitted work to preserve

| File | Pending change |
|---|---|
| `src/video_editor/analysis/gemini.py` | Independent bounded processing wait; returned usage on invalid responses; safe structural diagnostics; exact candidate endpoint/ID validation; exact-echo candidate-v3 prompt; explicit SDK single-attempt policy and environment retry wiring |
| `src/video_editor/cli.py` | Wire existing max_transient_attempts into adapter RetryPolicy |
| `tests/unit/test_bounded_refinement.py`, `tests/unit/test_cli.py` | Prompt provenance, configured attempts, explicit SDK cap and mocked real transport regressions |
| `src/video_editor/analysis/orchestrator.py` | Durable allowlisted failure evidence; usage preservation for interval/identity rejection; actual mismatch field names |
| `src/video_editor/analysis/segmentation.py` | Bounded 50 ms short-audio-tail tolerance; direct exact endpoint mapping |
| `src/video_editor/workflow.py` | Higher-precision chunk-range translation; registered-chunk reuse through full upload-provenance validation; candidate-v3 prompt identity without changing broad identity |
| `tests/unit/test_gemini.py` | Polling, failure privacy/usage, exact candidate contract regressions |
| `tests/unit/test_analysis_orchestrator.py` | Durable diagnostics, settlement, precise interval-rejection regressions |
| `tests/unit/test_segmentation.py` | Tail-gap boundaries/truncation rejection and exact endpoint tests |
| `tests/integration/test_phase2_workflow.py` | Interrupted chunk reuse and missing/tampered/stale chunk rejection |
| `AGENTS.md`, `CLAUDE.md` | Separate instruction/index edits; provenance not confirmed. Leave untouched and do not stage blindly. `CLAUDE.md` also has a worktree-cleanup rule removed in its diff. |

Documentation/handoff files added at this checkpoint are separate from the code fixes. No commit or push is authorized by a request to save context.

## Fresh SSD sandbox preparation

- Large-file root: `/Volumes/ssd/video-editor/phase2-sandbox-20261006/{input,workspace,cache,output}`; mounted exFAT, about313.91GiB free after copies.
- Config: `~/Library/Application Support/video-editor/phase2-ssd-sandbox-20261006/config.toml`.
- State/evidence root: `~/Library/Application Support/video-editor/phase2-ssd-sandbox-20261006/`; state stays internal. Fresh job **b9f89c3b-464e-4da5-a283-c61ea11ca1a8** created by approved broad-only runner. Local resume now completed inspect/proxy/segment directly on exFAT; two registered cloud chunks, no provider requests. Initial mode0600 failure retained as historical evidence.
- Generated 15-minute excerpt copied to SSD input with SHA-256 match; camera originals untouched.
- Policy: gemini-3.8-flash, candidate-v3, one adapter attempt/one SDK transport attempt,8192 output cap,10GiB SSD reserve,USD3/source-hour.
- Fresh job ledger limit will floor to **USD0.750027**. Estimated two broad requests reserve **USD0.135123** total at one attempt. If old four candidate windows recur, combined broad/refinement reservation forecast **USD0.357624**. New broad responses may produce different candidates/count, so this is a forecast, not paid authorization or guaranteed full-run cost.
- Fresh broad scans must run again; old job-owned rows/manifests are not imported. Old USD0.203670 hold remains in old ledger, not transferred/released.
- Evidence: `<fresh-state-root>/evidence/archive-verification.json`, `internal-generated-cleanup.json`, `fresh-ssd-preflight.json`.
- User approved **two fresh broad scans only**, one attempt each, maximum combined reservationUSD0.135123. Initial exFAT attempt failed before provider due0600 assumption; optional APFS image then prepared, but user requires direct exFAT. Filesystem-aware generation now uses O_EXCL reservation at final path on exFAT; POSIX mode/root checks remain on APFS; final evidence bound to validated descriptor and failed exFAT files retained unregistered. Real local resume completed inspect/proxy/segment, registered2 chunks, actual forecastUSD0.087351+0.047772=0.135123; source SHA unchanged. No key read/provider call. Final versioned sampling/provenance full gate running. Evidence: `<fresh-state-root>/evidence/exfat-broad-reservation.json`, `exfat-local-segment.log`; earlier failures preserved in `local-exfat-failure.json`, `broad-only.log`. APFS image/fresh job7541818f remains optional/interrupted, not current config.

## Archived sandbox job

| Item | Verified value |
|---|---|
| Job ID | `8acede66-a717-4d53-b0e8-9686cb0fa167` |
| Root | `~/Library/Application Support/video-editor/phase2-e2e-20261005/` |
| Config | `<root>/config.toml` |
| Database | `<root>/state/jobs.sqlite3` |
| Input | Existing generated 900-second excerpt; not the full trip |
| Input file | `~/Library/Application Support/video-editor/gemini-fifteen-minute-preflight-20261002/input/fifteen-minute-excerpt.mp4` |
| Original excerpt location | `GX011485.MP4`, beginning at source second 600 |
| Job status | `failed` in preserved DB; archived, not locally resumable |
| Generated-file archive | `/Volumes/ssd/video-editor/archive-phase2-e2e-20261005/` |
| Cleanup | Old internal cache/workspace removed only after SHA-256 verification; state/config/evidence retained |
| Completed stages | `inspect`, `proxy`, `segment` |
| Analysis | Two validated broad scans cached; full broad coverage; no validated candidate cache rows |
| Not reached | `rank`, `plan`, `render`, `validate`, `report` |
| Output videos | None |

The entire trip has 18 source files, about 11.56 hours and 142.17 GiB. All belong to evaluation batch `trip-1`. First three files were selected only as technical subsets: `GX011485.MP4`, `GX011486.MP4`, `GX011487.MP4`. They are not independent train/calibration/holdout batches.

### Current job ledger

These are **local ledger amounts**, not independently verified invoices. Snapshot read from SQLite on 2026-10-06:

| Amount | USD |
|---|---:|
| Limit | 0.750027 |
| Recorded spend | 0.106893 |
| Reserved, old unknown billing | 0.203670 |
| Available | 0.439464 |

- Broad calls: two completed, total recorded cost **0.053460**.
- Original failed candidate request: `c1fc54b2-f1c9-451e-ad1f-681c33a8f82d`, `billing_unknown`, reservation **0.203670**. Its original response/error details were not saved. Do not infer zero cost or release the hold.
- Diagnostic retry `candidate-diagnostic-492-575-20261006`: schema valid but exact identity/interval rejected; complete usage preserved, cost **0.028263** settled. Saved diagnostic names all four fields and cannot identify the actual mismatch retrospectively.
- Corrected-prompt retry `candidate-exact-echo-492-575-20261006`: exact IDs and 492–575-second interval validated; cost **0.025170** settled; four subject boxes. Exactly one generation call; remote upload deletion confirmed. Full response not cached; do not manufacture a candidate result from scores alone.

### Cached broad candidate windows

- First chunk (source/proxy chunk origin 0): `candidate_1` at **492–575 s**, `candidate_2` at **233–330 s**.
- Tail chunk (source origin 720): `cand_1` at **50–65 s**, `cand_2` at **98.5–116 s**, relative to that chunk's proxy.
- IDs are scoped by their actual chunk and cache identity. Do not infer equivalence from a short ID string alone.

## Remaining-candidate preflight (2026-10-06)

Read-only SQLite and offline estimation verified all four remaining windows. Existing broad cache keys match current broad identities; no broad rescan is needed. Exact-echo prompt now has distinct `candidate-v3` identity in workflow and adapter; response schema remains `candidate-v2`. All four new prompt cache keys are unused. Old `candidate-v2` unknown request remains held and unchanged.

| Chunk | Candidate | Proxy window (s) | Sandbox-source window (s) | Selected FPS | One-attempt reservation USD | Default three-attempt reservation USD |
|---|---|---|---|---:|---:|---:|
| First | `candidate_1` | 492–575 | 492–575 | 5 | 0.067890 | 0.203670 |
| First | `candidate_2` | 233–330 | 233–330 | 5 | 0.073644 | 0.220932 |
| Tail | `cand_1` | 50–65 | 770–785 | 5 | 0.039942 | 0.119826 |
| Tail | `cand_2` | 98.5–116 | 818.5–836 | 5 | 0.041025 | 0.123075 |
| **Total** | | | | | **0.222501** | **0.667503** |

Estimates use the current 8192 output-token cap, pinned pricing, schema/text/media allowances and worst-case 5 FPS reservation. The actual local selector also chooses 5 FPS for every window, using motion over each cache-identity **chunk**, not each individual candidate interval. One-attempt total fits USD 0.439464 available while keeping the old hold; default three-attempt total does not fit without intervening settlements. These are local reservation estimates, not guaranteed invoice bounds. A supported prompt-version change must be re-estimated because it can change schema/text policy.

### Why blind resume is blocked

- `JobStore._reserve_request` checks request ID **or same-job cache key** and rejects any existing non-reserved request. A fresh request ID does not make the old candidate key dispatchable. Reconciliation resolves billing only; it does not supply missing semantics or create a retry lifecycle.
- There is no supported same-key replacement-attempt path in the current code. Do not reset, delete, rename or release the old request to force another dispatch.
- Exact-echo prompt is now honestly versioned `candidate-v3`; old same-key guard remains unchanged. This is cache provenance for genuinely changed prompt semantics, not an arbitrary retry nonce. Fake-provider integration verifies broad results and original unknown request/hold remain byte-for-byte unchanged while new prompt results persist.
- CLI now passes existing `[gemini].max_transient_attempts` through `RetryPolicy` to the adapter; adapter-owned SDK clients explicitly set `HttpRetryOptions(attempts=1)`. Mocked SDK transport verifies no multiplication of adapter attempts. **Sandbox config now explicitly sets `max_transient_attempts = 1`**, validated through `resolve_config` and `RetryPolicy`. Default three-attempt estimates remain useful comparison only. Recheck cost/storage and obtain exact paid approval before dispatch.
- Storage remains independently blocked. Latest snapshot: 7,911,260,160 free bytes (7.37 GiB) versus 10,737,418,240 reserve, shortfall **2,826,158,080 bytes (2.63 GiB)** merely to reach reserve, plus working/output space still required. Free unrelated storage yourself or authorize a specific safe cleanup; no cache deletion, volume migration or reserve reduction was performed.

No key read, provider client creation, provider call, ledger mutation, new job, source edit, commit or push occurred during that preflight. A delegated read-only analysis failed with an API/watchdog error; controller completed the bounded preflight inline instead.

**Historical remaining-work section:** preceding same-job estimates no longer describe an executable resume after SSD fresh-start/cleanup. See fresh SSD preparation above; old ledger/hold still preserved.

## Verified live measurements

Prices used: Gemini Developer API Standard input USD 0.75 per million tokens; output including thinking USD 3.75 per million tokens. Costs below are calculated from returned usage and rounded to microUSD.

| Test | Scope | Usage-derived USD | Outcome |
|---|---|---:|---|
| Broad | 1 s, 0.5 FPS | 0.002486 | Strict response valid |
| Broad | 60 s, 0.5 FPS | 0.005816 | Strict response valid |
| Broad | 900 s, 0.5 FPS | 0.046273 | 5 scenes, 2 candidates; strict response valid |
| Standalone refinement | 495–575 s, 5 FPS | 0.024630 | Strict response valid, 5 subject boxes |
| Sandbox exact-echo retry | 492–575 s, 5 FPS | 0.025170 | Exact contract valid, 4 subject boxes |

Broad VIDEO token counts were about 58 tokens/s on the 60- and 900-second samples. AUDIO was not listed separately. This does not prove that audio is free or always folded into VIDEO. A few measurements do not establish a guaranteed upper bound, deterministic tokenization for all footage, story quality, or full-trip cost.

## Failures already understood—do not rediscover

| Failure | Evidence and resolution |
|---|---|
| Upload HTTP 402 | Prepay setup/top-up resolved subsequent uploads. Listing alone did not prove upload billing was usable. |
| Wire schema HTTP 400 | SDK 1.75.0 sent unsupported `additional_properties`; cleaned SDK schema while keeping strict local validation. |
| Model HTTP 404 | Provider refused `gemini-2.5-flash` for this account; user chose exact model `gemini-3.8-flash`. Model list alone was not proof of access. |
| Timestamp response validation | Added plain-decimal-seconds guidance to schema. Exact old rejected values were not retained. |
| Excess duration precision HTTP 400 | Protobuf Duration rejects more than nine fractional digits; offsets rounded to nanoseconds. |
| Processing timeout | Original status polling shared generation retry count. Pending fix uses 5-second polling and a 300-second monotonic polling deadline. It does not cap an already-running network request. |
| Audio segmentation failure | WAV 900.000000 s vs mapping 900.032467 s. Pending fix permits only <=50 ms shortfall, preserves overlong/truncated-payload rejection, and does not extend evidence through missing audio. |
| Endpoint provenance rejection | Default Decimal precision reconstructed final endpoint 1.38e-26 s beyond persisted endpoint. Pending exact-range fix does not relax upload comparisons. |
| Resume tried recreating registered chunk | Pending recovery reuses existing chunk only after `validate_upload_candidate`; missing/tampered/stale mapping fails closed. |
| Candidate refinement rejected despite valid schema | Adapter accepted subintervals while orchestrator required exact endpoints/IDs. Pending fix aligns adapter with strict rule and adds exact-echo prompt. Latest diagnostic call succeeds. |
| Failure obscured and usage lost | Pending fix preserves usage and durable structural error evidence, without response text/values. Cannot recover evidence absent from old run. |
| Full suite intermittent failure | `tests/unit/test_runner.py::test_runner_rejects_second_process_for_same_output` uses timing-sensitive processes. Isolated reruns pass; latest full gate passed. Do not claim a failed full run passed. |

## Standing decisions and safety limits

- Originals immutable. Never upload source originals; use generated, registered proxies with full provenance checks.
- Model is `gemini-3.8-flash`. Standard price pin is valid through **2026-12-31**, expires **2027-01-01 UTC**. Re-verify prices before changing that pin.
- Runtime reservation ceiling: **USD 3.00/source-hour**, explicitly chosen by user. Release evaluator still checks actual spend **USD 1.00/source-hour** and otherwise blocks release.
- Reserve before dispatch. Returned complete usage can settle through existing ownership/cost checks; unknown billing remains held. No speculative release or unapproved limit increase.
- Keep **10 GiB storage reserve**. Recheck available space before renders/resumes. Never delete caches or reduce reserve without approval.
- Key lives in macOS Keychain under service `automated-video-editing-gemini`, account = login user. Runtime use only; never put value in chat, files, logs, reports, config or git. Keychain entry is not automatically exported to the CLI. User intends rotation when testing ends.
- Active paid scope on2026-10-07: **four exact refinements** for job `ebc41996-05a2-476c-acc4-14fb7ebcd22c`,5FPS,8192 output cap,one attempt each,combined reservation<=USD0.216075; user explicitly approved after fresh broad success. Source windows245–320,490–585,770–782,858–873s. Refinement batch stopped after first paid call: subject_boxes schema rejection,USD0.006257 settled,zero validated candidate caches. Remaining three were not dispatched. Exclusive batch marker retained; no automatic restart or retry. Two broad approvals consumed withUSD0.049672 settlement; never rescan automatically. No rank/plan/render or paid retry approved. Next diagnose offline before requesting any further paid scope.
- Do not create a new job claiming it can directly reuse job-owned manifests/cache rows from the old job. Ownership checks prohibit that. Do not bypass them or reset the original request's state to enable retry.
- No automatic full-trip run. No automatic commit, push or merge of pending fixes.

## Next actions, in order

1. **Use fresh SSD config; do not resume archived job.** Verify SSD mount/identity, source SHA-256 and free space before execution. Old ledger/evidence retained; old cache/workspace now exist only as SSD archives, not valid imported job artifacts.
2. **Execute approved four-refinement scope after exact local preflight.** Latest full875tests passed; new SSD job `ebc41996-05a2-476c-acc4-14fb7ebcd22c` has two validated broad caches and complete coverage. Revalidate source/volume/storage, exact chunk-scoped candidate windows,5FPS and USD0.216075 aggregate cap before runtime key. One-shot private runner `refine-only-20261007.py` stops before rank/plan/render and prohibits repeated dispatch. OldUSD0.222501 estimate remains historical.
3. **Review/save pending fixes separately.** Run impact/detect-changes and scope tests before any commit. Preserve unrelated instruction-file edits. User approval is required for commit/push/merge.
4. **Ask for exact paid-batch approval**, then execute only approved remaining work. Keep within job budget and storage; fail closed if reservations cannot fit. Do not automatically increase cap or discard old hold.
5. **Reach first complete sandbox edit:** ranking, planning, rendering, validation and reports. Verify source SHA-256, request ledger, remote cleanup, dimensions/durations/plan provenance and output readability. Manual edit-quality review still required. The sandbox source is already downscaled, so this does not prove original-footage quality.
6. **Only then consider first-three-file acceptance**, with its own budget/storage approval. Full-trip execution remains a later decision.
7. **Finish Task 14 release evaluation:** independently linked output/source/upload/billing evidence, committed calibration/threshold provenance, holdout-only scoring, human labels across >=3 independent batches and >=100 candidate intervals. Current trip alone cannot satisfy this. Release check deliberately remains blocked.

## Evidence and navigation

- Human handoff: [Continue here](../.continue-here.md).
- Machine handoff: [HANDOFF.json](../HANDOFF.json).
- Architecture: [architecture.md](architecture.md).
- Historical SDD ledger: [progress.md](../.superpowers/sdd/2026-09-28-phase-2-automatic-highlights/progress.md). It contains point-in-time claims; use this document for current state.
- Focused task plans: `processing-wait-task_plan.md`, `audio-tail-task_plan.md`, `endpoint-recovery-task_plan.md`, `refinement-diagnostics-task_plan.md`, `refinement-contract-task_plan.md` beside the SDD ledger.
- Private sandbox evidence: `<sandbox>/evidence/cli.stdout`, `cli.stderr`, `resume-01.*`, `resume-02.*`, `resume-03.*`, and `candidate-*-20261006.json`.
- Read-only preflight evidence: `<sandbox>/evidence/sandbox-preflight-20261006.json`, `sandbox-ledger-preflight-20261006.json`, `sandbox-candidate-inputs-20261006.json`, `sandbox-candidate-preflight-20261006.json` and `sandbox-preflight-offline-tests-20261006.log`. Focused plan: `.superpowers/sdd/2026-09-28-phase-2-automatic-highlights/sandbox-preflight/task_plan.md`.
- Versioned prompt preflight: `<sandbox>/evidence/sandbox-candidate-v3-preflight-20261006.json`; current totals and new/old key comparison, no dispatch. Code task plan: `.superpowers/sdd/2026-09-28-phase-2-automatic-highlights/bounded-refinement/task_plan.md`.
- One-attempt readiness evidence: `<sandbox>/evidence/sandbox-one-attempt-ready-20261006.json`; parsed config, unchanged ledger/cache, no active process and current storage failure. No paid approval or dispatch.
- Current gate logs: `<sandbox>/evidence/bounded-refinement-full-20261006.log`, `bounded-refinement-quality-20261006.log`, `bounded-refinement-green-20261006.log` and `bounded-refinement-format-failure-20261006.log`.
- Durable sanitized failure files: `<sandbox>/state/analysis-failures/<job_id>/analysis-failure-<request_id>.json`.
- Full offline gate log: `/private/tmp/claude-501/-Users-coffeemug-Programming-automated-video-editing/b6ecea29-3978-433a-bc97-073d1566314c/tasks/bn0l5n21h.output`. Terminal task logs are temporary; durable job evidence and this status record must remain sufficient if they disappear.
- Private evidence may contain scene/provider text in raw CLI outputs; do not publish or commit raw sandbox logs.

## Updating this record

After a meaningful change, replace the current checkpoint, repository state, stage table, budget table, evidence references and next action together. Append a dated entry below and in the historical SDD ledger. Never mark completion solely from a shell exit code: verify the relevant DB row, file, returned usage or rendered output. Record failed gates and any skipped validation. Leave unresolved billing explicit.

### 2026-10-06 checkpoint

Latest exact-echo diagnostic succeeded; all code changes through that prompt were verified by 858 offline tests. Sandbox remains failed with cached broad scans and an unresolved old candidate hold. This record and root handoffs were created for later resumption. No live call, source modification, commit or push occurred while saving context.

### 2026-10-06 remaining-work preflight

Read-only job/cache/ledger verified unchanged; no active pipeline process. Free space 8.50 GiB fails 10 GiB reserve. All four candidates select 5 FPS; one-attempt total reservation USD 0.222501 fits available USD 0.439464, default three-attempt total USD 0.667503 does not fit without settlements. First candidate key matches old unknown request; normal resume cannot replace it. Recommended offline next change is honest versioning of the changed exact-echo prompt plus verified one-attempt execution, preserving cached broad results and old hold. At that preflight checkpoint this remained proposed, not implemented; the following entry records its implementation. Focused reservation/orchestrator/adapter regressions: **136 passed, 1 live deselected in 0.80s**. Full suite was not rerun because no source/test code changed; previous 858-test gate remains historical. Preflight evidence and handoffs updated; no paid call, key read, ledger mutation, source edit, commit or push.

### 2026-10-06 versioned prompt and bounded retry gate

Exact-echo prompt is now candidate-v3 in workflow/adapter; response schema remains candidate-v2 and broad-v1 implementation/cache identities are unchanged. Existing max_transient_attempts is wired from CLI through environment factory into adapter; owned SDK clients explicitly use HttpRetryOptions(attempts=1). RED tests verified missing wiring/version/cap before implementation. Scoped149 passed/1 live deselected; full867 passed/1 live deselected/1 fork warning in528.47s. Lint, source/tests format, mypy35 files, wheel/sdist build, CLI help and diff check passed. Whole-repo format check failed unchanged historical-plan Markdown examples; left untouched, not claimed green. Bounded API-contract review found no verified findings.

Fake-provider integration proves old unknown request and hold remain unchanged while broad results are reused and candidate-v3 results persist. Read-only real-job preflight confirms all four new keys unused, no candidate cache inserted, old hold USD0.203670 intact, available USD0.439464. One-attempt reservation total remains USD0.222501 at8192 output tokens, default3 total0.667503. Latest storage snapshot8.75GiB remains below10GiB reserve; sandbox config still defaults3 attempts. No key read, paid call, sandbox config/ledger/source-media edit, commit or push. Next at that checkpoint: storage headroom, exact one-attempt scope preparation and fresh paid approval.

### 2026-10-06 one-attempt sandbox configuration

User requested proceeding with preparation. Sandbox config now explicitly sets max_transient_attempts=1; resolve_config/RetryPolicy validation passed, model/cap/storage reserve unchanged. SQLite reopened read-only: same five requests, two broad caches, no candidate caches, unchanged limit/spend/old hold/available. No active pipeline process. Latest free space7.37GiB is below10GiB reserve by2.63GiB before working/output allowance. No cleanup, key read, provider call, ledger/source-media edit, commit or push. Next action at that checkpoint belonged to user: free unrelated storage or authorize specific safe cleanup; then recheck headroom and obtain exact paid approval.

### 2026-10-06 fresh SSD sandbox and archived-media cleanup

User authorized fresh start if same-job copying cannot resume, and redundant workspace cleanup. New SSD roots and config prepared, generated input SHA-256 verified; old sandbox and fifteen-minute preflight copied to distinct SSD archives with every file hash verified and SQLite snapshots checked by schema/rows/integrity. Only four old internal cache/workspace roots removed after verification,379.95MiB logical bytes; old SQLite/config/evidence and generated input retained. Old job now archived and not locally resumable; oldUSD0.203670 hold unchanged. Source camera footage and code worktrees untouched.

Fresh config validates one-attempt policy,10GiB reserve and exFATSSD with313.91GiB free. APFS sparsebundle later added after user approval. Two exFAT-run attempts stopped before provider: mode0600 failed; then APFS root mismatch stopped old job. Separate APFS fresh job local proxy completed, but existing random-seek segmentation ran over20min CPU and was interrupted with zero requests/budget/manifests. Input SHA remains unchanged. User requires exFAT direct for SSD storage; APFS used only for private cache/workspace/outputs, no reformat. Broad approvalUSD0.135123 remains unused.

Sequential `_read_video` fix replaces per-sample seeks with one forward decode, keeping scheduled timestamps. GitNexus impact CRITICAL, warned. RED verified 40 correct 0.1s samples and zero seeks; sequential frame equivalence regression added. Focused tests76 passed/3.06s, Ruff source/test format and lint plus mypy src passed. Full offline suite and local APFS segmentation rerun still required; no key/provider call/budget reservation/commit/push. `mypy src tests/unit/test_segmentation.py` reports11 test-file typing errors (including existing dynamic kwargs and return annotation); source-only mypy passes.
