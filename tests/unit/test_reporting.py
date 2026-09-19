from pathlib import Path

from video_editor.reporting import write_json_report, write_markdown_report


def test_reports_include_phase_one_disclosures(tmp_path: Path) -> None:
    state = {"job_id": "job-1", "status": "completed"}
    markdown = write_markdown_report(state, tmp_path / "report.md").read_text()
    assert "deterministic sample selection" in markdown
    assert "not automatic highlight intelligence" in markdown
    assert "Cloud usage: 0" in markdown
    assert write_json_report(state, tmp_path / "report.json").is_file()
