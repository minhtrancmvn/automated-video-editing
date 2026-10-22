"""Short diagnostics persist across plan stage and both report formats."""

import json
from pathlib import Path

from test_phase2_workflow import Phase2Env
from test_phase2_workflow import phase2 as fixture_phase2

phase2 = fixture_phase2


def test_short_diagnostics_are_persisted_without_changing_plans(
    phase2: Phase2Env,
) -> None:
    result = phase2.service.run(phase2.input_dir)
    planned = phase2.store.get_job(result["job_id"])["stages"]["plan"]["result"]
    diagnostics = planned["short_diagnostics"]
    report = json.loads(Path(result["reports"][0]).read_text())
    assert report["analysis"]["short_diagnostics"] == diagnostics
    assert len(diagnostics["candidates"]) == 8
    assert diagnostics["thresholds"]["minimum_score"] == "0.60"
    assert all(row["crop"] is not None for row in diagnostics["candidates"])
    assert all(
        "retention_ratio" in row["crop"]["validation"]
        for row in diagnostics["candidates"]
    )
    assert "short_diagnostics" in Path(result["reports"][1]).read_text()
    calls = (phase2.provider.broad_calls, phase2.provider.candidate_calls)
    resumed = phase2.service.resume(result["job_id"])
    assert resumed["outputs"] == result["outputs"]
    assert calls == (phase2.provider.broad_calls, phase2.provider.candidate_calls)
