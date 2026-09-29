# Task 4 Security Fix Round 1B Report

Date: 2026-09-30

## Status

Complete in isolated worktree `/Users/coffeemug/Programming/automated-video-editing/.claude/worktrees/agent-a752ccc2e0403fabe`, based on requested feature commit `9e8838b`.

## Scope

Implemented security fix round 1B:

- Descriptor-bound upload authorization returns a context-managed binary stream, never an upload path.
- Upload opens candidate once with no-follow semantics; digest, file facts, ffprobe, and uploader consumption use that same descriptor.
- Registration opens generated media once, validates digest/file facts/media through that descriptor, and persists only matching evidence.
- Generation, registration, and upload share strict stream-count/media evidence parsing and policy checks.
- Upload requires caller job ID and job-scoped manifest/chunk lookups.
- Generated root must be direct configured-root child named exactly as caller job ID.
- Every resolvable persisted source path and inode is protected across jobs; missing unrelated historical sources do not block safe uploads.
- Persisted source device, inode, size, fingerprint, path, and identity are revalidated.
- Persisted Phase 1 source identity, settings hash, tool version, monotonic ranges, equal-duration translation, and containment are validated at creation and upload.

No network or live Gemini calls were used. No subagents were dispatched.

## GitNexus Impact Gates

MCP initially returned `UNKNOWN` for new Task 4 symbols because workspace graph did not resolve them. Exact CLI impact plus text-reference confirmation was used as required.

Pre-edit exact CLI results:

- `register_proxy_manifest`: LOW; 0 direct callers; 0 affected processes.
- `_validate_candidate_path`: LOW; 1 direct caller (`validate_upload_candidate`); 1 affected process.
- `_validate_source_identity`: LOW; 1 direct caller (`validate_upload_candidate`); 1 affected process.
- `validate_upload_candidate`: LOW; 0 indexed callers; test callers confirmed by text search.
- `get_proxy_manifest`: LOW; 1 direct caller; 1 affected process.
- `get_analysis_chunk`: LOW; 1 direct caller; 1 affected process.
- `create_cloud_proxy_chunk`: LOW; 0 indexed callers; test callers confirmed by text search.
- `_validated_probe`: LOW; 1 direct caller (`create_cloud_proxy_chunk`); 1 affected process.
- `CloudProxySettings`: LOW; 0 indexed callers; test uses confirmed by text search.
- `JobStore`: LOW; 2 direct dependants; 0 affected processes.
- `_validate_probe`: LOW; 1 direct caller (`validate_upload_candidate`); 1 affected process.
- New helpers/classes unresolved by stale graph were confirmed by exact text search to be local to Task 4 module/tests.

No HIGH or CRITICAL impact result occurred.

Required pre-commit graph comparison:

```text
detect_changes(scope="compare", base_ref="main", worktree="/Users/coffeemug/Programming/automated-video-editing/.claude/worktrees/agent-a752ccc2e0403fabe", repo="automated-video-editing")
changed_count: 113
affected_count: 0
changed_files: 28
risk_level: low
affected_processes: []
```

Comparison includes Phase 2 work between `main` and feature base, not only round 1B files.

## Observed RED-GREEN Evidence

Each remaining review defect received an observed failing test before implementation:

1. Unsafe upload API exposed `AuthorizedUpload.path`; RED asserted no reopenable path. GREEN removed path from authorization object.
2. Cross-job generated root was accepted; RED moved valid bytes under another job root. GREEN binds direct root name to `expected_job_id`.
3. Same-byte source inode replacement was accepted; RED replaced source inode while preserving bytes. GREEN checks persisted device/inode/size.
4. Missing unrelated historical source raised `FileNotFoundError`; RED added deleted cross-job source. GREEN skips only missing historical paths while retaining resolvable path/inode checks.
5. Registration accepted forged digest facts; RED registered valid media with false digest. GREEN hashes/probes one no-follow descriptor and checks size/device/inode.
6. Upload used globally keyed manifest/chunk reads; RED replaced unscoped methods with failing sentinels. GREEN added and used job-scoped SQL lookups.
7. Creation accepted mapping settings differing from persisted Phase 1 metadata; RED supplied forged settings. GREEN validates persisted mapping before generation when store is provided; production fixture now passes store.
8. Registration accepted forged manifest media facts; RED changed manifest width only. GREEN compares observed descriptor evidence against manifest.
9. Generation used separate probe path; RED counted shared parser calls and observed zero. GREEN routes generation through shared `_probe_media_evidence` and policy.

