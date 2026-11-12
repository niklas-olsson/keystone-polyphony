"""Tests for Slice 3: Self-Organizing Conductor Loop, Advisory Nudges & Legacy Guardrails."""

import pytest
import tempfile
import os
import json
from src.core.collaboration.ledger import FencedLedger
from src.core.collaboration.adapter import DeterministicExecutionAdapter
from src.core.collaboration.coordinator import CollaborationCoordinator
from src.core.collaboration.conductor import EnsembleConductor, WorkerFlowManager
from src.core.collaboration.models import (
    TaskObjective,
    TaskStatus,
    ReviewVerdict,
    HostSignalType,
    HostSignalStatus,
    ConductorLeaseStatus,
)
from unittest.mock import AsyncMock, patch


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def setup_conductor(temp_db):
    ledger = FencedLedger(db_path=temp_db)
    adapter = DeterministicExecutionAdapter()
    coordinator = CollaborationCoordinator(ledger, adapter)
    conductor = EnsembleConductor(ledger, nudge_cooldown_seconds=10.0)
    return ledger, coordinator, conductor


def test_conductor_nudge_on_contention_and_worker_pivot(setup_conductor):
    """When a worker hits a contested task, Conductor emits a deduplicated nudge suggesting alternative work."""
    ledger, coordinator, conductor = setup_conductor

    # Register 2 tasks: task_A and task_B
    coordinator.register_task(
        task_id="task_A",
        title="Task A",
        description="Desc A",
        baseline_sha="baseA",
        acceptance_criteria=["crit A"],
    )
    coordinator.register_task(
        task_id="task_B",
        title="Task B",
        description="Desc B",
        baseline_sha="baseB",
        acceptance_criteria=["crit B"],
    )

    # Worker 1 acquires task_A
    ok1, h1, _ = coordinator.dispatch_attempt("task_A", "worker_1", now=100.0)
    assert ok1 is True

    # Worker 2 attempts task_A -> 409 Contested
    ok2, h2, reason2 = coordinator.dispatch_attempt("task_A", "worker_2", now=105.0)
    assert ok2 is False
    assert "Contested baton" in reason2

    # Worker 2 flow manager pivots
    flow_mgr = WorkerFlowManager("worker_2", conductor)
    pivot_target = flow_mgr.handle_contention("task_A", now=105.0)

    # Conductor nudged worker_2 to task_B
    assert pivot_target == "task_B"

    # Worker 2 now successfully claims task_B
    ok_pivot, h_pivot, _ = coordinator.dispatch_attempt(
        pivot_target, "worker_2", now=106.0
    )
    assert ok_pivot is True
    assert h_pivot.task_id == "task_B"

    # Cooldown test: Immediate repeated contention within 10s does not spam nudges
    nudge_spam = conductor.observe_contention_and_nudge("task_A", "worker_2", now=110.0)
    assert nudge_spam is None


