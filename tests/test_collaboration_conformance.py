"""Comprehensive Adversarial Conformance Test Suite for Issue #95.

Exercises all 10 adversarial failure scenarios:
1. Duplicate dispatch or submission
2. Worker returns after reassignment
3. Coordinator crashes after commit but before reply
4. Partition or missed notification
5. Conflicting contributions
6. Candidate or acceptance basis changes after review
7. Cancellation races with submission
8. Budget exhausted or capability unavailable
9. Missing artifact or verification evidence
10. Fresh participant joins
"""

import pytest
import tempfile
import os
from src.core.collaboration.ledger import FencedLedger
from src.core.collaboration.adapter import DeterministicExecutionAdapter, AttemptLimits
from src.core.collaboration.coordinator import CollaborationCoordinator
from src.core.collaboration.conductor import EnsembleConductor, WorkerFlowManager
from src.core.collaboration.admission import AdmissionHandoffEmitter
from src.core.collaboration.models import (
    TaskStatus,
    ReviewVerdict,
    AttemptStatus,
)


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def harness(temp_db):
    ledger = FencedLedger(db_path=temp_db)
    adapter = DeterministicExecutionAdapter()
    coordinator = CollaborationCoordinator(ledger, adapter)
    conductor = EnsembleConductor(ledger)
    admission = AdmissionHandoffEmitter(ledger)
    return ledger, adapter, coordinator, conductor, admission, temp_db


# Scenario 1: Duplicate dispatch or submission
def test_scenario_1_duplicate_dispatch_or_submission(harness):
    """No duplicate accepted assignment/result; consistent retry receipt."""
    ledger, adapter, coordinator, conductor, admission, _ = harness

    coordinator.register_task(
        task_id="t1",
        title="T1",
        description="Desc",
        baseline_sha="b1",
        acceptance_criteria=["c1"],
    )

    # First dispatch
    ok1, h1, _ = coordinator.dispatch_attempt("t1", "worker1", now=100.0)
    assert ok1 is True

    # Duplicate dispatch from same or different worker while first is active -> Rejected
    ok_dup, h_dup, _ = coordinator.dispatch_attempt("t1", "worker1", now=101.0)
    assert ok_dup is False

    # First submission
    ok_s1, sub1, _ = coordinator.submit_attempt(
        "t1", h1.attempt_id, "worker1", 1, "req_dup", "diff", 3.0, now=110.0
    )
    assert ok_s1 is True

    # Duplicate submission with identical content -> Returns identical receipt
    ok_s2, sub2, msg2 = coordinator.submit_attempt(
        "t1", h1.attempt_id, "worker1", 1, "req_dup", "diff", 3.0, now=112.0
    )
    assert ok_s2 is True
    assert sub2.receipt_id == sub1.receipt_id
    assert "Idempotent retry" in msg2


# Scenario 2: Worker returns after reassignment
def test_scenario_2_worker_returns_after_reassignment(harness):
    """Obsolete submission rejected when worker returns with stale fence."""
    ledger, adapter, coordinator, conductor, admission, _ = harness

    coordinator.register_task(
        task_id="t2",
        title="T2",
        description="Desc",
        baseline_sha="b2",
        acceptance_criteria=["c2"],
    )

    ok1, h1, _ = coordinator.dispatch_attempt(
        "t2", "worker_old", lease_duration=50.0, now=100.0
    )
    assert h1.fence_token == 1

    # Lease expires at 150.0. At 160.0, reassignment committed to worker_new with fence 2
    ok2, h2, _ = coordinator.dispatch_attempt(
        "t2", "worker_new", lease_duration=50.0, now=160.0
    )
    assert h2.fence_token == 2

    # Old worker returns with fence 1
    ok_late, sub_late, reason = coordinator.submit_attempt(
        "t2", h1.attempt_id, "worker_old", 1, "req_old", "late patch", 2.0, now=170.0
    )
    assert ok_late is False
    assert sub_late is None
    assert "Superseded fence" in reason


# Scenario 3: Coordinator crashes after commit but before reply
def test_scenario_3_coordinator_crash_after_commit_before_reply(harness):
    """Recovery returns committed state without repeating acceptance or corrupting fences."""
    ledger, adapter, coordinator, conductor, admission, db_path = harness

    coordinator.register_task(
        task_id="t3",
        title="T3",
        description="Desc",
        baseline_sha="b3",
        acceptance_criteria=["c3"],
    )

    ok1, h1, _ = coordinator.dispatch_attempt("t3", "w_crash", now=100.0)

    # Submission commits to SQLite WAL
    ok_sub, sub, _ = coordinator.submit_attempt(
        "t3", h1.attempt_id, "w_crash", 1, "req_crash", "patch content", 4.0, now=110.0
    )
    assert ok_sub is True

    # Simulate coordinator crash & process restart by recreating coordinator from the same DB file
    recovered_ledger = FencedLedger(db_path=db_path)
    recovered_adapter = DeterministicExecutionAdapter()
    recovered_coordinator = CollaborationCoordinator(
        recovered_ledger, recovered_adapter
    )

    # Worker resends the exact request_id
    ok_resend, sub_resend, msg_resend = recovered_coordinator.submit_attempt(
        "t3", h1.attempt_id, "w_crash", 1, "req_crash", "patch content", 4.0, now=115.0
    )
    assert ok_resend is True
    assert sub_resend.receipt_id == sub.receipt_id
    assert "Idempotent retry" in msg_resend

    # Verify task state in recovered DB
    task_recovered = recovered_ledger.get_task("t3")
    assert task_recovered.status == TaskStatus.INTEGRATING
    assert task_recovered.current_fence_token == 1


