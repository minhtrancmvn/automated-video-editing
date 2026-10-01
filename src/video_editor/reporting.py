"""Stable JSON and Markdown reports for workflow jobs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

_LIMITATION = "Phase 1 uses deterministic sample selection; it is not automatic highlight intelligence."
_AI_DISCLAIMER = (
    "Outputs use automatic AI highlight selection; quality, completeness, and "
    "subject framing are not guaranteed and should be reviewed before publishing."
)
_SECRET_KEYS = frozenset({"api_key", "gemini_api_key", "authorization", "credentials"})
_UPLOAD_PATH_KEYS = frozenset({"source_path", "artifact_path", "generated_root"})


def _redact(value: Any, drop: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact(item, drop)
            for key, item in value.items()
            if str(key).lower() not in drop
        }
    if isinstance(value, list | tuple):
        return [_redact(item, drop) for item in value]
    return value


def _payload(job_state: dict[str, Any]) -> dict[str, Any]:
    payload = cast(dict[str, Any], _redact(dict(job_state), _SECRET_KEYS))
    if "analysis" in payload:
        # Cloud manifests are reported by ID/digest only, never by local upload path.
        payload["analysis"] = _redact(payload["analysis"], _UPLOAD_PATH_KEYS)
        payload.setdefault("ai_quality_disclaimer", _AI_DISCLAIMER)
    payload["snapshot_status"] = (
        "final" if payload.get("status") == "completed" else "pre_completion"
    )
    payload.setdefault("selected_moments", [])
    payload.setdefault("skipped_inputs", [])
    payload.setdefault("warnings", [])
    payload.setdefault("fallbacks", [])
    payload.setdefault("chronology_warnings", [])
    payload.setdefault("stage_times", {})
    payload.setdefault("storage_roots", {})
    payload.setdefault("estimated_peak_space_bytes", 0)
    payload.setdefault("cloud_usage", 0)
    payload.setdefault("phase_one_limitations", _LIMITATION)
    return payload


def write_json_report(job_state: dict[str, Any], path: Path) -> Path:
    """Write canonical, JSON-serializable job report."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_payload(job_state), indent=2, sort_keys=True, default=str) + "\n"
    )
    return path


def _lines(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [f"- {key}: {item}" for key, item in value.items()]
    if isinstance(value, list):
        return [f"- {item}" for item in value] or ["- None"]
    return [f"- {value}"]


def write_markdown_report(job_state: dict[str, Any], path: Path) -> Path:
    """Write human-readable report with stable disclosure fields."""
    state = _payload(job_state)
    lines = [
        "# Video Editor Report",
        "",
        f"- Job: `{state.get('job_id', 'unknown')}`",
        f"- Status: `{state.get('status', 'unknown')}`",
        f"- Snapshot: `{state['snapshot_status']}`",
        f"- Cloud usage: {state.get('cloud_usage', 0)}",
        "",
        "## Selected moments",
        *_lines(state["selected_moments"]),
        "",
        "## Output",
    ]
    output = state.get("output", {})
    if isinstance(output, dict):
        lines.extend(_lines(output))
    else:
        lines.append(f"- {output}")
    for title, key in (
        ("Skipped inputs", "skipped_inputs"),
        ("Warnings", "warnings"),
        ("Chronology warnings", "chronology_warnings"),
        ("Fallbacks", "fallbacks"),
        ("Stage times", "stage_times"),
        ("Resolved storage roots", "storage_roots"),
    ):
        lines.extend(["", f"## {title}", *_lines(state[key])])
    lines.extend(
        [
            "",
            f"- Estimated peak space bytes: {state['estimated_peak_space_bytes']}",
            f"- Estimated peak space scope: {state.get('estimated_peak_space_scope', 'unknown')}",
            "",
            "## Phase 1 limitations",
            str(state["phase_one_limitations"]),
            "",
        ]
    )
    if "analysis" in state:
        lines.extend(
            [
                "## Analysis",
                *_lines(state["analysis"]),
                "",
                "## AI quality disclaimer",
                str(state["ai_quality_disclaimer"]),
                "",
            ]
        )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path