@pytest.mark.asyncio
async def test_legacy_complete_task_guardrail_for_enrolled_tasks(temp_db, monkeypatch):
    """Legacy complete_task tool rejects unverified completion of enrolled tasks."""
    # Point LIMINAL_COLLAB_DB to temp_db
    monkeypatch.setenv("LIMINAL_COLLAB_DB", temp_db)

    # Import server
    from src.liminal_bridge import server

    ledger = FencedLedger(temp_db)
    coordinator = CollaborationCoordinator(ledger, server.collab_adapter)
    monkeypatch.setattr(server, "collab_ledger", ledger)
    monkeypatch.setattr(server, "collab_coordinator", coordinator)

    # Register enrolled task (schema_version = 2)
    server.collab_coordinator.register_task(
        task_id="task_enrolled_99",
        title="Enrolled Task",
        description="Must have accepted review",
        baseline_sha="base99",
        acceptance_criteria=["crit"],
    )

    # Mesh mock for complete_task
    mock_mesh = AsyncMock()
    mock_mesh.running = True
    mock_mesh.require_advisory_resource = lambda resource: None
    mock_mesh.release_baton = AsyncMock()
    mock_mesh.get_kv = lambda k: []
    mock_mesh.update_set = AsyncMock()
    mock_mesh.log_audit_event = AsyncMock()
    server.mesh = mock_mesh

    # Attempt to complete without accepted candidate receipt
    with patch.object(server, "ensure_mesh"), patch.object(
        server, "check_warmup", return_value=None
    ):
        msg = await server.complete_task("task_enrolled_99")
        assert "DENIED" in msg
        assert "Collaboration Protocol" in msg

    # Now simulate valid contribution and passing review
    ok_disp, h, _ = server.collab_coordinator.dispatch_attempt(
        "task_enrolled_99", "worker_alice", now=100.0
    )
    ok_sub, sub, _ = server.collab_coordinator.submit_attempt(
        "task_enrolled_99",
        h.attempt_id,
        "worker_alice",
        1,
        "req99",
        "diff",
        2.0,
        now=110.0,
    )
    ok_rev, _, _ = server.collab_coordinator.record_review(
        sub.candidate_sha,
        "task_enrolled_99",
        "reviewer_bob",
        ReviewVerdict.PASS,
        0,
        now=120.0,
    )
    assert ok_rev is True

    with patch.object(server, "ensure_mesh"), patch.object(
        server, "check_warmup", return_value=None
    ):
        msg_ok = await server.complete_task("task_enrolled_99")
        assert "completed" in msg_ok


# --- Slice 5 Unit Tests ---


def test_conductor_lease_chamber_mode_and_force_override(setup_conductor):
    """Chamber mode (< 3 active workers and no integrating/held tasks) rejects Conductor lease unless force_for_solo_deadlock=True."""
    ledger, coordinator, _ = setup_conductor

    # No tasks, 0 active workers -> rejection
    ok, lease, reason = ledger.acquire_conductor_lease("agent_idle", now=100.0)
    assert ok is False
    assert "Chamber mode" in reason

    # Force override for solo deadlock succeeds
    ok_forced, lease_forced, _ = ledger.acquire_conductor_lease(
        "agent_idle", force_for_solo_deadlock=True, now=100.0
    )
    assert ok_forced is True
    assert lease_forced is not None
    assert lease_forced.actor_id == "agent_idle"
    assert lease_forced.fence_token == 1


def test_conductor_lease_active_coding_assignment_barred(setup_conductor):
    """An agent holding an active coding assignment cannot claim the Conductor podium."""
    ledger, coordinator, _ = setup_conductor

    coordinator.register_task("t1", "T1", "Desc", "base", ["c"])
    # Agent claims t1
    ok, handle, _ = coordinator.dispatch_attempt("t1", "agent_coding", now=100.0)
    assert ok is True

    # agent_coding attempts to claim conductor lease -> rejected
    ok_cond, _, reason = ledger.acquire_conductor_lease(
        "agent_coding", force_for_solo_deadlock=True, now=101.0
    )
    assert ok_cond is False
    assert "active coding assignment" in reason


def test_conductor_lease_expiry_and_token_bump(setup_conductor):
    """Expired conductor lease allows another idle agent to claim with an incremented fence_token."""
    ledger, coordinator, _ = setup_conductor

    # 3 active workers to meet standard threshold
    for i in range(3):
        tid = f"task_{i}"
        coordinator.register_task(tid, tid, "Desc", "base", ["c"])
        coordinator.dispatch_attempt(tid, f"worker_{i}", now=100.0)

    # Agent A claims conductor lease with 90s TTL
    ok_a, lease_a, _ = ledger.acquire_conductor_lease(
        "agent_a", lease_ttl=90.0, now=100.0
    )
    assert ok_a is True
    assert lease_a.fence_token == 1
    assert lease_a.active_worker_count == 3

    # Agent B tries to claim at t=120 -> rejected
    ok_b_early, _, reason_b = ledger.acquire_conductor_lease("agent_b", now=120.0)
    assert ok_b_early is False
    assert "currently held by agent_a" in reason_b

    # At t=200 (> 190), lease has expired
    # get_conductor_lease should lazily expire it
    assert ledger.get_conductor_lease(now=200.0) is None

    # Agent B claims conductor lease -> succeeds with fence_token = 2
    ok_b, lease_b, _ = ledger.acquire_conductor_lease("agent_b", now=201.0)
    assert ok_b is True
    assert lease_b.actor_id == "agent_b"
    assert lease_b.fence_token == 2