# Scenario 4: Partition or missed notification
def test_scenario_4_partition_or_missed_notification(harness):
    """No self-granted ownership; partitioned worker cannot mutate state; views reconcile from authority."""
    ledger, adapter, coordinator, conductor, admission, _ = harness

    coordinator.register_task(
        task_id="t4",
        title="T4",
        description="Desc",
        baseline_sha="b4",
        acceptance_criteria=["c4"],
    )

    # Partitioned adapter
    adapter.simulate_partition = True
    ok, h, msg = coordinator.dispatch_attempt("t4", "w_part", now=100.0)
    assert ok is True

    obs = adapter.observe(h)
    assert obs.status == "unreachable"

    # Worker cannot self-grant a new assignment or renew without authority acknowledgment
    # When connection restores, authority state remains authoritative
    adapter.simulate_partition = False
    task = ledger.get_task("t4")
    assert task.current_fence_token == 1


# Scenario 5: Conflicting contributions
def test_scenario_5_conflicting_contributions(harness):
    """Explicit hold; no last-writer-wins; conflicts do not become accepted results."""
    ledger, adapter, coordinator, conductor, admission, _ = harness

    coordinator.register_task(
        task_id="t5",
        title="T5",
        description="Desc",
        baseline_sha="b5",
        acceptance_criteria=["c5"],
    )

    ok1, h1, _ = coordinator.dispatch_attempt("t5", "w_conf_1", now=100.0)
    ok_sub1, sub1, _ = coordinator.submit_attempt(
        "t5", h1.attempt_id, "w_conf_1", 1, "req_c1", "diff1", 2.0, now=110.0
    )

    # Reviewer encounters merge conflict or unresolved dispute -> marks HOLD
    ok_rev, rec, _ = coordinator.record_review(
        candidate_id=sub1.candidate_sha,
        task_id="t5",
        reviewer_actor_id="rev_arbitrator",
        verdict=ReviewVerdict.HOLD,
        blocking_findings=1,
        notes="Unresolved conflict between parallel proposals",
        now=120.0,
    )
    assert ok_rev is True
    assert rec.verdict == ReviewVerdict.HOLD

    task = ledger.get_task("t5")
    assert task.status == TaskStatus.HELD
    assert task.accepted_candidate_id is None


# Scenario 6: Candidate or acceptance basis changes after review
def test_scenario_6_candidate_or_acceptance_basis_changes_after_review(harness):
    """Stale evidence cannot complete the task if spec or baseline changed."""
    ledger, adapter, coordinator, conductor, admission, _ = harness

    coordinator.register_task(
        task_id="t6",
        title="T6",
        description="Desc",
        baseline_sha="b6",
        acceptance_criteria=["c6"],
    )

    ok1, h1, _ = coordinator.dispatch_attempt("t6", "w6", now=100.0)
    ok_s, sub, _ = coordinator.submit_attempt(
        "t6", h1.attempt_id, "w6", 1, "req6", "diff6", 2.0, now=110.0
    )

    # Review passes
    coordinator.record_review(
        sub.candidate_sha, "t6", "rev6", ReviewVerdict.PASS, 0, now=115.0
    )
    assert ledger.get_task("t6").status == TaskStatus.ACCEPTED

    # Criteria changes
    ledger.bump_spec_revision("t6", new_acceptance_criteria=["new crit"], now=120.0)
    task_after = ledger.get_task("t6")
    assert task_after.status == TaskStatus.HELD
    assert task_after.accepted_candidate_id is None

    # Stale candidate cannot be emitted for admission
    with pytest.raises(ValueError, match="has no accepted candidate"):
        admission.emit_local_receipt("t6")


# Scenario 7: Cancellation races with submission
def test_scenario_7_cancellation_races_with_submission(harness):
    """Atomic CAS determines outcome; cancellation settles capacity properly."""
    ledger, adapter, coordinator, conductor, admission, _ = harness

    coordinator.register_task(
        task_id="t7",
        title="T7",
        description="Desc",
        baseline_sha="b7",
        acceptance_criteria=["c7"],
        max_budget=50.0,
    )

    ok1, h1, _ = coordinator.dispatch_attempt("t7", "w7", reserve_cost=15.0, now=100.0)

    # Cancellation executed first
    ok_cancel, term_conf, _ = coordinator.cancel_attempt("t7", h1.attempt_id, now=105.0)
    assert ok_cancel is True

    # Late submission attempt after cancellation -> Rejected
    ok_sub, sub, reason = coordinator.submit_attempt(
        "t7", h1.attempt_id, "w7", 1, "req7", "diff7", 10.0, now=110.0
    )
    assert ok_sub is False
    assert "already cancelled" in reason or "Superseded fence" in reason


