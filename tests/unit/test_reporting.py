from pathlib import Path

from video_editor.reporting import write_json_report, write_markdown_report


def test_reports_include_phase_one_disclosures(tmp_path: Path) -> None:
    state = {
        "job_id": "job-1",
        "status": "completed",
        "estimated_peak_space_scope": "render-output byte growth only",
    }
    markdown = write_markdown_report(state, tmp_path / "report.md").read_text()
    assert "deterministic sample selection" in markdown
    assert "not automatic highlight intelligence" in markdown
    assert "Cloud usage: 0" in markdown
    assert "Estimated peak space scope: render-output byte growth only" in markdown
    assert write_json_report(state, tmp_path / "report.json").is_file()


def test_reports_expose_chronology_warnings_with_group_and_source_context(
    tmp_path: Path,
) -> None:
    state = {
        "chronology_warnings": [
            {
                "group_id": "12-session-1",
                "source_id": "source-2",
                "warning": "duplicate chapter: file 12 chapter 2",
            }
        ]
    }
    json_report = write_json_report(state, tmp_path / "report.json").read_text()
    markdown = write_markdown_report(state, tmp_path / "report.md").read_text()
    assert '"group_id": "12-session-1"' in json_report
    assert '"source_id": "source-2"' in json_report
    assert "## Chronology warnings" in markdown
    assert "12-session-1" in markdown
    assert "source-2" in markdown


def test_pre_completion_snapshot_is_labelled(tmp_path: Path) -> None:
    state = {"job_id": "job-1", "status": "running"}
    report = write_json_report(state, tmp_path / "report.json").read_text()
    markdown = write_markdown_report(state, tmp_path / "report.md").read_text()
    assert '"snapshot_status": "pre_completion"' in report
    assert "Snapshot: `pre_completion`" in markdown


def test_completed_report_is_final_snapshot(tmp_path: Path) -> None:
    state = {"job_id": "job-1", "status": "completed"}
    report = write_json_report(state, tmp_path / "report.json").read_text()
    assert '"snapshot_status": "final"' in report


def test_phase2_report_omits_secret_and_upload_paths(tmp_path: Path) -> None:
    state = {
        "job_id": "job-1",
        "status": "completed",
        "analysis": {
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "api_key": "AIza-secret",
            "manifests": [
                {
                    "manifest_id": "m-1",
                    "digest": "abc",
                    "source_path": "/input/01.mp4",
                    "artifact_path": "/cache/chunk.mp4",
                }
            ],
        },
    }
    report = write_json_report(state, tmp_path / "report.json").read_text()
    markdown = write_markdown_report(state, tmp_path / "report.md").read_text()
    for text in (report, markdown):
        assert "AIza-secret" not in text
        assert "/input/01.mp4" not in text
        assert "/cache/chunk.mp4" not in text
    assert '"manifest_id": "m-1"' in report
    assert "automatic AI highlight selection" in markdown