def test_conductor_step_down_enables_coding(setup_conductor):
    """Stepping down releases conductor lease immediately so agent can claim coding assignments."""
    ledger, coordinator, _ = setup_conductor

    # Acquire conductor lease via force
    ok, lease, _ = ledger.acquire_conductor_lease(
        "agent_alice", force_for_solo_deadlock=True, now=100.0
    )
    assert ok is True

    # Alice steps down
    ok_step, _ = ledger.release_conductor_lease(
        "agent_alice", fence_token=lease.fence_token
    )
    assert ok_step is True

    # Now Alice can claim a coding task
    coordinator.register_task("t_code", "Code Task", "Desc", "base", ["c"])
    ok_claim, handle, _ = coordinator.dispatch_attempt(
        "t_code", "agent_alice", now=105.0
    )
    assert ok_claim is True
    assert handle.actor_id == "agent_alice"


def test_signal_emission_authorization_rules(setup_conductor):
    """Only active Conductor can emit SCALE_UP, while solo agent with candidate in INTEGRATING can emit SPAWN_REVIEWER."""
    ledger, coordinator, _ = setup_conductor

    coordinator.register_task("task_x", "Task X", "Desc", "base", ["c"])

    # Worker 1 submits candidate on task_x -> task moves to INTEGRATING
    ok, h, _ = coordinator.dispatch_attempt("task_x", "worker_1", now=100.0)
    ok_sub, sub, _ = coordinator.submit_attempt(
        "task_x", h.attempt_id, "worker_1", 1, "req1", "diff1", 5.0, now=110.0
    )
    assert ok_sub is True
    assert ledger.get_task("task_x").status == TaskStatus.INTEGRATING

    # Worker 1 is NOT conductor, tries to emit SCALE_UP -> rejected
    ok_scale, _, reason_scale = ledger.emit_host_signal(
        "worker_1",
        HostSignalType.SCALE_UP,
        requested_worker_delta=3,
        target_task_ids=["task_x"],
        rationale="Need more workers",
        now=115.0,
    )
    assert ok_scale is False
    assert "Only active Conductor" in reason_scale

    # Worker 1 emits SPAWN_REVIEWER -> authorized because worker_1 authored candidate on task_x which is INTEGRATING
    ok_spawn, sig_spawn, _ = ledger.emit_host_signal(
        "worker_1",
        HostSignalType.SPAWN_REVIEWER,
        requested_worker_delta=1,
        target_task_ids=["task_x"],
        rationale="Solo worker stuck awaiting independent reviewer",
        now=116.0,
    )
    assert ok_spawn is True
    assert sig_spawn.signal_type == HostSignalType.SPAWN_REVIEWER
    assert sig_spawn.emitted_by_actor == "worker_1"

    # Worker 2 (who did not author candidate) tries to emit SPAWN_REVIEWER -> rejected
    ok_spawn2, _, reason_spawn2 = ledger.emit_host_signal(
        "worker_2",
        HostSignalType.SPAWN_REVIEWER,
        requested_worker_delta=1,
        target_task_ids=["task_x"],
        rationale="Help review",
        now=117.0,
    )
    assert ok_spawn2 is False
    assert "no candidate awaiting review" in reason_spawn2


