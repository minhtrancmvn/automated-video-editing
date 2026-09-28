# Notes: Real HERO12 Benchmark Gate

## Final Summary
- Authorized batch: 18 GoPro MP4 files under `/Volumes/microSD/DCIM/100GOPRO`.
- Completed same-batch job: `d0b50171-a0e0-41dd-a721-08daddbdfac7`.
- All six stages completed: inspect, proxy, plan, render, validate, report.
- 18 sources, 10 chronology groups, zero skipped inputs.
- Horizontal output: H.264/AAC, 1920×1080, 144 seconds.
- Vertical output: H.264/AAC, 1080×1920, 144 seconds, all clips `center_crop`; representative frame has no black bars.
- Report cloud usage: `0`; no fallback encoders.
- Source SHA-256 values unchanged for all 18 files, including independently restored `GX011485.MP4`.
- Same-job resume returned in 3 seconds and changed zero substantive artifacts.

## Authorization and Safety Boundary
- User authorized benchmark use of all 18 MP4 files and writes under `/Volumes/microSD/video-editor/`.
- Source MP4 files remained immutable.
- No cloud calls or external generation services used.
- SSD disconnect/unmount behavior excluded; needs separate approval.
- Compatibility statement applies only to this batch and recorded environment.

## Environment
- macOS 27.0, build 26A428.
- FFmpeg/FFprobe 9.0.2.
- Application 0.1.0.
- Destination filesystem exFAT at `/Volumes/microSD`.
- `h264_videotoolbox` and `libx264` available.

## Stage Timing
- Inspect: 1.455 s.
- Proxy: 17,738.576 s (4h 55m 38.576s).
- Plan: 0.123 s.
- Render: 580.290 s (9m 40.290s).
- Validate: 0.082 s.
- Report: 0.197 s.
- No trustworthy continuous wall time; job used resumed execution periods.

## Storage Boundary
- A duplicate new job `a7f07689-c781-46bb-a5c9-0fd14f53a527` exited 11 at proxy preflight, before cache creation, preserving reserve.
- Deleting existing completed 9.8 GiB cache would not be enough for duplicate generation and would destroy reusable artifacts.
- Existing same-job reuse was safe path.

## Remaining Limitations
1. HERO12 firmware/capture settings not recorded.
2. Automated chronology still needs manual comparison with known shooting order.
3. Independent render peak-disk sampling not captured.
4. Disconnected-volume recovery not run.
5. Report-internal status is `running` because report is assembled before final `complete_job`; authoritative status is `completed`.

## Verification
- Ruff, format check, mypy, 216 tests, build, and CLI help passed.
- Evidence record: `docs/benchmark-records/2026-09-28/benchmark-record.md`.
