# Phase 1 Local Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local-first `video-editor` CLI that orders GoPro footage, protects external-storage workflows, persists resumable jobs, and renders validated horizontal and vertical MP4 files from deterministic edit plans.

**Architecture:** Typed Python units own configuration, discovery, chronology, inspection, plans, persistence, rendering, validation, and reporting. FFmpeg and ffprobe run through argument vectors without a shell; SQLite commits stage state and cache provenance. Phase 1 uses deterministic sample selection only and labels that limitation in every report.

**Tech Stack:** Python 3.12+, uv, Typer, Pydantic v2, JSON Schema, SQLite, FFmpeg/ffprobe, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-18-phase-1-local-foundation-design.md`

## Global Constraints

- CLI name is `video-editor`; Python package is `video_editor`.
- Originals are read-only and never cleanup targets.
- Cloud upload, paid APIs, automatic highlight intelligence, transcription, and content-aware tracking remain disabled and unimplemented.
- Long-form timeline must be strictly shorter than 60 minutes.
- Software `libx264` rendering is required; hardware encoding is optional and capability-gated.
- FFmpeg/ffprobe commands use `subprocess` argument sequences with `shell=False`.
- GoPro filenames are untrusted evidence; trustworthy embedded creation time wins chronology conflicts.
- Missing configured external volumes are errors; never silently redirect work to internal storage.
- Partial outputs live on the final output volume and become final only after validation.
- One render runs at a time.
- Tests generate tiny media fixtures; tests never call network or paid services.
- Every implementation task follows RED → GREEN → REFACTOR and ends with targeted tests plus a commit.

---

## File Map

- `pyproject.toml`: package metadata, `video-editor` entry point, dependencies, lint/type/test settings.
- `src/video_editor/config.py`: TOML settings and resolved path policy.
- `src/video_editor/errors.py`: stable error categories and CLI-safe exceptions.
- `src/video_editor/media/discovery.py`: recursive candidate discovery and bounded source fingerprints.
- `src/video_editor/media/sequencing.py`: GoPro parsing, grouping, chronology evidence, and conflicts.
- `src/video_editor/media/probe.py`: ffprobe execution and typed stream metadata.
- `src/video_editor/media/capabilities.py`: host, disk, RAM, FFmpeg, and encoder detection.
- `src/video_editor/media/storage.py`: volume identity, free-space checks, estimates, and safe containment.
- `src/video_editor/media/proxies.py`: analysis proxy/audio generation and timestamp mappings.
- `src/video_editor/models/edit_plan.py`: versioned plan models and semantic validation.
- `schemas/edit-plan-v1.json`: portable schema for persisted plans.
- `src/video_editor/planning/sample_plan.py`: deterministic horizontal and vertical sample plans.
- `src/video_editor/persistence/database.py`: SQLite schema and repositories.
- `src/video_editor/rendering/compiler.py`: validated plan to FFmpeg argument vector.
- `src/video_editor/rendering/runner.py`: subprocess lifecycle and interruption handling.
- `src/video_editor/validation/outputs.py`: ffprobe-based output checks and finalization.
- `src/video_editor/reporting.py`: JSON state and Markdown report generation.
- `src/video_editor/workflow.py`: inspect, plan, run, resume stage orchestration.
- `src/video_editor/cli.py`: Typer commands and exit-code mapping.
- `tests/fixtures.py`: deterministic FFmpeg fixture factory.
- `tests/unit/`: isolated behavior tests.
- `tests/integration/`: real FFmpeg end-to-end tests.
- `README.md`, `config.example.toml`, `.env.example`, `docs/architecture.md`, `docs/benchmark.md`: setup, usage, limits, privacy, storage, and benchmarking.

---

### Task 1: Package Scaffold and Error Contract

**Files:**
- Create: `pyproject.toml`
- Create: `src/video_editor/__init__.py`
- Create: `src/video_editor/cli.py`
- Create: `src/video_editor/errors.py`
- Create: `tests/unit/test_cli.py`

**Interfaces:**
- Produces: `video_editor.errors.ErrorCategory`, `VideoEditorError`, and Typer `app`.
- Produces CLI executable: `video-editor --help`.

- [ ] **Step 1: Write failing CLI contract test**

```python
from typer.testing import CliRunner
from video_editor.cli import app


def test_cli_exposes_phase_one_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("inspect", "plan", "render-from-plan", "run", "status", "resume"):
        assert command in result.stdout