def test_host_signal_deduplication_and_unsettled_cost(setup_conductor):
    """Pending signals with identical type and sorted target tasks are deduplicated; snapshot computes global unsettled cost."""
    ledger, coordinator, _ = setup_conductor

    coordinator.register_task("t1", "T1", "Desc", "base", ["c"], max_budget=100.0)
    coordinator.register_task("t2", "T2", "Desc", "base", ["c"], max_budget=100.0)

    # Worker reserves 15 on t1, 20 on t2
    coordinator.dispatch_attempt("t1", "w1", reserve_cost=15.0, now=100.0)
    coordinator.dispatch_attempt("t2", "w2", reserve_cost=20.0, now=100.0)

    # Conductor claims podium
    ok, _, _ = ledger.acquire_conductor_lease(
        "conductor_bob", force_for_solo_deadlock=True, now=105.0
    )
    assert ok is True

    # Emit SCALE_UP
    ok1, sig1, msg1 = ledger.emit_host_signal(
        "conductor_bob",
        HostSignalType.SCALE_UP,
        requested_worker_delta=2,
        target_task_ids=["t2", "t1"],
        rationale="Burst cohort",
        now=110.0,
    )
    assert ok1 is True
    assert sig1.unsettled_cost_snapshot == 35.0  # 15 + 20
    assert sig1.target_task_ids == ["t1", "t2"]  # sorted

    # Immediate duplicate emission with shuffled target_task_ids returns existing receipt
    ok2, sig2, msg2 = ledger.emit_host_signal(
        "conductor_bob",
        HostSignalType.SCALE_UP,
        requested_worker_delta=2,
        target_task_ids=["t1", "t2"],
        rationale="Burst cohort duplicate",
        now=112.0,
    )
    assert ok2 is True
    assert "Deduplicated" in msg2
    assert sig2.signal_id == sig1.signal_id


@pytest.mark.asyncio
async def test_mcp_host_signal_dual_key_auth(temp_db, monkeypatch):
    """OpenJules endpoints enforce OPENJULES_HOST_KEY; swarm endpoints enforce SWARM_KEY."""
    from src.liminal_bridge import server

    monkeypatch.setenv("LIMINAL_COLLAB_DB", temp_db)
    test_swarm_key = "test_swarm_secret_key_123"
    test_host_key = "openjules_secret_host_key_456"

    monkeypatch.setattr(server, "SWARM_KEY", test_swarm_key)
    monkeypatch.setattr(server, "HOST_CONTROL_KEY", test_host_key)

    ledger = FencedLedger(temp_db)
    monkeypatch.setattr(server, "collab_ledger", ledger)

    # 1. claim_emergent_conductor with invalid SWARM_KEY -> rejected
    res_claim_bad = await server.claim_emergent_conductor("agent_1", "bad_swarm_key")
    data_bad = json.loads(res_claim_bad)
    assert data_bad["success"] is False
    assert "Unauthorized" in data_bad["error"]

    # 2. claim_emergent_conductor with valid SWARM_KEY (needs force or workers; let's create a task in INTEGRATING)
    ledger.create_task(
        TaskObjective(
            task_id="t_held",
            title="Held",
            description="",
            baseline_sha="b",
            acceptance_criteria=[],
            status=TaskStatus.INTEGRATING,
        )
    )
    res_claim_ok = await server.claim_emergent_conductor("agent_1", test_swarm_key)
    data_ok = json.loads(res_claim_ok)
    assert data_ok["success"] is True
    fence_token = data_ok["data"]["fence_token"]

    # 3. request_host_scaling via MCP
    res_scale = await server.request_host_scaling(
        actor_id="agent_1",
        signal_type="SCALE_UP",
        requested_worker_delta=3,
        target_task_ids=["t_held"],
        rationale="Scale up pool",
        swarm_key=test_swarm_key,
    )
    data_scale = json.loads(res_scale)
    assert data_scale["success"] is True
    signal_id = data_scale["data"]["signal_id"]

    # 4. openjules_poll_outbox with bad key or SWARM_KEY -> rejected
    res_poll_bad = await server.openjules_poll_outbox(host_key="wrong_key")
    assert json.loads(res_poll_bad)["success"] is False
    res_poll_swarm = await server.openjules_poll_outbox(host_key=test_swarm_key)
    assert json.loads(res_poll_swarm)["success"] is False

    # 5. openjules_poll_outbox with valid OPENJULES_HOST_KEY -> succeeds
    res_poll_ok = await server.openjules_poll_outbox(host_key=test_host_key)
    poll_data = json.loads(res_poll_ok)
    assert poll_data["success"] is True
    assert len(poll_data["data"]) == 1
    assert poll_data["data"][0]["signal_id"] == signal_id

    # 6. openjules_ack_signal with valid OPENJULES_HOST_KEY -> fulfills signal
    res_ack = await server.openjules_ack_signal(
        signal_id=signal_id,
        new_status="FULFILLED",
        response_note="Spawned 3 worker containers",
        host_key=test_host_key,
    )
    ack_data = json.loads(res_ack)
    assert ack_data["success"] is True

    # Verify outbox has 0 PENDING signals now
    res_poll_empty = await server.openjules_poll_outbox(host_key=test_host_key)
    assert len(json.loads(res_poll_empty)["data"]) == 0

    # 7. step_down_conductor via MCP
    res_step = await server.step_down_conductor(
        actor_id="agent_1", fence_token=fence_token, swarm_key=test_swarm_key
    )
    assert json.loads(res_step)["success"] is True


