# Phase 2 Automatic Highlights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one automatic, resumable command that finds strong moments in authorized footage, creates one chronological 16:9 edit capped at 30 minutes and zero to five distinct tracked-crop 9:16 shorts capped at 3 minutes, and proves source safety, complete broad analysis, bounded Gemini cost, output validity, and evaluation quality.

**Architecture:** Extend the Phase 1 pipeline to `inspect → proxy → segment → analyze → rank → plan → render → validate → report`. Keep originals local and immutable; local code creates registered reduced proxy chunks, Gemini returns schema-constrained semantic evidence, deterministic code validates and ranks it, versioned plans drive local FFmpeg rendering, and SQLite records every cache, budget, coverage, crop, transition, and output decision.

**Tech Stack:** Python 3.12+, uv, Pydantic v2, Typer, SQLite, FFmpeg/ffprobe, OpenCV headless, google-genai, pytest, Ruff, mypy, JSON Schema.

**Spec:** `docs/superpowers/specs/2026-09-28-phase-2-automatic-highlights-design.md`

## Global Constraints

- Originals remain local and immutable; rendering reads originals, while cloud analysis may upload only registered generated proxy chunks with compressed audio.
- Preserve the configured 10 GiB storage reserve; do not delete cache or reduce reserve without explicit approval.
- Cloud-analysis cost must never exceed USD 1.00 per source hour for one job.
- Broad scan uses Gemini static video processing at 0.5 FPS; candidate refinement uses a bounded 2–5 FPS selected from local motion intensity.
- Require 100% validated broad-scan coverage of all usable source seconds before candidate refinement, ranking, planning, or rendering.
- Pin the provider model to exact ID `gemini-2.5-flash`; do not use a floating model alias.
- API credentials come only from `GEMINI_API_KEY`; never persist or print the value in job config, SQLite records, reports, artifact metadata, errors, or CLI output.
- Long output is chronological 16:9, quality-first, and no longer than 1,800 seconds.
- Generate zero to five independent 9:16 shorts, each quality-first and no longer than 180 seconds; default short cap is five.
- Different shorts may not reuse intervals with more than 10% temporal overlap relative to the shorter interval and may not reuse one semantic deduplication group, except one configured anchor moment used in at most two shorts with recorded rationale.
- Use source speech and ambience only; no music, captions, synthetic media, full-batch transcription, searchable transcripts, face identification, GUI, or publishing.
- Interpret speech meaning only inside shortlisted candidate windows; broad scan records speech presence only.
- Transition types and semantic relations must follow the approved mapping exactly: `cut` for `continuous_action`, `matched_motion`, or `same_event`; `dissolve` for `same_event` or `same_place_time_shift`; `fade` for `chapter_boundary` or `story_open_close`; `fade_black` for `chapter_boundary`, `time_jump`, or `location_change`.
- Tracked crops must stay in bounds, use monotonic timestamps, limit center velocity to 0.25 source-frame widths/second, limit acceleration to 0.50 source-frame widths/second², retain prioritized subjects with a 5% safe margin in at least 95% of sampled frames, and limit fallback holds to 2 seconds.
- Preserve Phase 1 edit-plan v1, deterministic sample planner, offline workflow, storage boundaries, resumability, artifact validation, and existing tests.
- Default test suite performs no network calls; live Gemini tests are explicit, separately marked, and budget-capped.
- Release evaluation requires at least 100 labeled candidate intervals across at least three source batches, separate train/calibration/holdout partitions, committed thresholds before holdout evaluation, and no quality claim broader than tested footage/model/settings.
- Do not implement in the current dirty `test/hero12-benchmark` worktree. Preserve Phase 1 closure, benchmark evidence, and approved spec first; inspect unrelated `CLAUDE.md`, `.claude/`, and `AGENTS.md` changes rather than discarding or bundling them blindly.
- Before editing any function, class, or method, GitNexus `impact` analysis is mandatory. Warn before HIGH or CRITICAL edits. Before every commit, run GitNexus `detect_changes({scope: "compare", base_ref: "main"})`. GitNexus currently fails with `CONNECT_TIMEOUT`; source implementation is blocked until connection works.

## Review Focus

- A symlinked, replaced, stale, or tampered proxy chunk must fail provenance checks before upload, even when its apparent path remains under the generated root; Task 4 pins this with adversarial manifest tests.
- Two concurrent request workers near the remaining budget must not both reserve the same money, and an unknown billing outcome must remain charged after restart; Task 2 pins this with two-connection SQLite tests.
- A final broad result that leaves even a sub-second usable range uncovered, duplicated without complete union coverage, or covered only by an invalid cached result must block every downstream stage and list exact missing ranges; Task 7 pins this with interval-union tests.
- Fast subject movement, temporary occlusion, and conflicting subject boxes must never create crop jumps or unsafe framing; Task 9 pins velocity, acceleration, safe-margin, fallback, and deterministic static-crop tests.
- Multiple short plans must never collide on filenames or silently share an interval/semantic group, including resume after one output completed; Tasks 3, 10, and 12 pin output identity, plan-set validation, and per-output recovery.

---

## File Map

### Configuration, errors, and dependencies

- Modify `pyproject.toml`: add compatible `google-genai>=1.0,<2.0` and `opencv-python-headless>=4.10,<5`; let `uv.lock` pin exact resolved distributions.
- Modify `uv.lock`: record exact dependency resolution.
- Modify `src/video_editor/config.py`: add immutable Gemini, ranking, crop, and short-planning settings without retaining secrets.
- Modify `src/video_editor/errors.py`: add stable analysis/provider/budget categories and safe machine-readable codes.
- Modify `config.example.toml` and `.env.example`: document Phase 1/offline and Phase 2/Gemini modes plus `GEMINI_API_KEY` handling.

### Domain models and persistence

- Create `src/video_editor/analysis/__init__.py`: public analysis package exports.
- Create `src/video_editor/analysis/models.py`: strict provider-neutral interval, manifest, evidence, response, candidate, score, crop, and usage models.
- Create `src/video_editor/persistence/migrations.py`: ordered SQLite migrations from current schema version 1.
- Modify `src/video_editor/persistence/database.py`: migration runner, manifest/result APIs, atomic budget ledger, reconciliation, and richer job serialization.
- Modify `src/video_editor/models/edit_plan.py`: retain v1 classes and add v2 plan classes, union loading, explicit output identity, evidence, tracked crop, semantic transitions, and plan-set validation.
- Create `schemas/edit-plan-v2.json`: checked-in Pydantic schema for v2; leave `schemas/edit-plan-v1.json` unchanged.

### Analysis pipeline

- Create `src/video_editor/analysis/proxy_chunks.py`: scene-safe 10–15 minute cloud proxy creation, validation, hashing, mappings, and upload provenance checks.
- Create `src/video_editor/analysis/segmentation.py`: deterministic local scene/audio/motion/technical feature extraction.
- Create `src/video_editor/analysis/pricing.py`: versioned Gemini pricing inputs and conservative token/cost arithmetic.
- Create `src/video_editor/analysis/budget.py`: job-cap computation, broad preflight reservation calculation, and request settlement facade.
- Create `src/video_editor/analysis/gemini.py`: google-genai Files API adapter, polling, static-video requests, strict response parsing, retries, and redaction.
- Create `src/video_editor/analysis/orchestrator.py`: broad/refinement orchestration, cache keys, coverage gate, normalization, and missing-range reporting.
- Create `src/video_editor/analysis/ranking.py`: score fusion, penalties, deterministic tie-breaking, deduplication, diversity, and rejection reasons.
- Create `src/video_editor/analysis/tracking.py`: local optical-flow tracking, crop smoothing, fallbacks, and numeric validation.

### Planning, rendering, workflow, and reporting

- Create `src/video_editor/planning/highlight_plan.py`: chronological long planner, zero-to-five short planner, theme selection, transition relations, and plan-set output.
- Modify `src/video_editor/rendering/compiler.py`: accept v1/v2, compile tracked crops, fade/fade-through-black, and audio policies.
- Modify `src/video_editor/validation/outputs.py`: add sampled frame, crop, black-bar, duration, and audio-boundary validation evidence.
- Modify `src/video_editor/workflow.py`: stage-specific versions and nine-stage Phase 2 orchestration while retaining Phase 1 mode.
- Modify `src/video_editor/reporting.py`: structured Phase 2 coverage/cost/ranking/planning/crop/transition sections and final-status disclosure.
- Modify `src/video_editor/cli.py`: describe Phase 2 behavior and preserve stable safe output.

### Evaluation and documentation

- Create `src/video_editor/evaluation.py`: label loading, metric formulas, partition checks, threshold checks, and release-gate result.
- Create `tests/evaluation/schema.json`: strict labeled-interval dataset contract.
- Create `tests/evaluation/thresholds-v1.json`: versioned threshold file selected from calibration before holdout execution.
- Create `tests/evaluation/synthetic-labels.json`: small non-release fixture proving formulas and partition rejection.
- Modify `README.md` and `docs/architecture.md`: configuration, privacy, cost, stages, outputs, limitations, and live-test instructions.

## Task 0: Preserve Closure State and Establish Execution Gates

**Files:**
- Verify: `README.md`
- Verify: `docs/benchmark.md`
- Verify: `docs/benchmark-records/2026-09-28/benchmark-record.md`
- Verify: `docs/superpowers/specs/2026-09-28-phase-2-automatic-highlights-design.md`
- Inspect separately: `CLAUDE.md`, `.claude/`, `AGENTS.md`
- Create at execution time: isolated worktree on branch `feature/phase2-automatic-highlights`