```

- [ ] **Step 2: Run red test**

Run: `uv run pytest tests/unit/test_cli.py -v`

Expected: FAIL because package and `app` do not exist.

- [ ] **Step 3: Add minimal package and stable errors**

```python
# errors.py
from enum import StrEnum

class ErrorCategory(StrEnum):
    CONFIGURATION = "configuration"
    STORAGE = "storage"
    INSPECTION = "inspection"
    PLAN = "plan_validation"
    RENDER = "rendering"
    OUTPUT = "output_validation"
    STATE = "state"

class VideoEditorError(Exception):
    def __init__(self, category: ErrorCategory, message: str) -> None:
        super().__init__(message)
        self.category = category
```

Create Typer commands with help text; command bodies may print `not implemented` and raise exit code 2 until their owning tasks replace them. Configure `pyproject.toml` with Python `>=3.12`, Typer, Pydantic v2, pytest, Ruff, and mypy; map `video-editor = "video_editor.cli:app"`.

- [ ] **Step 4: Run checks**

Run: `uv sync && uv run pytest tests/unit/test_cli.py -v && uv run video-editor --help`

Expected: test PASS; help lists six commands.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/video_editor tests/unit/test_cli.py
git commit -m "chore: scaffold video editor CLI"
```

---

### Task 2: Configuration and External-Volume Safety

**Files:**
- Create: `src/video_editor/config.py`
- Create: `src/video_editor/media/storage.py`
- Create: `tests/unit/test_config.py`
- Create: `tests/unit/test_storage.py`
- Create: `config.example.toml`

**Interfaces:**
- Produces: `PathSettings(input_dir, workspace_dir, cache_dir, output_dir, state_dir)`.
- Produces: `AppConfig(paths, storage_reserve_bytes, cloud_enabled=False, render_concurrency=1)`.
- Produces: `VolumeIdentity(device: int, mount_point: Path, filesystem: str | None)`.
- Produces: `resolve_config(path: Path) -> AppConfig`, `inspect_volume(path: Path) -> VolumeIdentity`, `assert_expected_volume(path: Path, expected: VolumeIdentity) -> None`, and `assert_free_space(path: Path, required_bytes: int, reserve_bytes: int) -> None`.

- [ ] **Step 1: Write failing path and volume tests**

```python
def test_config_expands_paths_without_redirecting_missing_volume(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[paths]\ninput_dir="/Volumes/Missing/footage"\nworkspace_dir="/Volumes/Missing/work"\ncache_dir="/Volumes/Missing/cache"\noutput_dir="/Volumes/Missing/out"\nstate_dir="~/Library/Application Support/video-editor"\n')
    config = resolve_config(config_file)
    assert config.paths.input_dir == Path("/Volumes/Missing/footage")
    with pytest.raises(VideoEditorError, match="not mounted"):
        inspect_volume(config.paths.workspace_dir)


def test_volume_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    actual = inspect_volume(tmp_path)
    wrong = VolumeIdentity(actual.device + 1, actual.mount_point, actual.filesystem)
    with pytest.raises(VideoEditorError, match="volume changed"):
        assert_expected_volume(tmp_path, wrong)
```

Also test `cloud_enabled` rejects `true` in Phase 1, free-space reserve, APFS/exFAT acceptance, and cleanup containment (`candidate.resolve().is_relative_to(root.resolve())`).

- [ ] **Step 2: Run red tests**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_storage.py -v`

Expected: FAIL because interfaces do not exist.

- [ ] **Step 3: Implement config and storage policy**

Use `tomllib`; expand `~`; never create missing `/Volumes/...` parents during validation. Derive device ID with `os.stat`, mount point by walking parents until `st_dev` changes, filesystem with `diskutil info -plist` on macOS when available, and free bytes with `shutil.disk_usage`. Raise `ErrorCategory.STORAGE` on missing path, identity mismatch, unsupported filesystem, or insufficient `free - reserve`.

- [ ] **Step 4: Run green tests**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_storage.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/video_editor/config.py src/video_editor/media/storage.py tests/unit/test_config.py tests/unit/test_storage.py config.example.toml
git commit -m "feat: validate external storage configuration"
```

---

### Task 3: Discovery and Stable Source Identity

**Files:**
- Create: `src/video_editor/media/discovery.py`
- Create: `tests/unit/test_discovery.py`