def test_host_signal_pruning(setup_conductor):
    """Pruning deletes fulfilled and rejected signals older than threshold."""
    ledger, coordinator, _ = setup_conductor

    # Force claim conductor lease
    ledger.acquire_conductor_lease(
        "cond_prune", force_for_solo_deadlock=True, now=100.0
    )

    # Emit signal at t=100
    ok, sig, _ = ledger.emit_host_signal(
        "cond_prune",
        HostSignalType.SCALE_UP,
        1,
        ["t1"],
        "scale",
        now=100.0,
    )
    assert ok is True

    # Acknowledge/fulfill at t=105
    ledger.acknowledge_host_signal(
        sig.signal_id, HostSignalStatus.FULFILLED, "done", now=105.0
    )

    # Prune with 60s retention at t=150 -> not pruned yet (150 - 105 = 45 < 60)
    del_early = ledger.prune_host_signals(older_than_seconds=60.0, now=150.0)
    assert del_early == 0

    # Prune with 60s retention at t=200 -> pruned! (200 - 105 = 95 > 60)
    del_late = ledger.prune_host_signals(older_than_seconds=60.0, now=200.0)
    assert del_late == 1

    # Outbox is now empty
    assert len(ledger.poll_host_signals(HostSignalStatus.FULFILLED)) == 0


def test_conductor_evaluate_and_act(setup_conductor):
    """EnsembleConductor.evaluate_and_act claims podium and emits appropriate signals."""
    ledger, coordinator, conductor = setup_conductor

    # Register 3 tasks to create backlog
    coordinator.register_task("t1", "T1", "Desc", "base", ["c"])
    coordinator.register_task("t2", "T2", "Desc", "base", ["c"])
    coordinator.register_task("t3", "T3", "Desc", "base", ["c"])

    # Worker w1 claims t1 and submits -> t1 enters INTEGRATING
    ok, h, _ = coordinator.dispatch_attempt("t1", "w1", now=100.0)
    coordinator.submit_attempt(
        "t1", h.attempt_id, "w1", 1, "req1", "diff1", 1.0, now=105.0
    )

    # evaluate_and_act by w_idle
    res = conductor.evaluate_and_act("w_idle", now=110.0)
    assert res["claimed_lease"] is True
    assert res["fence_token"] == 1
    # Emitted SPAWN_REVIEWER for t1 and SCALE_UP for open tasks
    signal_types = [s["signal_type"] for s in res["emitted_signals"]]
    assert HostSignalType.SPAWN_REVIEWER.value in signal_types