**Interfaces:**
- Consumes: current dirty `test/hero12-benchmark` tree and working GitNexus MCP.
- Produces: one reviewed Phase 1 closure baseline commit and one clean isolated Phase 2 worktree; no source symbol changes occur in this task.

- [ ] **Step 1: Prove closure artifacts exist and contain no placeholders**

```bash
test -f docs/benchmark-records/2026-09-28/benchmark-record.md
test -f docs/superpowers/specs/2026-09-28-phase-2-automatic-highlights-design.md
rg -n "TBD|TODO|FIXME|implement later|fill in" README.md docs/benchmark.md docs/benchmark-records/2026-09-28 docs/superpowers/specs/2026-09-28-phase-2-automatic-highlights-design.md
```

Expected: both `test` commands exit 0; `rg` emits no unresolved placeholder.

- [ ] **Step 2: Separate unrelated repository-instruction changes from product closure changes**

```bash
git status --short
git diff -- CLAUDE.md
git status --short .claude AGENTS.md
```

Expected: reviewer can classify `CLAUDE.md`, `.claude/`, and `AGENTS.md` explicitly. Do not delete them and do not include them in a product commit without user direction.

- [ ] **Step 3: Restore GitNexus before source work**

```bash
node .gitnexus/run.cjs analyze
```

Expected: index succeeds and GitNexus MCP reconnects. If `.gitnexus/run.cjs` is unavailable, run `npx gitnexus analyze`. Stop implementation if MCP still reports `CONNECT_TIMEOUT`.

- [ ] **Step 4: Run closure quality gate**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -v
uv build
uv run video-editor --help
git diff --check
```

Expected: all commands pass; current recorded baseline is 216 passing tests before Phase 2 tests are added.

- [ ] **Step 5: Commit closure only after explicit user approval and mandatory change detection**

```text
GitNexus call: detect_changes({scope: "compare", base_ref: "main"})
Expected affected scope: documentation, benchmark evidence, and approved design only.
```

```bash
git add README.md docs/benchmark.md docs/benchmark-records/2026-09-28 docs/superpowers/specs/2026-09-28-phase-2-automatic-highlights-design.md notes.md task_plan.md
git commit -m "docs: close phase 1 and define automatic highlights"
```

Expected: commit excludes unrelated instruction/tooling files unless user separately approves them.

- [ ] **Step 6: Create clean isolated implementation worktree**

Use `superpowers:using-git-worktrees` at execution time, based on closure commit, and create branch `feature/phase2-automatic-highlights`. Verify `git status --short` is empty before Task 1.

## Task 1: Configuration, Secret Boundary, and Stable Errors

**Files:**
- Modify: `pyproject.toml:1-20`
- Modify: `uv.lock`
- Modify: `src/video_editor/config.py:13-91`
- Modify: `src/video_editor/errors.py:6-26`
- Modify: `src/video_editor/workflow.py:58-66` only for new exit-code mapping
- Modify: `config.example.toml:10-16`
- Modify: `.env.example:1-2`
- Test: `tests/unit/test_config.py`
- Test: `tests/unit/test_workflow.py`

**Interfaces:**
- Consumes: TOML, `Mapping[str, str]`, and current `AppConfig`/`VideoEditorError` callers.
- Produces: `GeminiSettings`, `HighlightSettings`, `CropSettings`, `load_gemini_api_key(env) -> str`, `VideoEditorError.safe_details`, and exit codes 17–19.

- [ ] **Step 1: Run GitNexus impact gates**

```text
impact({target: "AppConfig", direction: "upstream"})
impact({target: "resolve_config", direction: "upstream"})
impact({target: "ErrorCategory", direction: "upstream"})
impact({target: "VideoEditorError", direction: "upstream"})
impact({target: "error_exit_code", direction: "upstream"})
```

Record callers/processes/risk. Warn and wait if any result is HIGH or CRITICAL.

- [ ] **Step 2: Write failing config and secret tests**

```python
def test_phase2_config_keeps_api_key_out_of_app_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value")
    path = write_config(tmp_path, cloud_enabled=True, gemini_enabled=True)
    config = resolve_config(path)
    assert config.gemini.model == "gemini-2.5-flash"
    assert config.gemini.max_cost_per_source_hour_usd == Decimal("1.00")
    assert "secret-value" not in repr(config)
    assert load_gemini_api_key(os.environ) == "secret-value"


def test_phase2_requires_key_only_when_cloud_execution_starts(tmp_path):
    config = resolve_config(write_config(tmp_path, cloud_enabled=True, gemini_enabled=True))
    with pytest.raises(VideoEditorError, match="GEMINI_API_KEY") as caught:
        load_gemini_api_key({})
    assert caught.value.category == ErrorCategory.CONFIGURATION
    assert "secret" not in str(caught.value).lower()
```

Also add parameterized failures for model not exactly `gemini-2.5-flash`, broad FPS not `0.5`, candidate FPS outside 2–5, cost cap above `1.00`, short cap outside 0–5, and weakened crop limits.

- [ ] **Step 3: Verify RED**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_workflow.py -v`

Expected: FAIL because new settings, loader, categories, and codes do not exist.

- [ ] **Step 4: Add strict immutable settings**

```python
@dataclass(frozen=True)
class GeminiSettings:
    enabled: bool = False
    model: str = "gemini-2.5-flash"
    broad_fps: Decimal = Decimal("0.5")
    candidate_min_fps: int = 2
    candidate_max_fps: int = 5
    max_cost_per_source_hour_usd: Decimal = Decimal("1.00")
    chunk_target_seconds: int = 720
    chunk_min_seconds: int = 600
    chunk_max_seconds: int = 900
    upload_poll_seconds: int = 5
    max_transient_attempts: int = 3
    max_schema_repair_attempts: int = 1


@dataclass(frozen=True)
class HighlightSettings:
    max_short_count: int = 5
    long_max_seconds: int = 1800
    short_max_seconds: int = 180
    cross_short_overlap_ratio: Decimal = Decimal("0.10")


@dataclass(frozen=True)
class CropSettings:
    max_velocity_widths_per_second: Decimal = Decimal("0.25")
    max_acceleration_widths_per_second_squared: Decimal = Decimal("0.50")
    safe_margin_ratio: Decimal = Decimal("0.05")
    minimum_subject_retention_ratio: Decimal = Decimal("0.95")
    max_fallback_hold_seconds: Decimal = Decimal("2")
```

Extend `AppConfig` with these values. Parse `[gemini]`, `[highlights]`, and `[crop]`. Retain `cloud_enabled = false` as valid Phase 1 mode; require `[gemini].enabled = true` when cloud mode is enabled. `load_gemini_api_key` reads but never stores key.

- [ ] **Step 5: Add safe errors and dependencies**

```python
class ErrorCategory(StrEnum):
    # existing values remain unchanged
    ANALYSIS = "analysis"
    PROVIDER = "provider"
    BUDGET = "budget"


class VideoEditorError(Exception):
    def __init__(self, category, message, *, code=None, safe_details=None, interrupted=False):
        super().__init__(message)
        self.category = category
        self.code = code
        self.safe_details = dict(safe_details or {})
        self.interrupted = interrupted
```

Map `ANALYSIS`, `PROVIDER`, and `BUDGET` to CLI exits 17, 18, and 19. Add `google-genai>=1.0,<2.0` and `opencv-python-headless>=4.10,<5` with `uv add`, allowing `uv.lock` to pin exact versions.

- [ ] **Step 6: Document configuration without a credential value**

```toml
[gemini]
enabled = false
model = "gemini-2.5-flash"
broad_fps = "0.5"
candidate_min_fps = 2
candidate_max_fps = 5
max_cost_per_source_hour_usd = "1.00"
```

`.env.example` contains only `GEMINI_API_KEY=` and states that the key is runtime-only.

- [ ] **Step 7: Verify GREEN and secret scan**

```bash
uv run pytest tests/unit/test_config.py tests/unit/test_workflow.py -v
uv run mypy src/video_editor/config.py src/video_editor/errors.py
rg -n "secret-value|GEMINI_API_KEY=.*[^=]" src tests config.example.toml .env.example
```

Expected: tests/types pass; secret scan finds test fixture text only, never persisted production values.

- [ ] **Step 8: Detect changes and commit**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

```bash
git add pyproject.toml uv.lock src/video_editor/config.py src/video_editor/errors.py src/video_editor/workflow.py config.example.toml .env.example tests/unit/test_config.py tests/unit/test_workflow.py
git commit -m "feat: add phase 2 configuration contracts"
```

## Task 2: Ordered Migrations and Atomic Cloud-Budget Ledger

**Files:**
- Create: `src/video_editor/persistence/migrations.py`
- Modify: `src/video_editor/persistence/database.py:56-184,530-580`
- Create: `src/video_editor/analysis/models.py`
- Create: `src/video_editor/analysis/budget.py`
- Create: `src/video_editor/analysis/pricing.py`
- Test: `tests/unit/test_database.py`
- Create: `tests/unit/test_budget.py`
- Create: `tests/unit/test_analysis_models.py`

**Interfaces:**
- Consumes: schema version 1 databases, exact `Decimal` USD amounts, provider-neutral Pydantic records.
- Produces: `run_migrations(connection)`, `JobStore.save_proxy_manifest`, `save_analysis_result`, `find_analysis_result`, `initialize_budget`, `reserve_request`, `settle_request`, `release_confirmed_nonbillable`, `budget_state`, and `reconcile_unknown_request`.

- [ ] **Step 1: Run impact gates**

```text
impact({target: "JobStore", direction: "upstream"})
impact({target: "JobStore._create_schema", direction: "upstream"})
impact({target: "JobStore._transaction", direction: "upstream"})
impact({target: "JobStore.get_job", direction: "upstream"})
```

