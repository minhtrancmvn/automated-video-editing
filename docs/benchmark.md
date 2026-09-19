# HERO12 Benchmark Gate

## Status

Synthetic fixtures exercise generated tone video, silent video, GoPro-style chapter names, multiple sessions, unreadable candidates, output probes, and resume behavior. They do not validate real HERO12 media. Real HERO12 support is pending until this procedure completes with user-authorized files.

Do not claim HERO12 compatibility, performance, or chronology correctness from synthetic test results.

## Prerequisites

1. Obtain explicit authorization for representative HERO12 footage.
2. Copy files to authorized external input folder. Keep originals immutable and retain source hashes.
3. Record camera settings before processing: HERO12 firmware, resolution, frame rate, codec, bit depth, color profile, stabilization, HDR/HLG, aspect ratio, chapters, and any Quik/export processing.
4. Mount external SSD at exact configured path. Record filesystem type, free space, Mac model, macOS, FFmpeg version, and `video-editor` version.
5. Configure isolated workspace, cache, and output paths. Never point generated roots into source folder.

## Procedure

```bash
video-editor inspect /Volumes/TravelSSD/authorized-hero12 --config config.toml
video-editor run /Volumes/TravelSSD/authorized-hero12 --config config.toml
video-editor status JOB_ID --config config.toml
```

Record wall time from start through report completion. Record peak disk use across workspace, cache, and output. Save `status` output and job report. Probe both final outputs independently:

```bash
ffprobe -v error -show_format -show_streams \
  /Volumes/TravelSSD/video-editor/output/JOB_ID/long.mp4
ffprobe -v error -show_format -show_streams \
  /Volumes/TravelSSD/video-editor/output/JOB_ID/short-01.mp4
```

Confirm horizontal dimensions are 1920×1080, vertical dimensions are 1080×1920, both MP4 files are readable, and both plans/reports exist. Confirm plan provenance is `phase1-sample-v1`. Verify long timeline is strictly below 3600 seconds.

Manually inspect filenames and chronology. Check GoPro chapter ordering, session transitions, repeated/reset numbers, and agreement with recorded shooting order. Compare original hashes before and after run. Confirm no network dependency by running only local commands and reviewing report cloud usage `0`.

Test storage boundary separately: disconnect or unmount configured SSD before a write stage. Command must fail without creating an internal fallback directory. Reconnect mount, inspect `status`, and use `resume`; record whether valid previous stages remain reused.

## Benchmark record

Record these fields for each batch:

| Field | Value |
|---|---|
| Authorization and batch identifier | |
| Camera/firmware/settings | |
| Mac, macOS, FFmpeg, application version | |
| SSD model, filesystem, free space | |
| Input file count and bytes | |
| Wall time by stage and total | |
| Peak workspace/cache/output bytes | |
| Horizontal and vertical ffprobe result | |
| Manual filename chronology result | |
| Original hash comparison | |
| Warnings, skipped inputs, fallbacks | |
| Resume and disconnected-volume result | |
| Discrepancies and follow-up owner | |

Record every discrepancy before any compatibility claim. A passing benchmark means only that tested authorized footage completed under recorded conditions; it does not add automatic intelligence or generalize to all HERO12 settings.