Earlier round 1B RED-GREEN cycles also covered validation-to-open pathname swap, descriptor cleanup, expected-job mismatch, cross-job source path and hard-link identity, Phase 1 identity/settings/tool/range mismatches, out-of-range mapping, forged media profile, missing/multiple streams, H.264, AAC mono, width, 15 FPS, and 128 kbps rejection.

Representative RED outputs:

```text
assert not hasattr(authorized, "path")
FAILED: hasattr(...) was True

with pytest.raises(VideoEditorError, match="expected job")
FAILED: DID NOT RAISE VideoEditorError

with pytest.raises(VideoEditorError, match="source file identity")
FAILED: DID NOT RAISE VideoEditorError

with pytest.raises(VideoEditorError, match="digest")
FAILED: DID NOT RAISE VideoEditorError

pytest.fail("unscoped manifest lookup used")
FAILED: unscoped manifest lookup used

create_cloud_proxy_chunk(..., store=store)
FAILED: unexpected keyword argument 'store'

assert calls == 1
FAILED: assert 0 == 1
```

Focused GREEN after all changes:

```text
uv run pytest tests/unit/test_proxy_chunks.py tests/unit/test_proxies.py -q
112 passed in 7.82s
```

## Files Changed

Committed Task 4 files:

- `src/video_editor/analysis/proxy_chunks.py`
- `src/video_editor/persistence/database.py`
- `tests/unit/test_proxy_chunks.py`

Excluded from commit:

- `task_plan.md` (execution artifact)
- GitNexus-generated `CLAUDE.md`, `AGENTS.md`, and `.claude/skills/**` changes created while refreshing the local index
- `dist/` build outputs, removed after verification

## Verification Results

Final required commands and results:

```text
uv run pytest tests/unit/test_proxy_chunks.py tests/unit/test_proxies.py -q
112 passed in 7.82s

uv run pytest -q
500 passed in 34.44s

uv run mypy src
Success: no issues found in 27 source files

uv run ruff check .
All checks passed!

uv run ruff format --check src tests
51 files already formatted

uv build
Successfully built dist/video_editor-0.1.0.tar.gz
Successfully built dist/video_editor-0.1.0-py3-none-any.whl

git diff --check
passed with no output
```

Build outputs were removed afterward. Full suite is offline/non-live; no Gemini credentials or network use.

## Self-Review

- Upload-authorized object has only manifest ID, stream, and lifecycle methods; no path can be reopened downstream.
- Descriptor closes on validation failure and context exit.
- Upload digest and probe run against `/dev/fd/<fd>` with `pass_fds`; stream rewinds before return.
- Registration digest, file facts, media policy, and manifest comparison use one no-follow descriptor.
- H.264, exactly one video, exactly one AAC mono audio stream, width no greater than 640, 15 FPS, mapped duration, and configured 64 kbps encoder policy are enforced independently of manifest claims.
- Observed AAC bitrate is content-dependent for VBR AAC (silent 64k-encoded fixture probes near 0.6 kbps); policy rejects non-positive values and values above 72 kbps, while exact encoder target remains persisted and required as `audio_bitrate_bps == 64000`.
- Job scope enters SQL predicates for manifest/chunk reads before deserialization.
- Global source protection retains resolvable path/inode checks and avoids denial from deleted unrelated records.
- Creation-time upstream validation is active when a `JobStore` is supplied; Task 4 production fixture supplies it.
- No shell interpolation added; subprocess calls remain argument arrays with `shell=False`.

## Concerns

- `create_cloud_proxy_chunk` keeps `store` optional for backward compatibility with existing direct callers. Upload remains fail-closed and always requires persisted Phase 1 evidence. Callers that require creation-time persisted validation must supply `store`; current registered Task 4 path does.
- AAC `bit_rate` from ffprobe measures observed average, not encoder option. Silent media encoded with `-b:a 64k` reports far below 64 kbps. Exact observed equality to 64000 would reject valid output, so encoder target and observed upper bound are validated separately.
- GitNexus refresh generated unrelated tracked/untracked instruction files in isolated worktree. They were intentionally excluded from Task 4 commit.

## Round 1B.1 Remediation

Date: 2026-09-30

### Status

Complete. Closed three remaining Important findings without weakening descriptor-bound authorization or job-scoped reads. No network/live Gemini use. No subagents dispatched.

### Pre-Edit GitNexus Impact