Stop for HIGH/CRITICAL warning review.

- [ ] **Step 2: Define strict persistence models with failing tests**

```python
class SourceRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1)
    start: Decimal = Field(ge=0)
    end: Decimal = Field(gt=0)


class RequestReservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    job_id: str
    cache_key: str
    mode: Literal["broad", "candidate"]
    maximum_cost_usd: Decimal = Field(gt=0)


class BudgetState(BaseModel):
    limit_usd: Decimal
    spent_usd: Decimal
    reserved_usd: Decimal
    remaining_usd: Decimal
```

Tests reject non-finite decimals, reversed intervals, negative cost, and extra fields.

- [ ] **Step 3: Write migration and concurrency tests**

```python
def test_version_one_database_migrates_without_losing_jobs(tmp_path):
    create_version_one_fixture(tmp_path / "state.db")
    with JobStore(tmp_path / "state.db") as store:
        assert store.migration_version() == LATEST_MIGRATION_VERSION
        assert store.get_job("existing")["status"] == "completed"


def test_two_connections_cannot_overreserve(tmp_path):
    first = open_store(tmp_path / "state.db")
    second = open_store(tmp_path / "state.db")
    first.initialize_budget("job", Decimal("1.00"))
    barrier = threading.Barrier(2)
    results = concurrently_reserve(first, second, barrier, Decimal("0.60"))
    assert sorted(results) == ["budget_exhausted", "reserved"]
    assert first.budget_state("job").reserved_usd == Decimal("0.60")


def test_unknown_billing_keeps_reservation_after_restart(tmp_path):
    request_id = reserve_fixture(tmp_path, maximum="0.25")
    mark_dispatch_unknown(tmp_path, request_id)
    with JobStore(tmp_path / "state.db") as store:
        assert store.budget_state("job").reserved_usd == Decimal("0.25")
```

- [ ] **Step 4: Verify RED**

Run: `uv run pytest tests/unit/test_analysis_models.py tests/unit/test_database.py tests/unit/test_budget.py -v`

Expected: FAIL on missing migrations, tables, and methods.

- [ ] **Step 5: Add ordered migrations**

`migrations.py` defines immutable `Migration(version: int, statements: tuple[str, ...])` values. Migration 2 creates:

```sql
CREATE TABLE proxy_manifests (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    manifest_id TEXT NOT NULL UNIQUE,
    digest TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE analysis_chunks (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL UNIQUE,
    manifest_id TEXT NOT NULL REFERENCES proxy_manifests(manifest_id),
    source_id TEXT NOT NULL,
    source_start TEXT NOT NULL,
    source_end TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE TABLE analysis_requests (
    request_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    cache_key TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('broad', 'candidate')),
    status TEXT NOT NULL CHECK(status IN ('reserved', 'dispatched', 'completed', 'released', 'billing_unknown')),
    maximum_cost_microusd INTEGER NOT NULL CHECK(maximum_cost_microusd >= 0),
    actual_cost_microusd INTEGER CHECK(actual_cost_microusd >= 0),
    data_json TEXT NOT NULL,
    UNIQUE(job_id, cache_key)
);
CREATE TABLE analysis_results (
    result_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    cache_key TEXT NOT NULL,
    chunk_id TEXT NOT NULL REFERENCES analysis_chunks(chunk_id),
    mode TEXT NOT NULL CHECK(mode IN ('broad', 'candidate')),
    validated INTEGER NOT NULL CHECK(validated IN (0, 1)),
    data_json TEXT NOT NULL,
    UNIQUE(job_id, cache_key)
);
CREATE TABLE budget_accounts (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    limit_microusd INTEGER NOT NULL CHECK(limit_microusd >= 0),
    spent_microusd INTEGER NOT NULL DEFAULT 0 CHECK(spent_microusd >= 0),
    reserved_microusd INTEGER NOT NULL DEFAULT 0 CHECK(reserved_microusd >= 0),
    CHECK(spent_microusd + reserved_microusd <= limit_microusd)
);
CREATE TABLE candidates (
    candidate_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE TABLE crop_tracks (
    track_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    data_json TEXT NOT NULL
);
CREATE TABLE plan_outputs (
    plan_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    data_json TEXT NOT NULL,
    UNIQUE(job_id, filename)
);
```

Run each migration in one transaction and update `schema_metadata.migration_version` only after every statement succeeds.

- [ ] **Step 6: Implement exact-money ledger**

Convert budget limits to integer micro-USD with floor and request maxima with ceiling. `reserve_request` uses `BEGIN IMMEDIATE`, checks reusable validated result first, then enforces:

```python
if spent + reserved + request_maximum > limit:
    raise VideoEditorError(ErrorCategory.BUDGET, "request exceeds job budget", code="budget_exhausted")
```

Settlement atomically subtracts full reservation and adds actual cost. Confirmed non-billable failure releases it. Unknown billing changes request status to `billing_unknown` without changing `reserved_microusd`. Reconciliation accepts only a provider-confirmed actual cost or confirmed non-billable outcome.

- [ ] **Step 7: Add pricing and preflight facade**

```python
@dataclass(frozen=True)
class ModelPricing:
    model: str
    media_input_usd_per_million_tokens: Decimal
    text_input_usd_per_million_tokens: Decimal
    output_usd_per_million_tokens: Decimal
    source_url: str
    effective_date: date


def maximum_request_cost(
    pricing: ModelPricing,
    media_tokens: int,
    prompt_tokens: int,
    output_tokens: int,
) -> Decimal:
    if min(media_tokens, prompt_tokens, output_tokens) < 0:
        raise ValueError("token maxima must be non-negative")
    million = Decimal(1_000_000)
    return (
        Decimal(media_tokens) * pricing.media_input_usd_per_million_tokens
        + Decimal(prompt_tokens) * pricing.text_input_usd_per_million_tokens
        + Decimal(output_tokens) * pricing.output_usd_per_million_tokens
    ) / million


def job_budget(
    source_seconds: Decimal,
    cap_per_hour: Decimal = Decimal("1.00"),
) -> Decimal:
    if source_seconds < 0 or cap_per_hour < 0:
        raise ValueError("duration and cap must be non-negative")
    return source_seconds / Decimal(3600) * cap_per_hour


def reserve_all_uncached_broad_requests(
    store: JobStore,
    job_id: str,
    requests: Sequence[RequestReservation],
) -> tuple[str, ...]:
    return store.reserve_request_batch(job_id, requests)
```

At execution, verify current official Gemini Developer API pricing for exact model `gemini-2.5-flash`, encode it as the sole production catalog entry with official source URL/effective date, and test arithmetic against that encoded entry. Absence or ambiguity raises `pricing_unknown`; no request is sent.

- [ ] **Step 8: Verify GREEN**

```bash
uv run pytest tests/unit/test_analysis_models.py tests/unit/test_database.py tests/unit/test_budget.py -v
uv run mypy src/video_editor/persistence src/video_editor/analysis/models.py src/video_editor/analysis/budget.py src/video_editor/analysis/pricing.py
```

Expected: migration, concurrency, restart, release, settlement, overflow, and unknown-pricing tests pass.

- [ ] **Step 9: Detect changes and commit**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

```bash
git add src/video_editor/persistence src/video_editor/analysis tests/unit/test_database.py tests/unit/test_budget.py tests/unit/test_analysis_models.py
git commit -m "feat: add analysis persistence and budget ledger"
```

## Task 3: Edit-Plan v2 and Collision-Proof Output Identity

**Files:**
- Modify: `src/video_editor/models/edit_plan.py:21-322`
- Create: `schemas/edit-plan-v2.json`
- Modify: `tests/unit/test_edit_plan.py`
- Create: `tests/unit/test_edit_plan_v2.py`

**Interfaces:**
- Consumes: existing v1 documents and provider-neutral evidence IDs.
- Produces: `EditPlanV2`, `EditPlanDocument = EditPlan | EditPlanV2`, `load_plan`, `write_plan`, `timeline_duration`, and `validate_plan_set(plans, policy)`.

- [ ] **Step 1: Run impact gates**

```text
impact({target: "EditPlan", direction: "upstream"})
impact({target: "load_plan", direction: "upstream"})
impact({target: "write_plan", direction: "upstream"})
impact({target: "timeline_duration", direction: "upstream"})
```

Report v1 callers and process risks before editing.

- [ ] **Step 2: Write v1 compatibility and v2 schema RED tests**

```python
def test_v1_checked_in_schema_and_round_trip_remain_unchanged():
    assert json.loads(V1_SCHEMA.read_text()) == EditPlan.model_json_schema()
    assert load_plan(write_fixture(valid_v1_data())).schema_version == 1


def test_v2_output_identity_and_caps():
    long = EditPlanV2.model_validate(v2_plan(kind="long", filename="long.mp4", duration=1800))
    assert timeline_duration(long) == Decimal("1800")
    with pytest.raises(ValidationError, match="180 seconds"):
        EditPlanV2.model_validate(v2_plan(kind="short", filename="short-01.mp4", duration=181))


def test_plan_set_rejects_filename_collision_and_cross_short_duplicates():
    plans = [short_plan("short-01.mp4", "group-a", 0, 10), short_plan("short-01.mp4", "group-b", 20, 30)]
    with pytest.raises(ValueError, match="filename"):
        validate_plan_set(plans, PlanSetPolicy(max_shorts=5))
```

Add relation/type matrix tests, overlap ratio boundary at exactly 10%, semantic-group reuse, anchor exception used in two versus three shorts, plan IDs, evidence references, tracked-crop identity, and schema/model parity.

- [ ] **Step 3: Verify RED**

Run: `uv run pytest tests/unit/test_edit_plan.py tests/unit/test_edit_plan_v2.py -v`

