# HERO12 Benchmark Record — 2026-09-28

## Outcome

**Real-camera workflow passed for this authorized 18-file batch, with documented benchmark limitations.** Existing completed job `d0b50171-a0e0-41dd-a721-08daddbdfac7` contains all six completed stages, matching source identity, valid plans, readable horizontal and vertical renders, reports, and reusable proxy/audio artifacts.

A completed-job resume returned in 3 seconds and changed no substantive artifacts. This proves same-job reuse without requiring duplicate proxy generation or extra storage.

This result applies only to the tested files and recorded environment. It does not establish compatibility with every HERO12 setting. Camera firmware/settings, independent peak-disk sampling, known shooting-order confirmation, and disconnected-volume recovery were not verified.

## Authorization and Scope

- Authorized input: `/Volumes/microSD/DCIM/100GOPRO`
- Input files: 18 MP4 files
- Input bytes: 152,659,041,281 bytes (142.175 GiB)
- Generated roots: `/Volumes/microSD/video-editor/{workspace,cache,output}`
- State root: `~/Library/Application Support/video-editor`
- Originals treated as immutable
- No cloud or network service used
- No SSD disconnect/unmount test performed

## Environment

- macOS 27.0, build 26A428
- FFmpeg 9.0.2
- FFprobe 9.0.2
- Application 0.1.0
- Destination filesystem: exFAT at `/Volumes/microSD`
- Initial reported capacity: 238 GiB total, 162 GiB used, 76 GiB available
- `h264_videotoolbox` and `libx264` available

Raw environment evidence: `environment.txt`.

## Input Inspection

- 18 sources usable
- 0 skipped inputs
- 10 chronology groups
- 0 chronology warnings
- Total probed duration: 41,600.725834 seconds (11h 33m 20.726s)
- Video: HEVC Main, 1920×1080, 29.97003 fps, BT.709 metadata, full-range `pc`
- Audio: AAC LC, stereo, 48 kHz
- Repeated warnings per source: HEVC source and unrecognized `pc` color-range classification

Automatic GoPro grouping produced:

- Single-file groups: 1485, 1486, 1487, 1488, 1490
- Two-file groups: 1489, 1491
- Three-file groups: 1492, 1493, 1494

Chapter order in both plans is deterministic: `GX01`, then `GX02`, then `GX03` for grouped file numbers. Agreement with known shooting intent remains unconfirmed.

## Completed Workflow

Job: `d0b50171-a0e0-41dd-a721-08daddbdfac7`

- Job status: completed
- Inspect: completed
- Proxy: completed
- Plan: completed
- Render: completed
- Validate: completed
- Report: completed
- Sources: 18
- Chronology groups: 10
- Persisted artifact rows: 43

Recorded stage durations:

| Stage | Duration |
|---|---:|
| Inspect | 1.455 s |
| Proxy | 17,738.576 s (4h 55m 38.576s) |
| Plan | 0.123 s |
| Render | 580.290 s (9m 40.290s) |
| Validate | 0.082 s |
| Report | 0.197 s |

The job spans resumed execution periods, so stage durations are reported individually. A trustworthy continuous start-to-finish wall time was not captured.

## Plans

Both plans contain 18 clips, each selecting the first 8 seconds of one source, for a 144-second timeline.

- Planner provenance: `phase1-sample-v2`
- Planner version: `phase1-sample-v2`
- Horizontal: 1920×1080, `fit_background`
- Vertical: 1080×1920, `center_crop`
- Horizontal timeline: 144 seconds, strictly below 3600
- Vertical timeline: 144 seconds

Exact clip sequence appears in `completed-job-clip-order.txt`.

## Render Validation

### Horizontal

- File: `long.mp4`
- Container: MP4-compatible `mov,mp4,m4a,3gp,3g2,mj2`
- Video: H.264, 1920×1080, 30 fps
- Audio: AAC, stereo, 48 kHz
- Duration: 144.000 seconds
- Size: 114,696,390 bytes

### Vertical

- File: `short-01.mp4`
- Container: MP4-compatible `mov,mp4,m4a,3gp,3g2,mj2`
- Video: H.264, 1080×1920, 30 fps
- Audio: AAC, stereo, 48 kHz
- Duration: 144.000 seconds
- Size: 77,892,613 bytes

Representative frames at 72 seconds confirm:

- Both frames fill their target dimensions.
- Vertical output has no black bars.
- Vertical center crop retains the central motorcycle cockpit and intentionally removes side content visible in the horizontal frame.

Evidence: `completed-job-long.ffprobe.json`, `completed-job-short.ffprobe.json`, `completed-job-long-frame.jpg`, and `completed-job-short-frame.jpg`.

## Report Validation