**Interfaces:**
- Produces: `SourceCandidate(path: Path, size_bytes: int, discovery_index: int, fingerprint: str, identity_version: str)`.
- Produces: `discover_sources(root: Path, extensions: frozenset[str] = VIDEO_EXTENSIONS) -> list[SourceCandidate]`.
- Produces: `bounded_fingerprint(path: Path, chunk_bytes: int = 1_048_576) -> str`.

- [ ] **Step 1: Write failing discovery tests**

```python
def test_discovery_is_recursive_case_insensitive_and_deterministic(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "GH020001.MP4").write_bytes(b"second")
    (tmp_path / "GOPR0001.mp4").write_bytes(b"first")
    (tmp_path / "notes.txt").write_text("ignore")
    sources = discover_sources(tmp_path)
    assert [s.path.name for s in sources] == ["GOPR0001.mp4", "GH020001.MP4"]
    assert all(s.identity_version == "bounded-v1" for s in sources)
```

Add tests showing fingerprint changes when first/last bounded chunks change and does not embed absolute path.

- [ ] **Step 2: Run red test**

Run: `uv run pytest tests/unit/test_discovery.py -v`

Expected: FAIL because discovery module does not exist.

- [ ] **Step 3: Implement deterministic discovery**

Sort by relative POSIX path only to assign discovery index; do not claim chronological meaning. Hash identity version, size, first chunk, and last chunk with SHA-256. Return empty list for readable empty folders; reject nonexistent/unreadable roots with `ErrorCategory.INSPECTION`.

- [ ] **Step 4: Run green test**

Run: `uv run pytest tests/unit/test_discovery.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/video_editor/media/discovery.py tests/unit/test_discovery.py
git commit -m "feat: discover media with stable fingerprints"
```

---

### Task 4: GoPro Filename Sequencing and Chronology

**Files:**
- Create: `src/video_editor/media/sequencing.py`
- Create: `tests/unit/test_sequencing.py`

**Interfaces:**
- Consumes: `SourceCandidate`.
- Produces: `ParsedSequence(pattern: str, file_number: int, chapter: int, session: str | None)`.
- Produces: `SequencedSource(source, parsed, creation_time, order_evidence, confidence, warnings)`.
- Produces: `ChronologyGroup(group_id: str, members: tuple[SequencedSource, ...], warnings: tuple[str, ...])`.
- Produces: `parse_gopro_name(name: str) -> ParsedSequence | None` and `sequence_sources(sources: Sequence[SourceCandidate], creation_times: Mapping[str, datetime | None]) -> list[ChronologyGroup]`.

- [ ] **Step 1: Write failing parser and ordering tests**

```python
@pytest.mark.parametrize(("name", "file_number", "chapter"), [
    ("GOPR0123.MP4", 123, 1),
    ("GP020123.MP4", 123, 2),
    ("GH010456.MP4", 456, 1),
    ("GX030456.MP4", 456, 3),
])
def test_parse_observed_gopro_shapes(name: str, file_number: int, chapter: int) -> None:
    parsed = parse_gopro_name(name)
    assert parsed is not None
    assert (parsed.file_number, parsed.chapter) == (file_number, chapter)


def test_chapters_group_by_file_number_and_order_numerically(candidates) -> None:
    groups = sequence_sources(candidates(["GP030123.MP4", "GOPR0123.MP4", "GP020123.MP4"]), {})
    assert [[m.source.path.name for m in g.members] for g in groups] == [["GOPR0123.MP4", "GP020123.MP4", "GP030123.MP4"]]
```

Add tests for names containing shell metacharacters returning `None`, duplicate chapters warning, numbering reset warning, metadata-first group order, filename/metadata conflict warning, and non-GoPro fallback to discovery order with low confidence.

- [ ] **Step 2: Run red tests**

Run: `uv run pytest tests/unit/test_sequencing.py -v`

Expected: FAIL because sequencing module does not exist.

- [ ] **Step 3: Implement conservative parser and precedence**

Use full-match ASCII regexes only. Recognize observed classic `GOPR####` first chapters and `G[A-Z]CCFFFF` chapter files, where `CC` and `FFFF` are parsed decimal numbers; preserve matched pattern label. Do not infer chronology from prefix letters. Group matching file numbers, sort members by chapter, then sort groups by trustworthy minimum creation time; groups lacking time use numeric continuity and finally discovery index. Emit structured warnings whenever evidence conflicts, duplicates, resets, or remains uncertain.