Expected: v1 tests pass and v2 tests fail on missing symbols.

- [ ] **Step 4: Add v2 models without changing v1 class schema**

```python
class CropKeyframe(_PlanModel):
    time: Decimal
    center_x: Decimal = Field(ge=0, le=1)
    center_y: Decimal = Field(ge=0, le=1)
    subject_box_id: str | None = None
    fallback: Literal["tracked", "hold", "ease_center", "static"]


class FramingV2(_PlanModel):
    mode: Literal["center_crop", "fit_background", "tracked_crop"]
    background: str | None = None
    track_id: str | None = None
    keyframes: list[CropKeyframe] = Field(default_factory=list)


class TransitionV2(_PlanModel):
    from_clip: int
    to_clip: int
    kind: Literal["cut", "dissolve", "fade", "fade_black"]
    duration: Decimal
    relation: Literal["continuous_action", "matched_motion", "same_event", "same_place_time_shift", "chapter_boundary", "time_jump", "location_change", "story_open_close"]
    reason: str
    confidence: Decimal = Field(ge=0, le=1)
    audio_policy: Literal["cut", "crossfade", "fade_out_in"]


class OutputSpecV2(_PlanModel):
    plan_id: str
    filename: str
    kind: Literal["short", "long"]
    width: int
    height: int
    frame_rate: Decimal
    codec: Literal["libx264"]
    audio: Literal["source", "silence", "none"] = "source"
    theme_summary: str | None = None
```

`TimelineClipV2` adds overall score, typed score breakdown, analysis reference, source/proxy mapping reference, dedup group, chapter/event ID, vertical suitability, confidence, planner version, and analysis version. `EditPlanV2.schema_version` is `Literal[2]`.

- [ ] **Step 5: Implement plan and plan-set semantic validation**

Long allows duration `<= 1800`; short allows `<= 180`; output dimensions must be 1920×1080 for long and 1080×1920 for short; filename must be `long.mp4` or `short-NN.mp4`; all transition pairs obey approved mapping. `validate_plan_set` checks one long maximum, short count, unique IDs/filenames, pairwise temporal overlap, semantic groups, and bounded recorded anchor exceptions.

- [ ] **Step 6: Generate and validate v2 schema**

```bash
uv run python -c 'import json; from pathlib import Path; from video_editor.models.edit_plan import EditPlanV2; Path("schemas/edit-plan-v2.json").write_text(json.dumps(EditPlanV2.model_json_schema(), indent=2, sort_keys=True) + "\n")'
uv run pytest tests/unit/test_edit_plan.py tests/unit/test_edit_plan_v2.py -v
```

Expected: v1 schema byte-equivalent JSON content still matches v1 model; v2 Draft 2020-12 schema is valid and matches model.

- [ ] **Step 7: Detect changes and commit**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

```bash
git add src/video_editor/models/edit_plan.py schemas/edit-plan-v2.json tests/unit/test_edit_plan.py tests/unit/test_edit_plan_v2.py
git commit -m "feat: add version two edit plans"
```

## Task 4: Registered Cloud Proxy Chunks

**Files:**
- Create: `src/video_editor/analysis/proxy_chunks.py`
- Modify: `src/video_editor/analysis/models.py`
- Modify: `src/video_editor/persistence/database.py`
- Create: `tests/unit/test_proxy_chunks.py`
- Modify: `tests/fixtures.py`

**Interfaces:**
- Consumes: Phase 1 proxy/source mapping, safe local scene boundaries, configured generated roots, and job/source identity.
- Produces: `CloudProxySettings`, `plan_chunk_ranges`, `create_cloud_proxy_chunk`, `ProxyManifest`, `register_proxy_manifest`, and `validate_upload_candidate`.

- [ ] **Step 1: Run impact gates**

```text
impact({target: "ProxyMapping", direction: "upstream"})
impact({target: "create_analysis_media", direction: "upstream"})
impact({target: "JobStore.save_artifact", direction: "upstream"})
```

- [ ] **Step 2: Write chunk/mapping/provenance RED tests**

```python
def test_chunk_ranges_cover_source_once_and_split_near_safe_boundaries():
    ranges = plan_chunk_ranges(Decimal("2000"), [Decimal("710"), Decimal("1430")], settings)
    assert ranges == [(D("0"), D("710")), (D("710"), D("1430")), (D("1430"), D("2000"))]


def test_registered_file_replaced_after_manifest_is_rejected(tmp_path):
    manifest = create_and_register_chunk(tmp_path)
    manifest.path.write_bytes(b"replacement")
    with pytest.raises(VideoEditorError, match="digest"):
        validate_upload_candidate(manifest.manifest_id, store, config.paths)


def test_symlink_to_original_is_rejected_even_inside_cache(tmp_path):
    link = generated_root / "chunk.mp4"
    link.symlink_to(original)
    with pytest.raises(VideoEditorError, match="original"):
        validate_upload_candidate(manifest_id_for(link), store, paths)
```

Also test unregistered path, manifest job/source mismatch, path escape, input descendant, wrong dimensions/FPS/audio codec/bitrate, non-monotonic mapping, stale source identity, and digest mismatch.

- [ ] **Step 3: Verify RED**

Run: `uv run pytest tests/unit/test_proxy_chunks.py -v`

Expected: FAIL because chunk APIs do not exist.

- [ ] **Step 4: Implement deterministic 10–15 minute chunk planning**

```python
@dataclass(frozen=True)
class CloudProxySettings:
    max_width: int = 640
    fps: int = 15
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    audio_bitrate: str = "64k"
    target_seconds: Decimal = Decimal("720")
    minimum_seconds: Decimal = Decimal("600")
    maximum_seconds: Decimal = Decimal("900")
```

Choose nearest safe scene boundary inside 600–900 seconds around each 720-second target. If none exists, split at 720 seconds. Never cross source identity boundaries. Union of mapped ranges must exactly equal `[0, source_duration]` without gaps or duplicate interiors.

- [ ] **Step 5: Generate, probe, hash, and register chunks**

Use shell-free FFmpeg args with `-ss`, `-t`, scale to max width 640, 15 FPS H.264, mono AAC at 64 kbps, and MP4. Write `.partial`, ffprobe video/audio/duration, SHA-256 final bytes, rename on same volume, then persist manifest containing job ID, source fingerprint/identity, source and proxy ranges, mapping version, digest, generated-root identity, dimensions, FPS, audio codec/bitrate, and path.

- [ ] **Step 6: Implement upload-time revalidation**

Resolve path and every parent, reject symlinks, prove containment under configured cache/workspace roots, prove exclusion from input root and every persisted source path, reread file size/digest, reprobe media, and compare every on-disk property to persisted manifest immediately before upload.

- [ ] **Step 7: Verify GREEN**

```bash
uv run pytest tests/unit/test_proxy_chunks.py tests/unit/test_proxies.py -v
uv run mypy src/video_editor/analysis/proxy_chunks.py
```

Expected: coverage, real FFmpeg chunk, tampering, symlink, mapping, and source-exclusion tests pass.

- [ ] **Step 8: Detect changes and commit**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

```bash
git add src/video_editor/analysis/proxy_chunks.py src/video_editor/analysis/models.py src/video_editor/persistence/database.py tests/unit/test_proxy_chunks.py tests/fixtures.py
git commit -m "feat: create registered cloud proxy chunks"
```

## Task 5: Deterministic Local Segmentation

**Files:**
- Create: `src/video_editor/analysis/segmentation.py`
- Modify: `src/video_editor/analysis/models.py`
- Create: `tests/unit/test_segmentation.py`
- Modify: `tests/fixtures.py`

**Interfaces:**
- Consumes: Phase 1 muted proxy, optional mono PCM WAV, and `ProxyMapping`.
- Produces: `SegmentationSettings`, `LocalSegmentation`, `segment_media(proxy, audio, mapping, settings)`, and canonical JSON evidence.

- [ ] **Step 1: Write RED tests using controlled fixtures**

```python
def test_segmentation_maps_proxy_ranges_to_source_time(tmp_path):
    result = segment_media(proxy, wav, offset_mapping(source_start="100", proxy_start="0"), settings)
    assert result.scenes[0].source_range.start == Decimal("100")
    assert result.scenes[-1].source_range.end == Decimal("104")


def test_speech_presence_never_contains_transcript_text(tmp_path):
    result = segment_media(proxy, wav, mapping, settings)
    payload = result.model_dump(mode="json")
    assert "transcript" not in json.dumps(payload).lower()
```

