"""Tests for Slice 1: Protocol Core, SQLite WAL Fenced State Engine & Deterministic Adapter."""

import pytest
import tempfile
import os
import sqlite3
from src.core.collaboration.models import (
    TaskObjective,
    TaskStatus,
    ResourceEnvelope,
    AttemptStatus,
)
from src.core.collaboration.ledger import FencedLedger
from src.core.collaboration.adapter import (
    DeterministicExecutionAdapter,
    AttemptLimits,
)
from src.core.collaboration.coordinator import CollaborationCoordinator


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def ledger(temp_db):
    return FencedLedger(db_path=temp_db)


@pytest.fixture
def coordinator(ledger):
    adapter = DeterministicExecutionAdapter()
    return CollaborationCoordinator(ledger=ledger, execution_adapter=adapter)


def test_sqlite_wal_mode_and_tables(temp_db, ledger):
    """Verify that ledger creates the tables and sets WAL mode."""
    conn = sqlite3.connect(temp_db)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode;")
    mode = cursor.fetchone()[0]
    assert mode.lower() == "wal"

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {row[0] for row in cursor.fetchall()}
    assert "tasks" in tables
    assert "assignments" in tables
    assert "submissions" in tables
    assert "candidates" in tables
    assert "reviews" in tables
    conn.close()


def test_atomic_assignment_and_fencing(coordinator, ledger):
    """Verify monotonic fencing tokens and atomic assignment."""
    task = coordinator.register_task(
        task_id="experimental_1",
        title="Test Task",
        description="Task desc",
        baseline_sha="abc1234",
        acceptance_criteria=["criterion 1"],
        max_budget=50.0,
        max_attempts=3,
    )
    assert task.current_fence_token == 0
    assert task.status == TaskStatus.OPEN

    # Worker A acquires assignment
    ok, handle_a, msg = coordinator.dispatch_attempt(
        task_id="experimental_1",
        actor_id="worker_a",
        reserve_cost=10.0,
        lease_duration=100.0,
        now=1000.0,
    )
    assert ok is True
    assert handle_a.fence_token == 1
    assert handle_a.actor_id == "worker_a"

    task_after = ledger.get_task("experimental_1")
    assert task_after.current_fence_token == 1
    assert task_after.envelope.in_flight_reserved_budget == 10.0
    assert task_after.envelope.settled_budget == 0.0

    # Worker B tries to acquire while A's lease is still active -> Contested baton (409 Conflict)
    ok_b, handle_b, msg_b = coordinator.dispatch_attempt(
        task_id="experimental_1",
        actor_id="worker_b",
        reserve_cost=10.0,
        lease_duration=100.0,
        now=1050.0,  # within A's lease (1000 + 100 = 1100)
    )
    assert ok_b is False
    assert handle_b is None
    assert "Contested baton" in msg_b


def test_expired_lease_reassignment_and_superseded_submission(coordinator, ledger):
    """When a worker lease expires, a replacement receives a higher fence, and late worker is rejected."""
    coordinator.register_task(
        task_id="experimental_2",
        title="Expiring Task",
        description="Desc",
        baseline_sha="base_sha",
        acceptance_criteria=["crit"],
    )

    # Worker A acquires at t=1000 with lease duration 50s (expires t=1050)
    ok_a, handle_a, _ = coordinator.dispatch_attempt(
        task_id="experimental_2",
        actor_id="worker_a",
        reserve_cost=10.0,
        lease_duration=50.0,
        now=1000.0,
    )
    assert ok_a is True
    assert handle_a.fence_token == 1

    # At t=1060, lease has expired. Worker B acquires reassignment -> Fence 2
    ok_b, handle_b, _ = coordinator.dispatch_attempt(
        task_id="experimental_2",
        actor_id="worker_b",
        reserve_cost=10.0,
        lease_duration=50.0,
        now=1060.0,
    )
    assert ok_b is True
    assert handle_b.fence_token == 2

    # Worker A now returns late with fence 1 -> Must be rejected atomically
    ok_sub_a, sub_a, reason_a = coordinator.submit_attempt(
        task_id="experimental_2",
        assignment_id=handle_a.attempt_id,
        actor_id="worker_a",
        fence_token=1,
        request_id="req_a_1",
        diff_text="patch A",
        actual_cost=8.0,
        now=1070.0,
    )
    assert ok_sub_a is False
    assert sub_a is None
    assert "Superseded fence" in reason_a

    # Worker B submits with fence 2 -> Accepted
    ok_sub_b, sub_b, reason_b = coordinator.submit_attempt(
        task_id="experimental_2",
        assignment_id=handle_b.attempt_id,
        actor_id="worker_b",
        fence_token=2,
        request_id="req_b_1",
        diff_text="patch B",
        actual_cost=7.5,
        now=1075.0,
    )
    assert ok_sub_b is True
    assert sub_b is not None
    assert sub_b.actual_cost == 7.5

    # Check budget settled properly (reserved released, actual cost accounted)
    task = ledger.get_task("experimental_2")
    # Worker A reserved 10.0 (still reserved since A never cleanly settled or was superseded on submit)
    # Worker B reserved 10.0 and settled 7.5 -> in_flight for B is 0.0, settled is 7.5
    assert task.envelope.settled_budget == 7.5


