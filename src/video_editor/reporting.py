"""Stable JSON and Markdown reports for workflow jobs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_LIMITATION = "Phase 1 uses deterministic sample selection; it is not automatic highlight intelligence."


def _payload(job_state: dict[str, Any]) -> dict[str, Any]:
    payload = dict(job_state)
    payload.setdefault("selected_moments", [])
    payload.setdefault("skipped_inputs", [])
    payload.setdefault("warnings", [])
    payload.setdefault("fallbacks", [])
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
        ("Fallbacks", "fallbacks"),
        ("Stage times", "stage_times"),
        ("Resolved storage roots", "storage_roots"),
    ):
        lines.extend(["", f"## {title}", *_lines(state[key])])
    lines.extend(
        [
            "",
            f"- Estimated peak space bytes: {state['estimated_peak_space_bytes']}",
            "",
            "## Phase 1 limitations",
            str(state["phase_one_limitations"]),
            "",
        ]
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path