Create fixtures with one hard visual cut, one silence-to-tone transition, moving rectangle, blurred segment, overexposed frames, and black obstruction. Assert boundary tolerance, speech-presence/silence ranges, transient location, relative motion ordering, and penalty ordering.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/unit/test_segmentation.py -v`

Expected: FAIL on missing segmentation module.

- [ ] **Step 3: Implement local feature extraction**

Use OpenCV frame decoding at deterministic sample timestamps. Compute histogram discontinuity for shot boundaries, Farnebäck optical-flow magnitude/continuity for motion, Laplacian variance for blur, luminance clipping for exposure, global dark/flat coverage for obstruction, and inter-frame affine residual for accidental shake. Use Python `wave` plus fixed windows for RMS energy, silence/speech presence, and transient peaks. Normalize every score to `[0, 1]` with versioned thresholds in `SegmentationSettings`.

- [ ] **Step 4: Generate deterministic candidate windows and canonical artifact**

Merge nearby local peaks only when scene boundaries allow it, preserve lead-in/resolution, map proxy time to source time exactly with `Decimal`, and include settings hash, implementation version, source identity, and evidence IDs. Run twice and assert byte-identical sorted JSON.

- [ ] **Step 5: Verify GREEN**

```bash
uv run pytest tests/unit/test_segmentation.py -v
uv run mypy src/video_editor/analysis/segmentation.py
```

- [ ] **Step 6: Detect changes and commit**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

```bash
git add src/video_editor/analysis/models.py src/video_editor/analysis/segmentation.py tests/unit/test_segmentation.py tests/fixtures.py
git commit -m "feat: add deterministic local segmentation"
```

## Task 6: Gemini Files and Structured-Response Adapter

**Files:**
- Create: `src/video_editor/analysis/gemini.py`
- Modify: `src/video_editor/analysis/models.py`
- Create: `tests/unit/test_gemini.py`
- Create: `tests/fixtures/gemini/broad-response.json`
- Create: `tests/fixtures/gemini/refinement-response.json`
- Create: `tests/fixtures/gemini/malformed-response.json`

**Interfaces:**
- Consumes: validated `ProxyManifest`, API key string held only in adapter memory, budget reservation ID, mode/FPS/offsets, and strict response model.
- Produces: `AnalysisProvider` protocol, `GeminiAdapter.upload`, `wait_until_active`, `broad_scan`, `refine_candidate`, `delete_upload`, `ProviderUsage`, and normalized validated responses.

- [ ] **Step 1: Write SDK-shape and retry RED tests with fake client**

```python
def test_broad_request_uses_static_video_metadata_and_schema(fake_client):
    adapter.broad_scan(upload, chunk, prompt_version="broad-v1")
    part = fake_client.last_contents[0]
    assert part.video_metadata.fps == 0.5
    assert part.video_metadata.start_offset == "0s"
    assert fake_client.last_config.response_mime_type == "application/json"
    assert fake_client.last_config.response_schema is BroadScanResponse


def test_authentication_failure_is_not_retried(
    fake_client,
    broad_chunk,
    uploaded_file,
):
    fake_client.fail_with(AuthenticationError("bad key"))
    adapter = GeminiAdapter(fake_client, retry_policy=RetryPolicy(max_attempts=3))
    with pytest.raises(VideoEditorError) as caught:
        adapter.broad_scan(
            uploaded_file,
            broad_chunk,
            prompt_version="broad-v1",
        )
    assert caught.value.code == "provider_authentication"
    assert fake_client.calls == 1
```

Add tests for upload config MIME type, PROCESSING→ACTIVE polling, FAILED terminal state, 429/5xx/network retry, invalid request no retry, one bounded schema repair, expiry reupload, candidate FPS 2/3/5, duration-string offsets, request IDs/usage persistence payload, and key redaction from exceptions/repr.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/unit/test_gemini.py -v`

Expected: FAIL because adapter is absent.

- [ ] **Step 3: Implement provider protocol and Files API lifecycle**

```python
class AnalysisProvider(Protocol):
    def upload(self, manifest: ProxyManifest) -> UploadedFile:
        raise NotImplementedError

    def broad_scan(
        self,
        upload: UploadedFile,
        chunk: AnalysisChunk,
        *,
        prompt_version: str,
    ) -> ProviderResult[BroadScanResponse]:
        raise NotImplementedError

    def refine_candidate(
        self,
        upload: UploadedFile,
        candidate: CandidateWindow,
        fps: int,
        *,
        prompt_version: str,
    ) -> ProviderResult[CandidateRefinementResponse]:
        raise NotImplementedError

    def delete_upload(self, upload: UploadedFile) -> None:
        raise NotImplementedError
```

Instantiate `genai.Client(api_key=key)`. Upload with `client.files.upload(file=path, config={"mime_type": "video/mp4"})`, poll `client.files.get(name=...)` until ACTIVE, and delete with `client.files.delete(name=...)`. Never log client/key objects.

- [ ] **Step 4: Build strict static-video requests**

Construct the part from concrete values:

```python
video_part = types.Part(
    file_data=types.FileData(
        file_uri=upload.uri,
        mime_type=upload.mime_type,
    ),
    video_metadata=types.VideoMetadata(
        start_offset=f"{format(chunk.proxy_start, 'f')}s",
        end_offset=f"{format(chunk.proxy_end, 'f')}s",
        fps=float(request_fps),
    ),
)
config = types.GenerateContentConfig(
    response_mime_type="application/json",
    response_schema=response_model,
    max_output_tokens=max_output_tokens,
)
```

Use `response.parsed`; validate chunk identity and every timestamp against requested offsets.

- [ ] **Step 5: Enforce semantic boundary**

`BroadScanResponse` has speech-presence ranges but no transcript/utterance/meaning fields. `CandidateRefinementResponse` may contain a bounded `speech_meaning_summary` only for its requested candidate interval. Both reject people identity fields and extra keys.

- [ ] **Step 6: Verify GREEN offline**

```bash
uv run pytest tests/unit/test_gemini.py -v
uv run mypy src/video_editor/analysis/gemini.py
```

Expected: all tests pass without DNS/socket access.

- [ ] **Step 7: Add explicit live-test marker without running it by default**

```python
@pytest.mark.gemini_live
@pytest.mark.skipif(
    not os.getenv("RUN_GEMINI_LIVE_TESTS"),
    reason="explicit opt-in required",
)
def test_live_one_tiny_registered_proxy(
    tiny_registered_manifest: ProxyManifest,
    live_budget_store: JobStore,
):
    maximum = estimate_broad_request_maximum(tiny_registered_manifest)
    assert maximum <= Decimal("0.01")
    reservation = live_budget_store.reserve_request(
        RequestReservation(
            request_id="live-contract",
            job_id=tiny_registered_manifest.job_id,
            cache_key="live-contract-v1",
            mode="broad",
            maximum_cost_usd=maximum,
        )
    )
    result = live_adapter().broad_scan(
        live_adapter().upload(tiny_registered_manifest),
        chunk_for(tiny_registered_manifest),
        prompt_version="broad-v1",
    )
    assert result.response.chunk_id == tiny_registered_manifest.chunk_id
    assert result.usage.actual_cost_usd <= maximum
    live_budget_store.settle_request(reservation, result.usage.actual_cost_usd)
```

Register marker in pytest config. Test must reserve no more than USD 0.01 and skip before upload if conservative maximum exceeds that amount.

- [ ] **Step 8: Detect changes and commit**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

```bash
git add src/video_editor/analysis tests/unit/test_gemini.py tests/fixtures/gemini pyproject.toml
git commit -m "feat: add schema constrained gemini adapter"
```

## Task 7: Analyze Orchestration and 100% Coverage Gate

**Files:**
- Create: `src/video_editor/analysis/orchestrator.py`
- Modify: `src/video_editor/analysis/budget.py`
- Modify: `src/video_editor/persistence/database.py`
- Create: `tests/unit/test_analysis_orchestrator.py`

**Interfaces:**
- Consumes: registered chunks, local segmentation, provider adapter, cache/result store, pricing, and budget ledger.
- Produces: `analysis_cache_key`, `coverage_report`, `run_broad_analysis`, `run_candidate_refinement`, `AnalysisOutcome`, and exact missing source ranges.

- [ ] **Step 1: Write cache and coverage RED tests**

```python
def test_subsecond_gap_blocks_downstream():
    expected = [SourceRange(source_id="s", start=D("0"), end=D("10"))]
    validated = [SourceRange(source_id="s", start=D("0"), end=D("9.999"))]
    report = coverage_report(expected, validated, exclusions=[])
    assert report.ratio < Decimal("1")
    assert report.missing == [SourceRange(source_id="s", start=D("9.999"), end=D("10"))]


def test_invalid_cached_result_does_not_count_as_coverage():
    outcome = run_broad_analysis(context_with_cache(validated=False))
    assert outcome.state == "analysis_incomplete"
    assert provider.calls == 1
```

Also test overlapping ranges union correctly, only inspection-persisted unsupported/unreadable exclusions reduce denominator, cache key changes for every specified field, all broad reservations happen before first upload, candidate work cannot begin before full coverage, budget exhaustion emits no candidate calls, and candidate speech meaning remains interval-bounded.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/unit/test_analysis_orchestrator.py -v`

Expected: FAIL on missing orchestrator.

- [ ] **Step 3: Implement canonical cache key**

Hash canonical JSON containing source identity/fingerprint, proxy digest, mapping version, chunk source range, mode, FPS, media resolution, provider, exact model, prompt version, response schema version, and analysis implementation version.

- [ ] **Step 4: Implement interval-union coverage**

Group by source, intersect validated broad results with usable ranges, merge touching/overlapping intervals, subtract only persisted inspection exclusions, and calculate exact `Decimal` seconds. Coverage passes only when missing is empty and numerator equals denominator; do not round to 100%.

- [ ] **Step 5: Implement fail-closed orchestration**

Before any provider call, compute every uncached broad request maximum and atomically reserve the whole set. For each request: revalidate upload provenance, dispatch, validate response, settle actual usage, persist normalized result. A provider or budget stop returns `budget_exhausted` or `analysis_incomplete`, retains exact missing ranges, and exposes no refinement candidates to later stages.

After coverage passes, choose candidate FPS by local motion (`2` static/conversation, `3` ordinary movement, `5` fast action), reserve each bounded refinement, and persist speech meaning only with candidate ID/range.

- [ ] **Step 6: Verify GREEN**

```bash
uv run pytest tests/unit/test_analysis_orchestrator.py tests/unit/test_budget.py tests/unit/test_gemini.py -v
uv run mypy src/video_editor/analysis/orchestrator.py
```

- [ ] **Step 7: Detect changes and commit**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

```bash
git add src/video_editor/analysis/orchestrator.py src/video_editor/analysis/budget.py src/video_editor/persistence/database.py tests/unit/test_analysis_orchestrator.py
git commit -m "feat: enforce complete cloud analysis coverage"
```

## Task 8: Auditable Ranking, Deduplication, and Diversity

**Files:**
- Create: `src/video_editor/analysis/ranking.py`
- Modify: `src/video_editor/analysis/models.py`
- Create: `tests/unit/test_ranking.py`

**Interfaces:**
- Consumes: validated normalized broad/refinement evidence plus local technical evidence.
- Produces: `RankingSettings`, `ScoreBreakdown`, `RankedCandidate`, `Rejection`, `rank_candidates`, and stable semantic dedup groups.

- [ ] **Step 1: Write score, penalty, and determinism RED tests**

```python
def test_score_exposes_every_dimension_and_penalty():
    ranked = rank_candidates([candidate_fixture()], RankingSettings())[0]
    assert set(ranked.score.positive) == {"action", "scenic", "human", "story", "technical", "novelty", "completeness", "long_story", "short", "vertical"}
    assert set(ranked.score.penalties) == {"blur_exposure", "shake_obstruction", "incomplete", "weak_boundary", "repetition", "overlap"}
    assert ranked.score.total == sum(ranked.score.positive.values()) - sum(ranked.score.penalties.values())