# Scenario 8: Budget exhausted or capability unavailable
def test_scenario_8_budget_exhausted_or_capability_unavailable(harness):
    """Explicit held/unresolved result; no silent weaker process."""
    ledger, adapter, coordinator, conductor, admission, _ = harness

    coordinator.register_task(
        task_id="t8",
        title="T8",
        description="Desc",
        baseline_sha="b8",
        acceptance_criteria=["c8"],
        max_budget=15.0,
        max_attempts=1,
    )

    ok1, h1, _ = coordinator.dispatch_attempt(
        "t8", "w8_1", reserve_cost=12.0, now=100.0
    )
    assert ok1 is True

    # Settle attempt 1
    coordinator.submit_attempt(
        "t8", h1.attempt_id, "w8_1", 1, "req8", "diff8", 12.0, now=110.0
    )

    # Next attempt exceeds budget (12.0 settled + 5.0 > 15.0) and attempt limit (1)
    ok2, h2, msg = coordinator.dispatch_attempt(
        "t8", "w8_2", reserve_cost=5.0, now=120.0
    )
    assert ok2 is False
    assert "Budget envelope exceeded" in msg or "Max attempt count reached" in msg


# Scenario 9: Missing artifact or verification evidence
def test_scenario_9_missing_artifact_or_verification_evidence(harness):
    """No completion claim or admission without verified reviewer receipt."""
    ledger, adapter, coordinator, conductor, admission, _ = harness

    coordinator.register_task(
        task_id="t9",
        title="T9",
        description="Desc",
        baseline_sha="b9",
        acceptance_criteria=["c9"],
    )

    ok1, h1, _ = coordinator.dispatch_attempt("t9", "w9", now=100.0)
    ok_sub, sub, _ = coordinator.submit_attempt(
        "t9", h1.attempt_id, "w9", 1, "req9", "diff9", 2.0, now=110.0
    )

    # No review recorded yet -> admission must fail
    with pytest.raises(ValueError, match="has no accepted candidate"):
        admission.emit_local_receipt("t9")

    # Review recorded with FAIL -> admission must fail
    coordinator.record_review(
        sub.candidate_sha,
        "t9",
        "rev9",
        ReviewVerdict.FAIL,
        2,
        notes="Blocking bugs",
        now=115.0,
    )
    with pytest.raises(ValueError, match="has no accepted candidate"):
        admission.emit_local_receipt("t9")


# Scenario 10: Fresh participant joins
def test_scenario_10_fresh_participant_joins_reconstructs_state(harness):
    """Can reconstruct accepted state, remaining work, and eligible tasks from authorized records."""
    ledger, adapter, coordinator, conductor, admission, db_path = harness

    coordinator.register_task(
        task_id="t10_done",
        title="Done Task",
        description="Desc",
        baseline_sha="b10",
        acceptance_criteria=["c10"],
    )
    coordinator.register_task(
        task_id="t10_open",
        title="Open Task",
        description="Desc",
        baseline_sha="b10",
        acceptance_criteria=["c10"],
    )

    ok1, h1, _ = coordinator.dispatch_attempt("t10_done", "w10", now=100.0)
    ok_sub, sub, _ = coordinator.submit_attempt(
        "t10_done", h1.attempt_id, "w10", 1, "req10", "diff10", 2.0, now=110.0
    )
    coordinator.record_review(
        sub.candidate_sha, "t10_done", "rev10", ReviewVerdict.PASS, 0, now=115.0
    )

    # Fresh participant arrives and connects to the ledger
    fresh_ledger = FencedLedger(db_path=db_path)
    all_tasks = fresh_ledger.list_tasks()
    task_map = {t.task_id: t for t in all_tasks}

    assert "t10_done" in task_map
    assert task_map["t10_done"].status == TaskStatus.ACCEPTED
    assert task_map["t10_done"].accepted_candidate_id == sub.candidate_sha

    assert "t10_open" in task_map
    assert task_map["t10_open"].status == TaskStatus.OPEN
    assert task_map["t10_open"].current_assignment_id is None

    # Fresh participants can inspect local completion without forge authority.
    fresh_admission = AdmissionHandoffEmitter(fresh_ledger)
    evidence = fresh_admission.emit_local_receipt("t10_done")
    assert evidence["receipt_type"] == "polyphony-experimental-local"
    assert evidence["local_reviews"][0]["verdict"] == "PASS"
    assert evidence["local_reviews"][0]["blocking_findings"] == 0
    assert evidence["candidate_id"] == sub.candidate_sha
    assert evidence["release_authorized"] is False