- [ ] **Step 4: Run green tests**

Run: `uv run pytest tests/unit/test_sequencing.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/video_editor/media/sequencing.py tests/unit/test_sequencing.py
git commit -m "feat: sequence GoPro chapters chronologically"
```

---

### Task 5: ffprobe Inspection and Capability Detection

**Files:**
- Create: `src/video_editor/media/probe.py`
- Create: `src/video_editor/media/capabilities.py`
- Create: `tests/unit/test_probe.py`
- Create: `tests/unit/test_capabilities.py`

**Interfaces:**
- Produces: `VideoStream`, `AudioStream`, `MediaProbe`, `InspectionWarning`, and `HostCapabilities` Pydantic models.
- Produces: `probe_media(path: Path, ffprobe: str = "ffprobe") -> MediaProbe`.
- Produces: `detect_capabilities(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> HostCapabilities`.
- Produces: `summarize_batch_warnings(probes: Sequence[MediaProbe]) -> list[InspectionWarning]`.

- [ ] **Step 1: Write failing JSON parsing tests**

Mock `subprocess.run` with ffprobe JSON containing HEVC Main 10, `yuv420p10le`, rotation, HDR transfer, unequal `avg_frame_rate` and `r_frame_rate`, no audio, and `creation_time`. Assert typed fields and warning codes: `hevc`, `ten_bit`, `rotation`, `hdr`, `possible_vfr`, `missing_audio`.

```python
def test_probe_uses_argument_vector_without_shell(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)) or completed_probe())
    probe_media(tmp_path / "clip;touch owned.mp4")
    assert calls[0][0][-1].endswith("clip;touch owned.mp4")
    assert calls[0][1].get("shell", False) is False
```

- [ ] **Step 2: Run red tests**

Run: `uv run pytest tests/unit/test_probe.py tests/unit/test_capabilities.py -v`

Expected: FAIL because modules do not exist.

- [ ] **Step 3: Implement inspection**

Call ffprobe with `-v error -show_format -show_streams -print_format json`. Parse rational numbers safely. Preserve unknown color as `unknown_color` warning; do not assign LUTs. Capability detection parses `ffmpeg -encoders` and records versions, architecture, macOS memory via `sysctl -n hw.memsize` when available, and encoder candidates. A VideoToolbox encoder is merely available until Task 10 proves it with a render probe.

- [ ] **Step 4: Run green tests**

Run: `uv run pytest tests/unit/test_probe.py tests/unit/test_capabilities.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/video_editor/media/probe.py src/video_editor/media/capabilities.py tests/unit/test_probe.py tests/unit/test_capabilities.py
git commit -m "feat: inspect media and host capabilities"
```

---

### Task 6: Persistent Job State, Chronology, and Cache

**Files:**
- Create: `src/video_editor/persistence/database.py`
- Create: `tests/unit/test_database.py`

**Interfaces:**
- Produces: `JobStatus` and `StageStatus` string enums.
- Produces: `JobStore(db_path: Path)` context manager.
- Methods: `create_job(config_json, volume_json) -> str`, `start_stage(job_id, name, input_fingerprint, settings_hash, implementation_version)`, `complete_stage(...)`, `fail_stage(...)`, `save_sources(...)`, `save_chronology(...)`, `save_artifact(...)`, `find_reusable_stage(...)`, and `get_job(job_id) -> dict[str, Any]`.

- [ ] **Step 1: Write failing transaction and resume tests**

```python
def test_failed_stage_retains_prior_completed_stage(tmp_path: Path) -> None:
    with JobStore(tmp_path / "state.db") as store:
        job = store.create_job("{}", "{}")
        store.start_stage(job, "inspect", "a", "b", "v1")
        store.complete_stage(job, "inspect", "{}")
        store.start_stage(job, "render", "c", "d", "v1")
        store.fail_stage(job, "render", "rendering", "encoder failed", interrupted=False)
        state = store.get_job(job)
    assert state["stages"]["inspect"]["status"] == "completed"
    assert state["stages"]["render"]["status"] == "failed"
```

Add tests for parameterized values containing quotes, interrupted status, chronology persistence, cache reuse only on all-key match, and missing artifact invalidating reuse.

- [ ] **Step 2: Run red test**