def test_equal_scores_use_stable_source_time_candidate_order():
    first = rank_candidates(shuffled_equal_candidates(seed=1), settings)
    second = rank_candidates(shuffled_equal_candidates(seed=2), settings)
    assert [c.candidate_id for c in first] == [c.candidate_id for c in second]
```

Add tests for temporal overlap, visual similarity, semantic similarity, event identity, machine-readable rejection reason, source/category/event/location quotas, and no one category dominating when alternatives clear threshold.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/unit/test_ranking.py -v`

Expected: FAIL on missing ranking module.

- [ ] **Step 3: Implement score fusion**

Use versioned decimal weights whose sum is explicit, clamp normalized inputs, preserve raw values/evidence IDs, calculate each penalty separately, and never accept an opaque provider total. Technical hard failures set `eligible_long=False` and `eligible_short=False` with reason codes.

- [ ] **Step 4: Implement deduplication and diversity selector**

Build dedup edges from temporal overlap, perceptual-frame similarity, semantic embedding/feature similarity supplied in normalized records, and exact event identity. Connected components receive stable IDs based on sorted candidate IDs. Select in descending `(total, confidence, completeness, -source_start, candidate_id)` order while enforcing configurable category/source/event/location shares. Persist every rejection reason and winning candidate reference.

- [ ] **Step 5: Verify GREEN**

```bash
uv run pytest tests/unit/test_ranking.py -v
uv run mypy src/video_editor/analysis/ranking.py
```

- [ ] **Step 6: Detect changes and commit**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

```bash
git add src/video_editor/analysis/models.py src/video_editor/analysis/ranking.py tests/unit/test_ranking.py
git commit -m "feat: rank and deduplicate highlight candidates"
```

## Task 9: Local Subject Tracking and Validated Crop Paths

**Files:**
- Create: `src/video_editor/analysis/tracking.py`
- Modify: `src/video_editor/analysis/models.py`
- Create: `tests/unit/test_tracking.py`
- Modify: `tests/fixtures.py`

**Interfaces:**
- Consumes: proxy frames, source/proxy mapping, candidate range, Gemini subject-priority/approximate boxes, and `CropSettings`.
- Produces: `SubjectObservation`, `CropKeyframe`, `CropTrack`, `track_subject`, `smooth_crop_track`, `validate_crop_track`, and best-static fallback.

- [ ] **Step 1: Write synthetic movement/occlusion RED tests**

```python
def test_fast_raw_track_is_smoothed_below_velocity_and_acceleration_limits():
    track = build_track(moving_rectangle_fixture(), subject_boxes, crop_settings)
    evidence = validate_crop_track(track, subject_boxes, source_size=(1920, 1080), settings=crop_settings)
    assert evidence.max_velocity <= Decimal("0.25")
    assert evidence.max_acceleration <= Decimal("0.50")


def test_occlusion_holds_then_eases_center_without_jump():
    track = build_track(occluded_subject_fixture(), subject_boxes, crop_settings)
    assert max_hold_duration(track) <= Decimal("2")
    assert track.keyframes_after_loss[0].fallback == "hold"
    assert "ease_center" in [k.fallback for k in track.keyframes_after_loss]
```

Add tests for 5% margin/95% retention, monotonic timestamps, bounds, track/source identity mismatch, conflicting boxes, empty detections, deterministic static fallback, and exclusion when neither track nor static crop passes.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/unit/test_tracking.py -v`

Expected: FAIL on missing tracking module.

- [ ] **Step 3: Implement local tracker**

Seed normalized subject regions at Gemini evidence timestamps. Detect Shi–Tomasi features inside each seed box, propagate with pyramidal Lucas–Kanade optical flow, reject outliers with forward/backward error and RANSAC affine fit, and periodically re-anchor at the next validated subject observation. Record local observed boxes; Gemini boxes alone never count as retention proof.

- [ ] **Step 4: Smooth and validate crop center**

Convert output aspect to source crop dimensions, choose centers retaining highest-priority observed boxes, apply velocity- and acceleration-limited smoothing, clamp bounds, and sample the final path at fixed intervals. On loss, hold no more than two seconds then ease toward center. If validation fails, test the best static center that maximizes safe-margin retention; otherwise mark candidate ineligible for short output.

- [ ] **Step 5: Verify GREEN**

```bash
uv run pytest tests/unit/test_tracking.py -v
uv run mypy src/video_editor/analysis/tracking.py
```

- [ ] **Step 6: Detect changes and commit**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

```bash
git add src/video_editor/analysis/models.py src/video_editor/analysis/tracking.py tests/unit/test_tracking.py tests/fixtures.py
git commit -m "feat: generate validated tracked crop paths"
```

## Task 10: Chronological Long and Distinct Multi-Short Planning

**Files:**
- Create: `src/video_editor/planning/highlight_plan.py`
- Modify: `src/video_editor/models/edit_plan.py`
- Create: `tests/unit/test_highlight_plan.py`

**Interfaces:**
- Consumes: ranked candidates, chronology groups, validated crop tracks, source records, transition evidence, `HighlightSettings`, and optional anchor policy.
- Produces: `HighlightPlanSet(long: EditPlanV2, shorts: tuple[EditPlanV2, ...])`, output plan filenames, themes, and plan-set validation evidence.

- [ ] **Step 1: Write planning RED tests**

```python
def test_long_plan_preserves_chronology_and_stops_before_weak_filler():
    plans = create_highlight_plans(ranked_fixture(), chronology, tracks, settings)
    assert source_times(plans.long) == sorted(source_times(plans.long))
    assert timeline_duration(plans.long) <= Decimal("1800")
    assert "weak" not in selected_ids(plans.long)


def test_short_count_is_zero_when_no_coherent_quality_cluster_exists():
    plans = create_highlight_plans(low_quality_fixture(), chronology, tracks, settings)
    assert plans.shorts == ()


def test_multiple_shorts_have_unique_names_and_no_forbidden_reuse():
    plans = create_highlight_plans(multi_theme_fixture(), chronology, tracks, settings)
    assert [p.output.filename for p in plans.shorts] == ["short-01.mp4", "short-02.mp4"]
    validate_plan_set([plans.long, *plans.shorts], settings.plan_set_policy())
```

Add tests for balance across four categories, complete-event lead-in/resolution, omitted weak chapters, quality-first durations, default/configured cap, 10% overlap boundary, semantic-group exclusion, one anchor in exactly two shorts, anchor rationale, vertical ineligibility, and deterministic results.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/unit/test_highlight_plan.py -v`

Expected: FAIL on missing planner.

- [ ] **Step 3: Implement chronological long planner**

Group eligible candidates by chronology chapter. Select complete, diverse moments under 1,800 seconds with lead-in/resolution, then emit clips strictly by chronology key. Drop chapters with no threshold-clearing material. Record category distribution and chapter progression; never pad toward cap.

- [ ] **Step 4: Implement automatic independent short planner**

Cluster short-eligible candidates by semantic theme/event/location/energy. Create a reel only when cluster coherence and aggregate quality meet versioned thresholds. Greedily choose highest-quality non-duplicate reel, remove forbidden overlaps/groups, and repeat up to configured cap. Assign stable `short-NN.mp4` names after final ordering and include theme summary/category distribution.

- [ ] **Step 5: Implement semantic transition selection**

```python
ALLOWED_TRANSITIONS = {
    "cut": {"continuous_action", "matched_motion", "same_event"},
    "dissolve": {"same_event", "same_place_time_shift"},
    "fade": {"chapter_boundary", "story_open_close"},
    "fade_black": {"chapter_boundary", "time_jump", "location_change"},
}
```

Choose from validated adjacent-scene compatibility. Unrelated scenes never receive `cut`. Bound durations by kind and adjacent clip lengths; include reason/confidence/audio policy; ensure important action boundaries are not hidden.

- [ ] **Step 6: Verify GREEN**

```bash
uv run pytest tests/unit/test_highlight_plan.py tests/unit/test_edit_plan_v2.py -v
uv run mypy src/video_editor/planning/highlight_plan.py
```

- [ ] **Step 7: Detect changes and commit**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

```bash
git add src/video_editor/planning/highlight_plan.py src/video_editor/models/edit_plan.py tests/unit/test_highlight_plan.py
git commit -m "feat: plan long and multi-short highlight edits"
```

## Task 11: Tracked-Crop and Semantic-Transition Rendering