- Cloud usage: `0`
- Skipped inputs: `0`
- Chronology warnings: `0`
- Fallbacks: none
- Estimated peak render-output growth: 37,324,800 bytes
- Estimate scope: render-output byte growth only; workspace and cache excluded

The report embeds job status `running` because report state is assembled before final `complete_job`. Authoritative post-run `status` is `completed`. This is expected from current workflow sequencing but can confuse report readers.

Independent peak-disk samples were not captured during the original render, so the estimate cannot be compared with observed peak growth for this batch.

## Resume and Artifact Reuse

After restoring `GX011485.MP4`, its SHA-256 matched the original manifest exactly.

Completed-job resume:

- Exit code: 0
- Wall time: 3 seconds
- Returned both output records and both report paths
- Substantive files before resume: 43
- Substantive files after resume: 43
- Added: 0
- Removed: 0
- Size changed: 0
- Modification time changed: 0
- Hash changed among 27 directly hash-compared artifacts: 0

Large artifacts were checked by unchanged path, size, and nanosecond modification time rather than rehashing during the reuse comparison.

## Source Integrity

- Pre-run manifest: 18 files
- Post-run manifest: 18 files
- Matching paths: 18/18
- Matching SHA-256 values: 18/18
- Changed files: none
- Restored `GX011485.MP4`: independently matched original SHA-256

Evidence: `source-hashes-before.txt`, `source-hashes-after.txt`, and `source-integrity-summary.json`.

## Storage Boundary Evidence

A separate new job, `a7f07689-c781-46bb-a5c9-0fd14f53a527`, correctly stopped before duplicate proxy generation:

- Exit code: 11 (`storage`)
- Inspect stage: completed
- Proxy stage: failed during free-space validation
- New job cache directory: absent
- Proxy/audio artifacts created: none

Exact rejection:

```text
storage: insufficient free space on /Volumes/microSD: 82045042688 free, 10737418240 reserved, 76329520640 required
```

Deleting the completed job's 9.8 GiB cache would not create enough capacity for a duplicate full-batch run and would destroy reusable analysis artifacts. Existing same-job reuse is the safe path.

## Validation Matrix

| Gate | Result |
|---|---|
| Authorized real files discovered | Pass |
| All 18 files probe successfully | Pass |
| No inputs skipped | Pass |
| GoPro chapters grouped deterministically | Pass |
| Proxy/audio generation | Pass for completed job |
| Plan provenance `phase1-sample-v2` | Pass |
| Horizontal 1920×1080 render | Pass |
| Vertical 1080×1920 center-crop render | Pass |
| Vertical fills frame without black bars | Pass at representative frame |
| Long timeline strictly below 3600 seconds | Pass — 144 seconds |
| Output readability, dimensions, codecs, audio, duration | Pass |
| Report cloud usage `0` | Pass |
| Same-job resume artifact reuse | Pass |
| Source hashes unchanged | Pass — 18/18 |
| Storage reserve enforced before duplicate proxies | Pass |
| Camera firmware/settings recorded | Not verified |
| Chronology agrees with known shooting order | Not verified manually |
| Independent render peak measurement | Not captured |
| Disconnected-volume recovery | Not run by scope decision |

## Verdict

**Scoped pass with four limitations.** Phase 1 successfully processed and rendered this authorized 18-file real-camera batch, preserved sources, stayed local, enforced storage safety, and reused completed artifacts. Do not generalize beyond these files/settings until camera metadata and more representative batches are tested.

Remaining benchmark work:

1. Record HERO12 firmware and capture settings.
2. Confirm chronology against known shooting order.
3. Capture independent peak-disk growth during a future render.
4. Run disconnected-volume recovery only with separate approval.

## Evidence Files

- `environment.txt`
- `source-hashes-before.txt`
- `source-hashes-after.txt`
- `source-integrity-summary.json`
- `inspect.stdout`
- `inspect.stderr`
- `inspect-repro.stdout`
- `inspect-repro.stderr`
- `inspect-repro.result`
- `status.stdout`
- `run.started`
- `run.completed`
- `run.result`
- `run.stdout`
- `run.stderr`
- `run-status.stdout`
- `run-status.stderr`
- `run-summary.json`
- `completed-job-summary.json`
- `completed-job-final-status.stdout`
- `completed-job-final-status.stderr`
- `completed-job-resume.started`
- `completed-job-resume.completed`
- `completed-job-resume.result`
- `completed-job-resume.stdout`
- `completed-job-resume.stderr`
- `completed-job-artifacts-before-resume.json`
- `completed-job-artifacts-after-resume.json`
- `completed-job-reuse-summary.json`
- `completed-job-long.ffprobe.json`
- `completed-job-short.ffprobe.json`
- `completed-job-long-frame.jpg`
- `completed-job-short-frame.jpg`
- `completed-job-clip-order.txt`