Run: `uv run pytest tests/unit/test_database.py -v`

Expected: FAIL because `JobStore` does not exist.

- [ ] **Step 3: Implement schema and repository**

Create tables named in spec plus schema metadata. Set `row_factory = sqlite3.Row`, enable foreign keys, use explicit transactions and placeholders, and serialize structured data as canonical JSON. Migration version starts at 1. `find_reusable_stage` requires matching source/settings/version plus an artifact callback proving outputs still exist and validate.

- [ ] **Step 4: Run green test**

Run: `uv run pytest tests/unit/test_database.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/video_editor/persistence/database.py tests/unit/test_database.py
git commit -m "feat: persist resumable video jobs"
```

---

### Task 7: Versioned Edit Plan and Semantic Validation

**Files:**
- Create: `src/video_editor/models/edit_plan.py`
- Create: `schemas/edit-plan-v1.json`
- Create: `tests/unit/test_edit_plan.py`

**Interfaces:**
- Produces Pydantic models: `PlanSource`, `TimelineClip`, `Transition`, `Framing`, `OutputSpec`, `EditPlan`.
- Produces: `load_plan(path: Path) -> EditPlan`, `write_plan(plan: EditPlan, path: Path) -> None`, and `timeline_duration(plan: EditPlan) -> Decimal`.

- [ ] **Step 1: Write failing semantic tests**

```python
def test_speed_and_transition_control_duration(valid_plan_data: dict) -> None:
    valid_plan_data["clips"] = [clip("a", 0, 10, speed=2), clip("a", 10, 20, speed=1)]
    valid_plan_data["transitions"] = [{"from_clip": 0, "to_clip": 1, "kind": "dissolve", "duration": 1}]
    plan = EditPlan.model_validate(valid_plan_data)
    assert timeline_duration(plan) == Decimal("14")


def test_long_form_must_be_strictly_below_one_hour(valid_plan_data: dict) -> None:
    valid_plan_data["output"]["kind"] = "long"
    valid_plan_data["clips"] = [clip("a", 0, 3600)]
    with pytest.raises(ValidationError, match="strictly shorter"):
        EditPlan.model_validate(valid_plan_data)
```

Add tests for unknown source, missing/changed identity, negative/out-of-range intervals, speed `<=0`, overlap without transition, gap, impossible transition, unsupported primitive, missing dimensions, shell-command field rejection (`extra="forbid"`), and missing-audio plans.

- [ ] **Step 2: Run red tests**

Run: `uv run pytest tests/unit/test_edit_plan.py -v`

Expected: FAIL because models do not exist.

- [ ] **Step 3: Implement models and checked-in schema**

Use Decimal for timeline arithmetic. Restrict Phase 1 transitions to `cut` and fixture-only `dissolve`; framing to `center_crop` and `fit_background`; color to `passthrough` or explicit declared override. Require schema version `1`. Generate `schemas/edit-plan-v1.json` from `EditPlan.model_json_schema()` and assert checked-in schema equality in test.

- [ ] **Step 4: Run green tests**

Run: `uv run pytest tests/unit/test_edit_plan.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/video_editor/models/edit_plan.py schemas/edit-plan-v1.json tests/unit/test_edit_plan.py
git commit -m "feat: validate versioned edit plans"
```

---

### Task 8: Proxies, Audio, and Timestamp Mapping

**Files:**
- Create: `src/video_editor/media/proxies.py`
- Create: `tests/unit/test_proxies.py`
- Create: `tests/fixtures.py`

**Interfaces:**
- Produces: `ProxySettings(max_width=960, fps=15, video_codec="libx264")`.
- Produces: `ProxyMapping(source_id, source_start, source_end, proxy_start, proxy_end, settings_hash, tool_version)`.
- Produces: `build_proxy_args(...) -> list[str]`, `build_audio_args(...) -> list[str]`, and `create_analysis_media(...) -> tuple[Path, Path | None, ProxyMapping]`.

- [ ] **Step 1: Write failing argument and mapping tests**