**Files:**
- Modify: `src/video_editor/rendering/compiler.py:11-342`
- Modify: `src/video_editor/validation/outputs.py:13-56`
- Modify: `tests/unit/test_compiler.py`
- Modify: `tests/integration/test_render.py`
- Create: `tests/integration/test_phase2_render.py`

**Interfaces:**
- Consumes: validated v1/v2 plan documents and explicit `output.filename` for v2.
- Produces: shell-free FFmpeg graph for tracked crop, cut/dissolve/fade/fade_black and audio policy; `validate_phase2_output` returns structured quality evidence.

- [ ] **Step 1: Run impact gates**

```text
impact({target: "compile_render", direction: "upstream"})
impact({target: "_video_framing", direction: "upstream"})
impact({target: "_audio_filter", direction: "upstream"})
impact({target: "validate_output", direction: "upstream"})
```

- [ ] **Step 2: Write compiler RED tests**

```python
def test_v2_uses_explicit_output_filename(tmp_path):
    command = compile_render(v2_plan(filename="short-03.mp4"), "ffmpeg", output_dir=tmp_path)
    assert command.final_path.name == "short-03.mp4"


def test_tracked_crop_graph_interpolates_validated_keyframes():
    command = compile_render(tracked_crop_plan(), "ffmpeg")
    graph = filter_graph(command)
    assert "crop=" in graph
    assert "if(between(t" in graph
    assert "short-01.mp4" in str(command.final_path)
```

Add graph assertions for fade, fade-through-black, video overlap duration, audio crossfade/fade-out-in, plain cut only for allowed relations, and v1 graph compatibility.

- [ ] **Step 3: Verify RED**

Run: `uv run pytest tests/unit/test_compiler.py tests/integration/test_render.py tests/integration/test_phase2_render.py -v`

Expected: new tests fail; existing v1 render tests remain green.

- [ ] **Step 4: Compile tracked crops deterministically**

Translate normalized crop centers from validated keyframes into FFmpeg `crop=w:h:x:y` expressions with piecewise linear/eased interpolation over clip-local time. Escape expressions as argument data, never shell strings. Clamp final pixel coordinates and use original source timestamps after trim.

- [ ] **Step 5: Compile transitions and source audio**

Keep concat for zero-duration cut. Use `xfade=fade` plus `acrossfade` for dissolve. For `fade`, apply outgoing/incoming fades without inserting unrelated effects. For `fade_black`, fade outgoing to black, concatenate bounded black interval/faded incoming sequence, and apply `afade`/silence/crossfade according to `audio_policy`. Update expected timeline duration exactly as v2 semantics specify.

- [ ] **Step 6: Add output quality validation**

`validate_phase2_output(path, plan, crop_track_evidence)` calls existing probe checks, samples frames to detect persistent unintended pillar/letter bars, confirms crop-track ID/path identity and sampled bounds, checks source-audio stream continuity around planned boundaries, and returns warnings versus hard failures with evidence timestamps.

- [ ] **Step 7: Render real synthetic fixtures**

Render a moving-rectangle tracked crop and four two-scene transition plans. Assert dimensions, duration tolerance, subject pixel region retained in at least 95% sampled frames, audio stream present, unique filenames, and no unexpected black bars outside intended fade-black frames.

- [ ] **Step 8: Verify GREEN**

```bash
uv run pytest tests/unit/test_compiler.py tests/integration/test_render.py tests/integration/test_phase2_render.py -v
uv run mypy src/video_editor/rendering/compiler.py src/video_editor/validation/outputs.py
```

- [ ] **Step 9: Detect changes and commit**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

```bash
git add src/video_editor/rendering/compiler.py src/video_editor/validation/outputs.py tests/unit/test_compiler.py tests/integration/test_render.py tests/integration/test_phase2_render.py
git commit -m "feat: render tracked crops and semantic transitions"
```

## Task 12: Nine-Stage Workflow, Resume, Reporting, and CLI

**Files:**
- Modify: `src/video_editor/workflow.py:55-1296`
- Modify: `src/video_editor/reporting.py:9-88`
- Modify: `src/video_editor/cli.py:18-111`
- Modify: `tests/unit/test_workflow.py`
- Modify: `tests/unit/test_reporting.py`
- Modify: `tests/integration/test_cli_workflow.py`
- Create: `tests/integration/test_phase2_workflow.py`

**Interfaces:**
- Consumes: all Tasks 1–11 services plus current Phase 1 workflow.
- Produces: Phase 2 stages `inspect, proxy, segment, analyze, rank, plan, render, validate, report`, stage-specific cache versions, safe status/report payloads, and collision-proof per-output recovery.

- [ ] **Step 1: Run impact gates**

```text
impact({target: "WorkflowService", direction: "upstream"})
impact({target: "WorkflowService._execute", direction: "upstream"})
impact({target: "WorkflowService._stage_reusable", direction: "upstream"})
impact({target: "WorkflowService._render_plan", direction: "upstream"})
impact({target: "WorkflowService._ensure_render", direction: "upstream"})
impact({target: "WorkflowService._report_state", direction: "upstream"})
impact({target: "write_json_report", direction: "upstream"})
impact({target: "write_markdown_report", direction: "upstream"})
impact({target: "_invoke", direction: "upstream"})
```

This likely has broad blast radius. Report direct callers and affected CLI/end-to-end processes; stop before edits if risk is HIGH/CRITICAL until user confirms.

- [ ] **Step 2: Write workflow-gate RED tests with `FakeAnalysisProvider`**

```python
def test_phase2_stage_order_and_complete_outputs(phase2_service):
    result = phase2_service.run(input_dir)
    state = phase2_service.status(result["job_id"])
    assert list(state["stages"]) == ["inspect", "proxy", "segment", "analyze", "rank", "plan", "render", "validate", "report"]
    assert [Path(o["output"]).name for o in result["outputs"]] == ["long.mp4", "short-01.mp4", "short-02.mp4"]


def test_incomplete_broad_coverage_creates_no_plan_or_media(phase2_service):
    provider.omit_last_range = True
    with pytest.raises(VideoEditorError) as caught:
        phase2_service.run(input_dir)
    state = phase2_service.status(caught.value.safe_details["job_id"])
    assert state["stages"]["analyze"]["error"]["code"] == "analysis_incomplete"
    assert not artifacts_for(state, "plan")
    assert not artifacts_for(state, "render")
```

Add tests for Phase 1 `cloud_enabled=false` retaining six stages, budget exhaustion, provider auth redaction, no key in DB/report/status, changed prompt invalidating analyze onward only, changed ranking settings invalidating rank onward, missing one short rerendering only that output, resume reusing uploads/results, source hashes unchanged, and report final status `completed`.

- [ ] **Step 3: Verify RED**

Run: `uv run pytest tests/unit/test_workflow.py tests/unit/test_reporting.py tests/integration/test_cli_workflow.py tests/integration/test_phase2_workflow.py -v`

Expected: Phase 1 tests pass; Phase 2 tests fail.

- [ ] **Step 4: Add stage-specific versions and dependency fingerprints**

```python
PHASE1_STAGES = ("inspect", "proxy", "plan", "render", "validate", "report")
PHASE2_STAGES = ("inspect", "proxy", "segment", "analyze", "rank", "plan", "render", "validate", "report")
STAGE_IMPLEMENTATION_VERSIONS = {
    "inspect": "inspect-v2",
    "proxy": "proxy-v2",
    "segment": "segment-v1",
    "analyze": "gemini-static-v1",
    "rank": "ranking-v1",
    "plan": "highlight-plan-v1",
    "render": "render-v3",
    "validate": "output-validation-v2",
    "report": "report-v2",
}
```

Each stage fingerprint includes only authoritative upstream results and its settings/version. A downstream setting change must not regenerate valid proxies or broad results.

- [ ] **Step 5: Wire Phase 2 stages with hard gates**

`_ensure_segment` creates local evidence and cloud chunks. `_ensure_analyze` checks all broad reservations, runs broad/refinement, and refuses incomplete coverage. `_ensure_rank` requires coverage report exactly complete. `_ensure_plan` chooses sample planner in Phase 1 and highlight planner in Phase 2. `_ensure_render` uses v2 explicit filenames and serial rendering. `_ensure_validate` returns per-output advanced evidence.

- [ ] **Step 6: Fix per-output artifact recovery**

Derive output path from v2 `plan.output.filename`, never dimensions. Persist plan ID/digest and expected final before render. On resume, validate each plan/output pair independently; keep valid `long.mp4` and completed shorts while rerendering only missing/changed outputs.

- [ ] **Step 7: Build safe structured report and authoritative final status**

Report includes provider/model/FPS, manifest IDs/digests but no original upload paths, per-source coverage/missing ranges, estimated/actual/reserved cost, cache hits, candidate/rejection counts, score breakdowns, chapters, output relationships, crop/transition evidence, warnings/fallbacks, and AI quality disclaimer.

After report stage completes, call `complete_job`, rebuild report state from authoritative completed job, and atomically rewrite JSON/Markdown reports. If interrupted before final rewrite, resume performs finalization. Pre-completion reports must explicitly say `snapshot_status: pre_completion`.

- [ ] **Step 8: Verify GREEN and no-network default**

```bash
uv run pytest tests/unit/test_workflow.py tests/unit/test_reporting.py tests/integration/test_cli_workflow.py tests/integration/test_phase2_workflow.py -v
uv run mypy src/video_editor/workflow.py src/video_editor/reporting.py src/video_editor/cli.py
rg -n "GEMINI_API_KEY|api_key" tests/.tmp src/video_editor --glob '!**/*.pyc'
```

Expected: Phase 1 and Phase 2 tests pass; no runtime key value appears in artifacts.

- [ ] **Step 9: Detect changes and commit**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

