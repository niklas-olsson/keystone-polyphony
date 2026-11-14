"""Run the published recipe against the real native service and a new store."""

import json
from pathlib import Path
import subprocess
import sys


def test_fixed_dependency_recipe_retains_work_and_refuses_existing_directory(tmp_path):
    script = (
        Path(__file__).resolve().parents[1] / "scripts/example-fixed-dependencies.py"
    )
    directory = tmp_path / "example"
    command = [sys.executable, "-B", str(script), "--directory", str(directory)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=70)
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    report_bytes = (directory / "report.json").read_bytes()
    report = json.loads(report_bytes)
    assert summary["passed"] and report["saved_artifacts_after_reopen"] == 5
    assert report["current_admissions_after_reopen"] == ["unrelated"]
    assert len(report["observed_concurrently_reserved_attempts"]) == 2
    assert report["allocation"]["sum_allocated_seconds"] == 150
    assert report["allocation"]["plan_wide_enforcement"] is False
    assert report["parallel_execution_measured"] is False
    assert report["release_authorized"] is False
    assert "scripted fixtures" in report["principal_kind"]
    for name, packet in report["historical_admissions"].items():
        assert packet["candidate"]["task"] == report["tasks"][name]
        assert packet["candidate"]["author"] != packet["evidence"][0]["reviewer"]
        assert packet["candidate"]["patch"] == report["artifacts"][name]["sha256"]
    refused = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert refused.returncode != 0
    assert (directory / "report.json").read_bytes() == report_bytes