```python
def test_audio_is_whisper_compatible(tmp_path: Path) -> None:
    args = build_audio_args(tmp_path / "in.mp4", tmp_path / "out.wav")
    assert args[-6:] == ["-vn", "-acodec", "pcm_s16le", "-ar", "16000", str(tmp_path / "out.wav")]


def test_full_proxy_mapping_is_explicit() -> None:
    mapping = identity_mapping("source-1", Decimal("12.5"), "hash", "ffmpeg-8")
    assert (mapping.source_start, mapping.source_end) == (Decimal("0"), Decimal("12.5"))
    assert (mapping.proxy_start, mapping.proxy_end) == (Decimal("0"), Decimal("12.5"))
```

Also test no-audio returns `None`, outputs stay inside configured cache root, and source path never appears as output.

- [ ] **Step 2: Run red tests**

Run: `uv run pytest tests/unit/test_proxies.py -v`

Expected: FAIL because proxy functions do not exist.

- [ ] **Step 3: Implement proxy generation**

Build bounded proxy with aspect-preserving scale, 15 fps, H.264, muted proxy audio; separately extract mono 16 kHz PCM signed 16-bit WAV when source audio exists. Run with argument vectors. Write to `.partial` on cache volume, probe, then rename in place. Use deterministic settings hash.

- [ ] **Step 4: Run green tests**

Run: `uv run pytest tests/unit/test_proxies.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/video_editor/media/proxies.py tests/unit/test_proxies.py tests/fixtures.py
git commit -m "feat: generate mapped analysis proxies"
```

---

### Task 9: Deterministic Sample Planner

**Files:**
- Create: `src/video_editor/planning/sample_plan.py`
- Create: `tests/unit/test_sample_plan.py`

**Interfaces:**
- Consumes ordered `ChronologyGroup` values and `MediaProbe` mappings.
- Produces: `create_sample_plans(...) -> tuple[EditPlan, EditPlan]`.

- [ ] **Step 1: Write failing planner tests**

```python
def test_sample_plans_have_required_outputs(ordered_sources, probes, paths) -> None:
    horizontal, vertical = create_sample_plans(ordered_sources, probes, paths)
    assert (horizontal.output.width, horizontal.output.height) == (1920, 1080)
    assert (vertical.output.width, vertical.output.height) == (1080, 1920)
    assert vertical.clips[0].framing.mode == "fit_background"
    assert all(c.selection_reason == "phase1_sample" and c.confidence is None for c in horizontal.clips)
```

Add tests that planner follows chronology groups, caps requested interval to source duration, preserves audio availability, produces no gaps, and never pads weak/absent material.

- [ ] **Step 2: Run red tests**

Run: `uv run pytest tests/unit/test_sample_plan.py -v`

Expected: FAIL because planner does not exist.

- [ ] **Step 3: Implement minimal deterministic selection**

Choose up to a configurable 8 seconds from each ordered source until a small fixture-oriented cap is reached. Use clean cuts. Horizontal uses aspect-preserving fit to 1920×1080; vertical defaults to `fit_background`, never center-crop without explicit override. Label provenance `planner="phase1-sample-v1"`.

- [ ] **Step 4: Run green tests**

Run: `uv run pytest tests/unit/test_sample_plan.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/video_editor/planning/sample_plan.py tests/unit/test_sample_plan.py
git commit -m "feat: create deterministic sample edit plans"
```

---

### Task 10: Rendering Compiler, Runner, and Output Validation

**Files:**
- Create: `src/video_editor/rendering/compiler.py`
- Create: `src/video_editor/rendering/runner.py`
- Create: `src/video_editor/validation/outputs.py`
- Create: `tests/unit/test_compiler.py`
- Create: `tests/unit/test_runner.py`
- Create: `tests/integration/test_render.py`

**Interfaces:**
- Produces: `RenderCommand(args: tuple[str, ...], partial_path: Path, final_path: Path, expected_duration: Decimal)`.
- Produces: `compile_render(plan: EditPlan, ffmpeg: str, encoder: str = "libx264") -> RenderCommand`.
- Produces: `run_render(command: RenderCommand, on_interrupt: Callable[[], None]) -> None`.
- Produces: `validate_output(path: Path, output: OutputSpec, expected_duration: Decimal, tolerance: Decimal = Decimal("0.20")) -> MediaProbe`.
- Produces: `probe_hardware_encoder(ffmpeg: str, encoder: str) -> bool`.

- [ ] **Step 1: Write failing compiler security and timing tests**

