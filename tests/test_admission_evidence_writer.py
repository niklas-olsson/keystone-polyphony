"""Experimental inspection stays distinct from authenticated forge evidence."""

import json
import sqlite3

import pytest

from src.core.collaboration.ledger import FencedLedger
from src.core.collaboration.coordinator import CollaborationCoordinator
from src.core.collaboration.adapter import DeterministicExecutionAdapter
from src.core.collaboration.admission import (
    AdmissionHandoffEmitter,
    ExperimentalReceiptExporter,
)
from src.core.collaboration.models import ReviewVerdict
from src.liminal_bridge.review_gate import eligibility

TASK = "t_local_1"
BASE = "b" * 40


@pytest.fixture
def accepted_ledger(tmp_path):
    path = tmp_path / "historical-ledger.db"
    ledger = FencedLedger(str(path))
    coord = CollaborationCoordinator(ledger, DeterministicExecutionAdapter())
    coord.register_task(TASK, "Local task", "Desc", BASE, ["crit"])
    ok, handle, _ = coord.dispatch_attempt(TASK, "dev_1", now=10.0)
    assert ok
    ok, submitted, _ = coord.submit_attempt(
        TASK, handle.attempt_id, "dev_1", 1, "req1", "diff", 2.0, now=20.0
    )
    assert ok
    for verdict, count, notes, stamp in (
        (ReviewVerdict.FAIL, 1, "Original dissent", 25.0),
        (ReviewVerdict.PASS, 0, "Local follow-up", 30.0),
    ):
        assert coord.record_review(
            submitted.candidate_sha,
            TASK,
            "rev_1",
            verdict,
            count,
            notes=notes,
            now=stamp,
        )[0]
    return path, ledger, coord, submitted.candidate_sha


def test_export_preserves_local_identity_dissent_and_historical_records(
    accepted_ledger, tmp_path
):
    path, ledger, _, candidate_id = accepted_ledger
    with sqlite3.connect(path) as connection:
        before = list(connection.iterdump())
    exporter = ExperimentalReceiptExporter(ledger)
    output = tmp_path / "experimental-receipt.json"
    exporter.write_local_receipt(TASK, str(output))
    receipt = json.loads(output.read_text())
    assert receipt["receipt_type"] == "polyphony-experimental-local"
    assert receipt["schema_version"] == 1
    assert receipt["release_authorized"] is False
    assert receipt["managed_authority"] is False
    assert receipt["identity_source"] == "caller-supplied"
    assert receipt["task_id"] == TASK
    assert receipt["candidate_id"] == candidate_id
    assert receipt["baseline_reference"] == BASE
    assert [review["verdict"] for review in receipt["local_reviews"]] == [
        "FAIL",
        "PASS",
    ]
    assert receipt["local_reviews"][0]["notes"] == "Original dissent"
    assert not {
        "head_sha",
        "base_sha",
        "workflow_path",
        "run_id",
        "run_attempt",
        "pr_number",
        "review_text",
        "review_verdict",
        "verdict",
    }.intersection(receipt)

    reopened = ExperimentalReceiptExporter(FencedLedger(str(path)))
    assert reopened.emit_local_receipt(TASK) == receipt
    with sqlite3.connect(path) as connection:
        assert list(connection.iterdump()) == before


def test_later_failure_cannot_export_an_old_accepted_pointer(accepted_ledger):
    _, ledger, coord, candidate_id = accepted_ledger
    assert coord.record_review(
        candidate_id, TASK, "rev_2", ReviewVerdict.FAIL, 1, now=35.0
    )[0]
    assert ledger.get_task(TASK).accepted_candidate_id == candidate_id
    with pytest.raises(ValueError, match="not locally accepted"):
        ExperimentalReceiptExporter(ledger).emit_local_receipt(TASK)


def test_legacy_names_only_export_local_receipts(
    accepted_ledger, tmp_path, monkeypatch
):
    _, ledger, _, _ = accepted_ledger
    exporter = AdmissionHandoffEmitter(ledger)
    expected = exporter.emit_local_receipt(TASK)
    with pytest.warns(DeprecationWarning, match="experimental local receipt"):
        assert exporter.emit_review_evidence(TASK) == expected
    monkeypatch.chdir(tmp_path)
    with pytest.warns(DeprecationWarning, match="experimental local receipt"):
        assert exporter.write_evidence_file(TASK) == "experimental-receipt.json"
    assert json.loads((tmp_path / "experimental-receipt.json").read_text()) == expected
    assert not (tmp_path / "review-evidence.json").exists()


@pytest.mark.parametrize(
    "metadata",
    [
        {"pr_number": 101},
        {"workflow_path": ".github/workflows/opencode.yml"},
        {"run_id": 12345678},
        {"run_attempt": 1},
    ],
)
def test_legacy_forge_metadata_fails_without_writing(
    accepted_ledger, tmp_path, metadata
):
    _, ledger, _, _ = accepted_ledger
    exporter = AdmissionHandoffEmitter(ledger)
    with pytest.raises(ValueError, match="cannot certify GitHub"):
        exporter.emit_review_evidence(TASK, **metadata)
    output = tmp_path / "receipt.json"
    with pytest.raises(ValueError, match="cannot certify GitHub"):
        exporter.write_evidence_file(TASK, str(output), **metadata)
    assert not output.exists()


def test_export_cannot_overwrite_the_forge_evidence_filename(accepted_ledger, tmp_path):
    _, ledger, _, _ = accepted_ledger
    output = tmp_path / "review-evidence.json"
    output.write_text("Existing workflow evidence")
    with pytest.raises(ValueError, match="reserved for authenticated forge"):
        ExperimentalReceiptExporter(ledger).write_local_receipt(TASK, str(output))
    assert output.read_text() == "Existing workflow evidence"


def test_actual_forge_gate_rejects_local_receipt_with_other_readiness_green(
    accepted_ledger,
):
    _, ledger, _, _ = accepted_ledger
    head = "a" * 40
    repo = "niklas-olsson/keystone-polyphony"
    pr = {
        "number": 8,
        "state": "open",
        "draft": False,
        "mergeable": True,
        "head": {"sha": head},
        "base": {"sha": BASE, "ref": "main", "repo": {"full_name": repo}},
        "labels": [{"name": "decision-merge"}],
    }
    readiness = {
        "checks": [
            {"name": "pr-validation", "status": "completed", "conclusion": "success"}
        ],
        "statuses": [],
        "reviews": [],
        "unresolved_threads": False,
        "required_checks": ["pr-validation"],
    }
    qualified_shape = {
        "version": 2,
        "repository": repo,
        "pr_number": 8,
        "head_sha": head,
        "base_sha": BASE,
        "exit_code": 0,
        "scope": "general",
        "review_text": 'No findings.\nREVIEW_RESULT: {"verdict":"PASS","blocking_findings":0}',
        "review_verdict": "PASS",
        "blocking_findings": 0,
    }
    assert eligibility(pr, qualified_shape, **readiness) == []
    receipt = ExperimentalReceiptExporter(ledger).emit_local_receipt(TASK)
    blockers = eligibility(pr, receipt, **readiness)
    assert "invalid_evidence_identity" in blockers
    assert "head_sha_mismatch" in blockers
    assert "automatic_review_not_passed" in blockers