```bash
git add src/video_editor/workflow.py src/video_editor/reporting.py src/video_editor/cli.py tests/unit/test_workflow.py tests/unit/test_reporting.py tests/integration/test_cli_workflow.py tests/integration/test_phase2_workflow.py
git commit -m "feat: orchestrate resumable phase 2 workflow"
```

## Task 13: End-to-End Safety and Acceptance Fixtures

**Files:**
- Modify: `tests/integration/test_end_to_end.py`
- Modify: `tests/integration/test_phase2_workflow.py`
- Modify: `tests/integration/test_phase2_render.py`
- Create: `tests/fixtures/gemini/multi-short-broad.json`
- Create: `tests/fixtures/gemini/multi-short-refinements.json`

**Interfaces:**
- Consumes: public CLI, real local FFmpeg/ffprobe, fake provider, generated media, SQLite.
- Produces: offline end-to-end proof of source immutability, cost/coverage gates, multiple distinct shorts, output validity, and resume/cache behavior.

- [ ] **Step 1: Write full offline acceptance test**

```python
def test_phase2_end_to_end_multiple_shorts_resume_and_source_integrity(
    input_dir: Path,
    phase2_config: Path,
    fake_provider_fixture: FakeAnalysisProvider,
):
    before = hash_tree(input_dir)
    first = run_phase2_cli(
        phase2_config,
        input_dir,
        provider=fake_provider_fixture,
    )
    assert output_names(first) == {
        "long.mp4",
        "short-01.mp4",
        "short-02.mp4",
    }
    first_report = report(first)
    assert first_report["analysis"]["coverage_ratio"] == "1"
    assert Decimal(first_report["cloud_usage"]["actual_cost_usd"]) <= (
        source_hours(first) * Decimal("1.00")
    )
    rendered_hashes = output_hashes(first)
    calls_before_resume = fake_provider_fixture.calls
    second = resume_cli(
        first.job_id,
        phase2_config,
        provider=fake_provider_fixture,
    )
    assert output_hashes(second) == rendered_hashes
    assert fake_provider_fixture.calls == calls_before_resume
    assert hash_tree(input_dir) == before
```

- [ ] **Step 2: Add fail-closed scenarios**

Run separate jobs for broad preflight over budget, one invalid broad result, provider timeout after bounded retries, unknown billing after dispatch, tampered proxy before upload, and disconnected output volume. Assert no plan/render artifacts for analysis failures, reservation remains for unknown billing, storage reserve is unchanged, and completed earlier stages remain reusable.

- [ ] **Step 3: Add semantic output assertions**

Load every v2 plan and assert chronology, caps, unique filenames, short overlap/group rules, transition relation matrix, crop evidence limits, dimensions, audio presence, and report relationships. Confirm broad fixture has no speech meaning and refinement fixture meaning stays within candidate IDs.

- [ ] **Step 4: Run acceptance suite twice**

```bash
uv run pytest tests/integration/test_end_to_end.py tests/integration/test_phase2_workflow.py tests/integration/test_phase2_render.py -v
uv run pytest tests/integration/test_end_to_end.py tests/integration/test_phase2_workflow.py tests/integration/test_phase2_render.py -v
```

Expected: both runs pass; no network call occurs; deterministic plans/reports match after removing timestamps/job IDs.

- [ ] **Step 5: Detect changes and commit**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

```bash
git add tests/integration tests/fixtures/gemini
git commit -m "test: cover phase 2 end to end safety"
```

## Task 14: Evaluation Harness, Release Gate, and Documentation

**Files:**
- Create: `src/video_editor/evaluation.py`
- Create: `tests/unit/test_evaluation.py`
- Create: `tests/evaluation/schema.json`
- Create: `tests/evaluation/thresholds-v1.json`
- Create: `tests/evaluation/synthetic-labels.json`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `config.example.toml`
- Modify: `.env.example`

**Interfaces:**
- Consumes: labeled dataset, generated selection/crop/transition records, calibration-selected thresholds, and holdout partition.
- Produces: `EvaluationMetrics`, `EvaluationThresholds`, `evaluate_predictions`, `check_release_gate`, and a machine-readable evaluation report.

- [ ] **Step 1: Write fixed-formula and partition RED tests**

```python
def test_fixed_metric_formulas():
    metrics = evaluate_predictions(labels_fixture(), predictions_fixture())
    assert metrics.must_include_recall == Decimal("2") / Decimal("3")
    assert metrics.weak_rejection == Decimal("3") / Decimal("4")
    assert metrics.duplicate_rate == Decimal("1") / Decimal("6")
    assert metrics.abrupt_cut_rate == Decimal("1") / Decimal("5")


def test_release_gate_rejects_small_or_leaky_dataset():
    with pytest.raises(EvaluationError, match="100 candidate intervals"):
        check_release_gate(synthetic_labels, thresholds)
    with pytest.raises(EvaluationError, match="partition"):
        check_release_gate(overlapping_partition_labels, thresholds)
```

Add chronology correctness, crop retention, duration compliance, short diversity, human preference, missing labels, fewer than three batches, threshold version/model/prompt/config mismatch, and holdout run before threshold freeze.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/unit/test_evaluation.py -v`

Expected: FAIL on missing evaluation module.

- [ ] **Step 3: Implement strict label and metric contracts**

Dataset rows identify batch/source/range, partition (`train`, `calibration`, `holdout`), must-include/acceptable/weak/duplicate labels, chapter/order, subject boxes, and acceptable transitions. Enforce unique interval IDs and batch-level partition isolation. Implement exact approved formulas and hard gates for duration, budget, source safety, and schema validity.

- [ ] **Step 4: Commit threshold workflow before holdout**

`thresholds-v1.json` contains explicit values for must-include recall minimum, weak-rejection minimum, duplicate-rate maximum, crop-retention minimum, abrupt-cut maximum, short-diversity minimum, and human-preference minimum, plus model/prompt/config/dataset versions and calibration evidence digest. Synthetic values prove plumbing only and carry `release_eligible: false`.

For real release work: label at least 100 intervals from at least three authorized batches; tune on train, select/freeze thresholds on calibration, commit threshold file, then run holdout exactly once for release evidence. Missing real labels keeps release status blocked rather than weakening requirements.

- [ ] **Step 5: Update user and architecture documentation**

Document nine stages, offline versus Gemini configuration, proxy-only upload proof, exact model, USD 1/hour cap, 100% coverage gate, output names, multiple shorts, source-only audio, tracked crop, transition semantics, resume behavior, live-test opt-in, credential safety, evaluation limitations, and Phase 1 compatibility. Do not claim Phase 2 quality passed until real holdout evidence exists.

- [ ] **Step 6: Verify evaluation and docs**

```bash
uv run pytest tests/unit/test_evaluation.py -v
uv run python -m video_editor.evaluation --labels tests/evaluation/synthetic-labels.json --thresholds tests/evaluation/thresholds-v1.json --expect-blocked-release
rg -n "USD 1.00|100%|gemini-2.5-flash|GEMINI_API_KEY|short-01.mp4|1,800|180" README.md docs/architecture.md config.example.toml .env.example
```

Expected: formula tests pass; synthetic evaluation reports metrics but blocks release; documentation contains every boundary.

- [ ] **Step 7: Run complete quality gate**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -v -m "not gemini_live"
uv build
uv run video-editor --help
git diff --check
```

Expected: all offline checks pass. Record total test count and exact dependency/model/config versions. Do not run live Gemini test without explicit approval and a pre-reserved ≤USD 0.01 maximum.

- [ ] **Step 8: Scan final tree for safety violations**

```bash
rg -n "GEMINI_API_KEY=.+|api[_-]?key.{0,20}[A-Za-z0-9]{16}|TODO|TBD|FIXME|implement later|fill in" . --glob '!uv.lock' --glob '!.git/**'
rg -n "upload\(|files\.upload" src/video_editor
rg -n "unlink\(|rmtree\(|remove\(" src/video_editor
```

Expected: no credential or placeholder; every upload call routes through manifest validation; every cleanup target is proven inside generated roots.

- [ ] **Step 9: Whole-branch GitNexus and independent review**

```text
detect_changes({scope: "compare", base_ref: "main"})
```

Expected flows: configuration, persistence, Phase 2 analysis/planning/render/report, plus preserved Phase 1 flow. No unexpected source-discovery or cleanup flow.

Run fresh reviews for business logic, config/secret safety, concurrency/budget races, silent failures, and whole-branch code quality. Fix confirmed findings and rerun complete quality gate.

- [ ] **Step 10: Commit evaluation and docs**

```bash
git add src/video_editor/evaluation.py tests/unit/test_evaluation.py tests/evaluation README.md docs/architecture.md config.example.toml .env.example
git commit -m "docs: add phase 2 evaluation and operations"
```

## Final Acceptance Evidence

Before requesting merge or making Phase 2 quality claims, retain:

1. Full offline quality-gate logs.
2. GitNexus final `detect_changes` scope.
3. SQLite concurrency/budget test evidence.
4. Proxy provenance/tamper rejection test evidence.
5. 100% broad-coverage and missing-range failure evidence.
6. One offline fake-provider end-to-end run with multiple shorts and unchanged source hashes.
7. Resume evidence showing zero additional provider calls and unchanged valid outputs.
8. Real labeled evaluation report with at least 100 intervals across at least three batches and frozen calibration thresholds.
9. Separately approved live Gemini contract test evidence with exact model, request maximum, actual cost, sanitized response, and no source upload.
10. Updated limitation statement restricting claims to evaluated footage categories and recorded model/settings.

No commit, push, merge, live cloud request, cache deletion, reserve reduction, or original-file modification is authorized by this plan itself.