```python
def test_compiler_never_returns_shell_text(plan_with_hostile_filename: EditPlan) -> None:
    command = compile_render(plan_with_hostile_filename, "ffmpeg")
    assert isinstance(command.args, tuple)
    assert command.args[0] == "ffmpeg"
    assert ";touch owned" in command.args


def test_partial_output_is_on_final_volume(valid_plan: EditPlan) -> None:
    command = compile_render(valid_plan, "ffmpeg")
    assert command.partial_path.parent == command.final_path.parent
    assert command.partial_path.suffix == ".partial"
```

Add unit assertions for trim/speed filters, transition duration, audio silence when missing, 1920×1080 fit, 1080×1920 blurred-background fit, and center crop. Add integration test rendering generated tone and silent clips with `libx264`.

- [ ] **Step 2: Run red tests**

Run: `uv run pytest tests/unit/test_compiler.py tests/unit/test_runner.py tests/integration/test_render.py -v`

Expected: FAIL because renderer does not exist.

- [ ] **Step 3: Implement deterministic rendering**

Construct one `-filter_complex` graph owned by compiler. Normalize pixel format to `yuv420p`, resample audio, and avoid arbitrary metadata interpolation. Run process with `Popen(args, shell=False)`. On SIGINT/SIGTERM, terminate child, wait with timeout, kill only if needed, invoke interruption callback, and keep registered partial artifact. Validate readability, video dimensions, expected duration tolerance, and audio presence policy before same-directory `Path.replace` finalization. Probe VideoToolbox with a generated 16-frame null input; use it only on successful exit and valid probe, otherwise `libx264`.

- [ ] **Step 4: Run green tests**

Run: `uv run pytest tests/unit/test_compiler.py tests/unit/test_runner.py tests/integration/test_render.py -v`

Expected: PASS using software encoder; hardware test skips or reports capability.

- [ ] **Step 5: Commit**

```bash
git add src/video_editor/rendering src/video_editor/validation tests/unit/test_compiler.py tests/unit/test_runner.py tests/integration/test_render.py
git commit -m "feat: render and validate edit plans"
```

---

### Task 11: Workflow, Reports, and CLI Commands

**Files:**
- Create: `src/video_editor/workflow.py`
- Create: `src/video_editor/reporting.py`
- Modify: `src/video_editor/cli.py`
- Create: `tests/unit/test_workflow.py`
- Create: `tests/unit/test_reporting.py`
- Create: `tests/integration/test_cli_workflow.py`

**Interfaces:**
- Produces: `WorkflowService(config: AppConfig, store: JobStore)`.
- Methods: `inspect(input_path)`, `plan(input_path, output_path)`, `render_from_plan(plan_path)`, `run(input_path)`, `status(job_id)`, and `resume(job_id)`.
- Produces: `write_json_report(job_state, path) -> Path` and `write_markdown_report(job_state, path) -> Path`.

- [ ] **Step 1: Write failing resume and reporting tests**

```python
def test_resume_skips_valid_completed_stage(service, store, job_id, spy_probe) -> None:
    store.complete_stage(job_id, "inspect", '{"artifact":"inventory.json"}')
    service.resume(job_id)
    spy_probe.assert_not_called()


def test_report_discloses_phase_one_limitations(job_state, tmp_path: Path) -> None:
    report = write_markdown_report(job_state, tmp_path / "report.md").read_text()
    assert "deterministic sample selection" in report
    assert "not automatic highlight intelligence" in report
    assert "Cloud usage: 0" in report
```

Add tests for vanished external artifact forcing stage rerun, missing expected volume failing before writes, per-source probe failure isolation, stable exit categories, and six CLI commands invoking service methods.

- [ ] **Step 2: Run red tests**

Run: `uv run pytest tests/unit/test_workflow.py tests/unit/test_reporting.py tests/integration/test_cli_workflow.py -v`

Expected: FAIL because workflow/reporting do not exist and CLI stubs remain.

- [ ] **Step 3: Implement stage orchestration**

Before write-heavy stages, verify recorded volume identity and free-space estimate. Persist stage start before work and completion only after artifacts validate. `resume` walks `inspect`, `proxy`, `plan`, `render`, `validate`, `report`, reusing only matching valid results. Reports include selected moments, output dimensions/durations/codecs, skipped inputs, warnings, fallbacks, stage times, resolved storage roots, estimated peak space, zero cloud cost, and Phase 1 limitation statement. Map `VideoEditorError.category` to documented nonzero exit codes.

- [ ] **Step 4: Run green tests**