- `_parse_probe_payload`: `UNKNOWN`; exact text references confirmed local generation/registration parser usage.
- `register_proxy_manifest`: LOW; 0 direct indexed callers; 0 affected processes.
- `_validate_candidate_path`: LOW; 1 direct caller (`validate_upload_candidate`); 1 affected process.
- `validate_upload_candidate`: LOW; 0 indexed callers/processes; test callers confirmed by text search.
- `create_cloud_proxy_chunk`: LOW; 0 indexed callers/processes; direct test callers confirmed by text search.
- `ProxyManifest`: LOW; 0 indexed dependants/processes; construction references confirmed by text search.
- No HIGH or CRITICAL result occurred.

### Exact Observed RED Evidence

All required regressions failed before production edits:

```text
uv run pytest tests/unit/test_proxy_chunks.py::test_upload_capable_creation_requires_persisted_phase1_store -q
FAILED: DID NOT RAISE any of (TypeError, VideoEditorError)

uv run pytest tests/unit/test_proxy_chunks.py::test_registration_rejects_generated_media_with_extra_subtitle_stream -q
FAILED: DID NOT RAISE VideoEditorError

uv run pytest tests/unit/test_proxy_chunks.py::test_registration_rejects_caller_forged_32kbps_media -q
FAILED: DID NOT RAISE VideoEditorError

uv run pytest tests/unit/test_proxy_chunks.py::test_missing_cross_job_source_copy_is_globally_protected -q
FAILED: DID NOT RAISE VideoEditorError

uv run pytest tests/unit/test_proxy_chunks.py::test_malformed_persisted_source_identity_fails_closed -q
FAILED: DID NOT RAISE VideoEditorError
```

### GREEN Changes

- Exact stream policy now requires exactly two total streams: one video and one audio. Subtitle, attachment, data, and other extra streams fail registration.
- Generation records an HMAC proof in a private weak identity registry, bound to manifest ID, SHA-256 digest, device, inode, and size. Registration consumes this process-private proof after descriptor-bound media checks. Proof is not exposed on caller-visible manifest, so caller-reconstructed manifests, including 32 kbps media claiming a 64 kbps target, cannot register.
- Observed AAC bitrate remains content-sensitive and is not compared for exact equality with 64000, preserving silent VBR support.
- Upload parses every persisted source record. Path, fingerprint, identity version, and size must be complete and valid; malformed records fail closed.
- Upload computes bounded fingerprint from already-open candidate descriptor and rejects matches against every persisted source fingerprint, including deleted/missing-path records and copied bytes under new inode/path.
- Upload-capable `create_cloud_proxy_chunk` now requires `JobStore`; persisted Phase 1 mapping verification is unconditional before generation.
- Existing one-open/no-follow digest, ffprobe, and uploader-stream contract remains intact. Expected-job SQL scoping remains intact.

Direct regression GREEN:

```text
5 passed in 0.93s
```

Focused GREEN:

```text
uv run pytest tests/unit/test_proxy_chunks.py tests/unit/test_proxies.py -q
117 passed in 8.99s
```

### Final Verification

```text
uv run pytest -q
505 passed in 71.02s

uv run mypy src
Success: no issues found in 27 source files

uv run ruff check .
All checks passed!

uv run ruff format --check src tests
51 files already formatted

uv build
Successfully built dist/video_editor-0.1.0.tar.gz
Successfully built dist/video_editor-0.1.0-py3-none-any.whl

git diff --check
passed with no output
```

Build outputs removed after verification.

GitNexus compare against `main`:

```text
changed_count: 114
changed_files: 29
affected_count: 0
risk_level: low
affected_processes: []
partial/truncated: false
```

Comparison includes all Phase 2 feature changes since `main`, not only round 1B.1.

### Files Changed

- `src/video_editor/analysis/proxy_chunks.py`
- `tests/unit/test_proxy_chunks.py`
- `.superpowers/sdd/2026-09-28-phase-2-automatic-highlights/task-4-fix-1b-report.md`

`task_plan.md` remains an uncommitted execution artifact. GitNexus-generated instruction/skill changes remain excluded.

### Self-Review and Concerns

- Trusted proof is intentionally process-private and ephemeral. A manifest generated in one process must register in that same process. Weak object identity permits transactional registration retry while preventing caller-reconstructed objects from inheriting trust. Persisted registration remains durable for later upload authorization.
- Proof stays outside caller-visible manifest in a private weak identity registry; verification uses constant-time `hmac.compare_digest` and binds exact inspected file identity/facts.
- Every persisted source record now participates in global deny checks even when its path no longer resolves.
- Missing unrelated but complete source records remain upload-compatible unless candidate fingerprint matches.
- Fail-closed malformed legacy source records block upload. No implicit legacy bypass exists.
- No remaining known round 1B.1 concern.
