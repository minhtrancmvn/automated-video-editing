# Task Plan: Real HERO12 Benchmark Gate

## Goal
Run the documented Phase 1 benchmark against authorized GoPro footage, preserve verifiable evidence, and report compatibility limits and discrepancies without modifying source media.

## Phases
- [x] Phase 1: Confirm authorization, input batch, storage roots, and execution boundary
- [x] Phase 2: Capture source hashes and camera/system/storage metadata
- [x] Phase 3: Run inspection and full workflow benchmark with timing evidence
- [x] Phase 4: Validate outputs, reports, chronology, source integrity, local-only behavior, and same-job artifact reuse
- [x] Phase 5: Write benchmark record and identify follow-up work
- [x] Phase 6: Run final scoped verification and deliver results

## Key Questions
1. Can all 18 authorized GoPro MP4 files be inspected and processed successfully?
2. Do generated horizontal and vertical outputs satisfy dimensions, duration, readability, plan provenance, and crop requirements?
3. Does the run preserve original source hashes and report zero cloud usage?
4. Do filename chronology and chapter/session ordering agree with available source evidence?
5. How do measured wall time and disk growth compare with reported estimates?
6. Which discrepancies block a scoped HERO12 compatibility claim?

## Decisions Made
- Use branch `test/hero12-benchmark` because benchmark records change repository artifacts.
- Treat `/Volumes/microSD/DCIM/100GOPRO` as the explicitly authorized 18-file input batch.
- Restrict writes to configured `/Volumes/microSD/video-editor/` roots and local benchmark evidence files.
- Keep source MP4 files immutable and compare hashes before and after processing.
- Exclude SSD disconnect/unmount testing from this run because it can disrupt active work and needs separate approval.
- Do not claim general HERO12 compatibility; any pass applies only to the tested batch and recorded conditions.
- Keep the configured 10 GiB storage reserve intact; do not force duplicate proxy generation when capacity is insufficient.
- Reuse and validate completed same-batch job `d0b50171-a0e0-41dd-a721-08daddbdfac7`: all six stages are complete, its inspect fingerprint matches the benchmark jobs, and its cache/plans/renders/reports exist. This is safer than deleting its 9.8 GiB cache, which would still leave insufficient room for a new run.

## Errors Encountered
- Initial relative `microSD` directory probe failed because the configured media is mounted at `/Volumes/microSD`; corrected subsequent probes to the absolute configured volume path.
- Full-run storage calculation found a 4.674 GiB shortfall after the required 10 GiB reserve; preserve the reserve and pause full rendering rather than forcing execution.
- Hash-completion waiters matched their own `pgrep -f` command text and waited indefinitely after hashing completed; stopped those waiters and switched to direct artifact/process verification before starting inspection.
- First inspection wrapper exited before writing its completion bookkeeping despite a completed inspection stage and valid 18-source stdout payload; identical direct reproduction exited 0, so record this as a non-reproducible wrapper anomaly rather than an application inspection failure.
- Retry check found `GX011485.MP4` temporarily missing from the authorized input directory, reducing the batch from 18 files to 17; it was restored and its SHA-256 matched the original manifest exactly.
- Clip-order evidence script initially used `source_id` for plan source records, but persisted plans use `id`; corrected the evidence-only script and generated the expected 18-clip sequence.

## Status
**Complete for scoped batch** - Existing completed same-batch job passed inspection, proxy, planning, render, validation, reporting, source-integrity, visual frame, and same-job reuse checks. Four benchmark limitations remain documented: firmware/settings, manual chronology confirmation, independent peak-disk sampling, and disconnected-volume recovery.