Run: `uv run pytest tests/unit/test_workflow.py tests/unit/test_reporting.py tests/integration/test_cli_workflow.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/video_editor/workflow.py src/video_editor/reporting.py src/video_editor/cli.py tests/unit/test_workflow.py tests/unit/test_reporting.py tests/integration/test_cli_workflow.py
git commit -m "feat: orchestrate resumable video editing jobs"
```

---

### Task 12: End-to-End Fixtures, Documentation, and Quality Gate

**Files:**
- Create: `tests/integration/test_end_to_end.py`
- Create: `README.md`
- Create: `.env.example`
- Create: `docs/architecture.md`
- Create: `docs/benchmark.md`
- Modify: `config.example.toml`

**Interfaces:**
- Verifies public CLI and artifact contract.
- Documents real HERO12 benchmark gate; does not claim completion without user footage.

- [ ] **Step 1: Write failing end-to-end acceptance test**

```python
def test_run_produces_validated_outputs_without_changing_originals(media_batch, cli, config_file) -> None:
    before = hash_tree(media_batch.input_dir)
    result = cli.invoke(app, ["run", str(media_batch.input_dir), "--config", str(config_file)])
    assert result.exit_code == 0, result.stdout
    assert ffprobe_size(media_batch.output_dir / "long.mp4") == (1920, 1080)
    assert ffprobe_size(media_batch.output_dir / "short-01.mp4") == (1080, 1920)
    assert (media_batch.output_dir / "edit-plan-horizontal.json").exists()
    assert (media_batch.output_dir / "edit-plan-vertical.json").exists()
    assert (media_batch.output_dir / "report.json").exists()
    assert (media_batch.output_dir / "report.md").exists()
    assert hash_tree(media_batch.input_dir) == before
```

Fixture batch must include tone video, silent video, GoPro chapter names, multiple sessions, and one unreadable candidate. Assert chronological plan order, strictly sub-hour outputs, status visibility, and resume without duplicate renders.

- [ ] **Step 2: Run red acceptance test**

Run: `uv run pytest tests/integration/test_end_to_end.py -v`

Expected: FAIL until full artifact naming and wiring match acceptance contract.

- [ ] **Step 3: Complete documentation and exact examples**

README covers Apple Silicon prerequisites (`brew install ffmpeg uv`), `uv sync`, all six commands, external SSD paths, no silent fallback, APFS/exFAT notes, privacy, zero cloud calls, deterministic-selection limitation, unsupported media warnings, cleanup boundaries, and troubleshooting. `docs/benchmark.md` requires copying representative HERO12 files to an authorized input folder, recording camera settings, running `inspect` then `run`, capturing wall time/peak disk/output probes, manually verifying filename chronology, and recording discrepancies before claiming real HERO12 support.

- [ ] **Step 4: Run complete verification**

Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -v
uv build
uv run video-editor --help
```

Expected: all commands exit 0. End-to-end outputs validate with ffprobe. No test accesses network. If real HERO12 media is unavailable, report synthetic-fixture success and real-HERO12 benchmark pending.

- [ ] **Step 5: Check artifact wiring and Git state**

Run:

```bash
test -f README.md
test -f schemas/edit-plan-v1.json
test -f docs/benchmark.md
git diff --check
git status --short
```

Expected: required files exist; no whitespace errors; only intended task changes appear.

- [ ] **Step 6: Commit**

```bash
git add README.md .env.example config.example.toml docs/architecture.md docs/benchmark.md tests/integration/test_end_to_end.py
git commit -m "docs: complete phase one local workflow"
```

---

## Final Verification Gate

Before calling Phase 1 complete:

1. Run the complete Task 12 quality gate from a clean checkout on the feature branch.
2. Inspect generated JSON plan and report; ensure provenance says `phase1-sample-v1`.
3. Verify horizontal and vertical dimensions with independent ffprobe calls.
4. Hash fixture originals before and after run; hashes must match.
5. Disconnect or simulate missing external volume; command must fail without creating internal fallback directories.
6. Interrupt a render; `status` must show interrupted and `resume` must continue without redoing valid prior stages.
7. Confirm synthetic GoPro chronology tests pass.
8. Do not claim real HERO12 support until benchmark procedure runs against user-provided HERO12 files.
9. Invoke `superpowers:verification-before-completion` before completion claim.
10. Invoke `superpowers:requesting-code-review` before merge or PR.
