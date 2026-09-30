# Task 6A Report: Gemini Adapter RED Contract

## Scope

Create and commit offline RED tests only from feature HEAD `9d9ef30`. No production adapter, model, dependency, network, credential, upload, or live Gemini action.

## Inputs copied

- `tests/fixtures/gemini/broad-response.json`
- `tests/fixtures/gemini/refinement-response.json`
- `tests/fixtures/gemini/malformed-response.json`
- This report scaffold

Only requested three fixtures and report scaffold copied from `/Users/coffeemug/Programming/automated-video-editing/.claude/worktrees/agent-abce352ec0254e9b0`.

## Blast radius

`impact(target="GeminiAdapter", direction="upstream")` returned target missing, `impactedCount: 0`, risk `UNKNOWN`. New adapter symbol does not exist at `9d9ef30`; no existing production symbol changed.

## RED contract

`tests/unit/test_gemini.py` defines offline fake-client contract for:

- `video/mp4` upload, `PROCESSING` to `ACTIVE`, terminal `FAILED`, expiry reupload, delete
- exact `gemini-2.5-flash`, broad 0.5 FPS, candidate 2/3/5 FPS, duration-string offsets
- JSON MIME, strict response schemas, `response.parsed`
- chunk identity and requested timestamp containment
- no retry for auth and invalid request; bounded retry for rate limit, 5xx, network; one schema repair
- broad no transcript/speech meaning/person identity; bounded candidate speech meaning
- request ID and usage payload; redacted key/client/errors
- explicit `gemini_live` marker; default skip; USD 0.01 preflight before upload
- sanitized recorded fixtures

`pyproject.toml` registers `gemini_live` marker.

## Focused RED command and output

```text
$ uv run pytest tests/unit/test_gemini.py -v
============================= test session starts ==============================
platform darwin -- Python 3.12.12, pytest-9.1.1, pluggy-1.6.0 -- /Users/coffeemug/Programming/automated-video-editing/.claude/worktrees/agent-a2dddf90746df7ae1/.venv/bin/python
cachedir: .pytest_cache
rootdir: /Users/coffeemug/Programming/automated-video-editing/.claude/worktrees/agent-a2dddf90746df7ae1
configfile: pyproject.toml
plugins: anyio-4.15.1
collecting ... collected 19 items

1 passed, 1 skipped, 17 errors in 0.11s

E AssertionError: Gemini adapter contract missing: video_editor.analysis.gemini public symbols do not exist
E assert ModuleNotFoundError("No module named 'video_editor.analysis.gemini'") is None

EXIT_CODE=1
```

RED failure reason correct: adapter module/public symbols absent. Fixture validation passes. Live test skipped before any upload because `RUN_GEMINI_LIVE_TESTS` not set.

## Production changes

None. `src/` untouched.