def test_idempotent_submission_retry(coordinator, ledger):
    """An identical retry returns the same receipt; conflicting payload with reused request_id is rejected."""
    coordinator.register_task(
        task_id="experimental_3",
        title="Retry Task",
        description="Desc",
        baseline_sha="base_3",
        acceptance_criteria=["crit"],
    )

    ok, handle, _ = coordinator.dispatch_attempt(
        task_id="experimental_3",
        actor_id="worker_c",
        reserve_cost=10.0,
        lease_duration=100.0,
        now=1000.0,
    )

    # First submission
    ok_sub1, sub1, msg1 = coordinator.submit_attempt(
        task_id="experimental_3",
        assignment_id=handle.attempt_id,
        actor_id="worker_c",
        fence_token=1,
        request_id="req_unique_123",
        diff_text="diff content",
        actual_cost=5.0,
        now=1010.0,
    )
    assert ok_sub1 is True
    assert sub1 is not None

    # Lost response: Identical retry
    ok_retry, sub_retry, msg_retry = coordinator.submit_attempt(
        task_id="experimental_3",
        assignment_id=handle.attempt_id,
        actor_id="worker_c",
        fence_token=1,
        request_id="req_unique_123",
        diff_text="diff content",
        actual_cost=5.0,
        now=1015.0,
    )
    assert ok_retry is True
    assert sub_retry.receipt_id == sub1.receipt_id
    assert "Idempotent retry" in msg_retry

    # Malicious or conflicting retry with reused request_id but different diff
    ok_bad, sub_bad, msg_bad = coordinator.submit_attempt(
        task_id="experimental_3",
        assignment_id=handle.attempt_id,
        actor_id="worker_c",
        fence_token=1,
        request_id="req_unique_123",
        diff_text="tampered diff content",
        actual_cost=5.0,
        now=1020.0,
    )
    assert ok_bad is False
    assert sub_bad is None
    assert "Reused request_id with conflicting payload" in msg_bad


def test_budget_exhaustion_blocks_dispatch(coordinator, ledger):
    """Verify that exceeding task envelope max_budget or max_attempts rejects new assignments."""
    coordinator.register_task(
        task_id="experimental_budget",
        title="Budget Limited Task",
        description="Desc",
        baseline_sha="base_budget",
        acceptance_criteria=["crit"],
        max_budget=25.0,
        max_attempts=2,
    )

    # Attempt 1: reserve 15.0
    ok1, h1, _ = coordinator.dispatch_attempt(
        task_id="experimental_budget", actor_id="w1", reserve_cost=15.0, now=100.0
    )
    assert ok1 is True

    # Settle attempt 1 with cost 12.0
    coordinator.submit_attempt(
        task_id="experimental_budget",
        assignment_id=h1.attempt_id,
        actor_id="w1",
        fence_token=1,
        request_id="req_1",
        diff_text="d1",
        actual_cost=12.0,
        now=110.0,
    )

    # Attempt 2: reserve 10.0 (12.0 settled + 10.0 reserved = 22.0 <= 25.0) -> OK
    ok2, h2, _ = coordinator.dispatch_attempt(
        task_id="experimental_budget", actor_id="w2", reserve_cost=10.0, now=120.0
    )
    assert ok2 is True

    # Cancel attempt 2
    coordinator.cancel_attempt(
        task_id="experimental_budget", assignment_id=h2.attempt_id, now=125.0
    )

    # Attempt 3: max_attempts is 2 -> attempt count is now 2 -> Attempt 3 rejected
    ok3, h3, msg3 = coordinator.dispatch_attempt(
        task_id="experimental_budget", actor_id="w3", reserve_cost=5.0, now=130.0
    )
    assert ok3 is False
    assert "Max attempt count reached" in msg3
