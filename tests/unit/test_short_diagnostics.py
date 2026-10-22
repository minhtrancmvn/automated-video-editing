"""Observational short diagnostics never change planning decisions."""

import json
from pathlib import Path

from test_highlight_plan import _candidate, _plan, _track

from video_editor.config import CropSettings, HighlightSettings
from video_editor.planning import highlight_plan
from video_editor.reporting import write_json_report, write_markdown_report


def test_short_diagnostics_distinguish_primary_and_theme_rejections() -> None:
    low = _candidate("low", "s", "0", "10", total="0.50", event_id="ride")
    solo = _candidate("solo", "s", "20", "30", event_id="ride")
    missing = _candidate("missing", "s", "40", "50", selected=False, event_id="other")
    tracks = [_track(low, eligible=False), _track(solo)]
    before = _plan([low, solo, missing], ("s",), tracks=tuple(tracks))
    diagnostic = highlight_plan.short_eligibility_diagnostics(
        [low, solo, missing], tracks, HighlightSettings(), CropSettings()
    )
    rows = {r["candidate_id"]: r for r in diagnostic["candidates"]}
    assert rows["low"]["reasons"] == ["score_below_minimum", "crop_not_eligible"]
    assert rows["solo"]["reasons"] == ["theme_insufficient_moments"]
    assert rows["missing"]["reasons"] == ["not_selected", "crop_track_missing"]
    assert rows["low"]["theme_candidate_count"] == 2
    assert rows["low"]["theme_eligible_count"] == 1
    assert rows["low"]["score_total"] == "0.50"
    assert rows["low"]["raw_vertical"] == "0.90"
    assert diagnostic["thresholds"]["minimum_score"] == "0.60"
    assert _plan([low, solo, missing], ("s",), tracks=tuple(tracks)) == before


def test_crop_diagnostics_record_measurement_not_invent_original_failure() -> None:
    candidate = _candidate("a", "s", "0", "10")
    track = _track(candidate, eligible=True)
    result = highlight_plan.short_eligibility_diagnostics(
        [candidate], [track], HighlightSettings(), CropSettings()
    )["candidates"][0]
    assert result["crop"]["stored_short_eligible"] is True
    assert result["crop"]["measurement_source"] == "recomputed_from_saved_track"
    assert result["crop"]["validation"]["sampled_observations"] == 0
    assert result["crop"]["validation"]["eligible"] is False
    assert result["crop"]["failure_codes"] == [
        "no_local_observations",
        "retention_below_minimum",
    ]
    # Stored flag, not recomputation, remains planner's authoritative predicate.
    assert result["primary_eligible"] is True
    assert result["crop"]["eligibility_matches_measurement"] is False


def test_diagnostics_flow_into_both_report_formats(tmp_path: Path) -> None:
    candidate = _candidate("a", "s", "0", "10", total="0.5")
    diagnostic = highlight_plan.short_eligibility_diagnostics(
        [candidate], [], HighlightSettings(), CropSettings()
    )
    state = {
        "job_id": "job",
        "status": "completed",
        "analysis": {"short_diagnostics": diagnostic},
    }
    json_path = write_json_report(state, tmp_path / "report.json")
    md_path = write_markdown_report(state, tmp_path / "report.md")
    assert (
        json.loads(json_path.read_text())["analysis"]["short_diagnostics"] == diagnostic
    )
    assert "score_below_minimum" in md_path.read_text()
    assert "crop_track_missing" in md_path.read_text()
