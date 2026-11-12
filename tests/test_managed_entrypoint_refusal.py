"""Deterministic legacy-boundary checks, not native transport or authority trials."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.collaboration.adapter import DeterministicExecutionAdapter
from src.core.collaboration.admission import ExperimentalReceiptExporter
from src.core.collaboration.conductor import EnsembleConductor
from src.core.collaboration.coordinator import CollaborationCoordinator
from src.core.collaboration.experimental_boundary import ManagedTaskRefusal
from src.core.collaboration.ledger import FencedLedger
from src.core.collaboration.models import (
    HostSignalStatus,
    HostSignalType,
    ReviewVerdict,
    TaskObjective,
)
from src.liminal_bridge.crdt import ORSet
from src.liminal_bridge.mesh import LiminalMesh
from src.liminal_bridge.pulse import Pulse
from src.liminal_bridge.storage import SQLiteStorageProvider


def task(task_id):
    return TaskObjective(task_id, "Local task", "Experiment", "base", ["check"])


def database_state(ledger):
    with sqlite3.connect(ledger.db_path) as connection:
        return tuple(connection.iterdump())


def make_historical(ledger, original, reserved):
    """Explicit fixture construction of rows written before namespace refusal."""
    with sqlite3.connect(ledger.db_path) as connection:
        for table in ("tasks", "assignments", "submissions", "candidates", "reviews"):
            connection.execute(
                f"UPDATE {table} SET task_id = ? WHERE task_id = ?",
                (reserved, original),
            )


@pytest.fixture
def history(tmp_path):
    ledger = FencedLedger(str(tmp_path / "experimental.db"))
    adapter = DeterministicExecutionAdapter()
    coordinator = CollaborationCoordinator(ledger, adapter)
    ledger.create_task(task("historical_seed"))
    ok, handle, _ = coordinator.dispatch_attempt("historical_seed", "author", now=100)
    assert ok
    ok, submission, _ = coordinator.submit_attempt(
        "historical_seed",
        handle.attempt_id,
        "author",
        handle.fence_token,
        "original-request",
        "original-diff",
        1,
        now=101,
    )
    assert ok
    ok, _, _ = coordinator.record_review(
        submission.candidate_sha,
        "historical_seed",
        "reviewer",
        ReviewVerdict.PASS,
        0,
        now=102,
    )
    assert ok
    make_historical(ledger, "historical_seed", "task_v2_history")
    return ledger, coordinator, handle, submission


@pytest.mark.parametrize("task_id", ["task_v2_history", "task_v2_unseen"])
def test_every_reserved_ledger_mutation_refuses_before_database_access(
    history, monkeypatch, task_id
):
    ledger, coordinator, handle, submission = history
    before = database_state(ledger)
    access = Mock(side_effect=AssertionError("refusal must precede a transaction"))
    monkeypatch.setattr(ledger, "_get_connection", access)
    with pytest.raises(ManagedTaskRefusal):
        ledger.create_task(task(task_id))
    results = [
        ledger.acquire_assignment(task_id, "forged-label"),
        ledger.submit_contribution(
            task_id,
            handle.attempt_id,
            "author",
            handle.fence_token,
            "original-request",
            "original-diff",
            1,
        ),
        ledger.cancel_assignment(task_id, handle.attempt_id),
        ledger.bump_spec_revision(task_id, ["changed"]),
        ledger.record_review(
            submission.candidate_sha, task_id, "forged-reviewer", ReviewVerdict.PASS, 0
        ),
        coordinator.dispatch_attempt(task_id, "forged-label"),
    ]
    assert all(result[0] is False and "coordinator" in result[-1] for result in results)
    access.assert_not_called()
    assert database_state(ledger) == before


def test_historical_reserved_rows_and_local_receipts_remain_inspectable(history):
    ledger, _, _, submission = history
    before = database_state(ledger)
    assert ledger.list_tasks()[0].task_id == "task_v2_history"
    assert (
        ledger.get_task("task_v2_history").accepted_candidate_id
        == submission.candidate_sha
    )
    receipt = ExperimentalReceiptExporter(ledger).emit_local_receipt("task_v2_history")
    assert receipt["candidate_id"] == submission.candidate_sha
    assert receipt["managed_authority"] is False
    assert receipt["release_authorized"] is False
    assert database_state(ledger) == before


@pytest.mark.parametrize("caller", ["task_v2_history", "experimental_wrong_caller"])
def test_retained_reserved_handle_cannot_be_cancelled_by_another_label(history, caller):
    ledger, coordinator, handle, _ = history
    retained = replace(handle, task_id="task_v2_history")
    coordinator.active_handles[handle.attempt_id] = retained
    coordinator.adapter.cancel = Mock(side_effect=AssertionError("adapter effect"))
    before = database_state(ledger)
    result = coordinator.cancel_attempt(caller, handle.attempt_id)
    assert result[:2] == (False, False)
    assert "coordinator" in result[2]
    coordinator.adapter.cancel.assert_not_called()
    assert coordinator.active_handles[handle.attempt_id] is retained
    assert database_state(ledger) == before


def test_reserved_history_does_not_become_conductor_work(history):
    ledger, _, _, _ = history
    for index in range(3):
        ledger.create_task(task(f"seed_{index}"))
        make_historical(ledger, f"seed_{index}", f"task_v2_open_{index}")
    ledger.create_task(task("ordinary"))
    conductor = EnsembleConductor(ledger)
    before = database_state(ledger)
    assert (
        conductor.observe_contention_and_nudge("task_v2_history", "actor", now=200)
        is None
    )
    assert conductor.propose_task_decomposition("task_v2_history") is None
    with pytest.raises(ManagedTaskRefusal):
        conductor.apply_decomposition("task_v2_history")
    assert conductor.contention_counts == {}
    assert database_state(ledger) == before
    nudge = conductor.observe_contention_and_nudge("unseen-local", "actor", now=200)
    assert nudge.suggested_alternative_task_ids == ["ordinary"]
    assert ledger.acquire_conductor_lease(
        "observer", force_for_solo_deadlock=True, now=200
    )[0]
    result = conductor.evaluate_and_act("observer", now=201)
    assert result["emitted_signals"] == []
    assert ledger.poll_host_signals() == []


@pytest.mark.parametrize("reserved_location", ["parent", "second-child"])
def test_cached_decomposition_refuses_before_any_child_or_parent_write(
    history, reserved_location
):
    ledger, _, _, _ = history
    ledger.create_task(task("ordinary_parent"))
    conductor = EnsembleConductor(ledger)
    conductor.decomposition_proposals["ordinary_parent"] = {
        "original_task_id": (
            "task_v2_history" if reserved_location == "parent" else "ordinary_parent"
        ),
        "subtask_ids": [
            "ordinary_first_child",
            (
                "task_v2_child"
                if reserved_location == "second-child"
                else "ordinary_second_child"
            ),
        ],
    }
    before = database_state(ledger)
    with pytest.raises(ManagedTaskRefusal):
        conductor.apply_decomposition("ordinary_parent")
    assert database_state(ledger) == before
    assert ledger.get_task("ordinary_first_child") is None


@pytest.mark.parametrize(
    "targets",
    [
        ["task_v2_unseen"],
        ["ordinary", "task_v2_unseen"],
        "task_v2_unseen",
        None,
        [False],
    ],
)
def test_host_signal_emission_refuses_reserved_or_malformed_targets_before_transaction(
    history, monkeypatch, targets
):
    ledger, _, _, _ = history
    before = database_state(ledger)
    access = Mock(side_effect=AssertionError("unexpected host effect"))
    monkeypatch.setattr(ledger, "_get_connection", access)
    result = ledger.emit_host_signal(
        "actor", HostSignalType.SCALE_UP, 1, targets, "reason"
    )
    assert result[0] is False
    assert result[1] is None
    access.assert_not_called()
    assert database_state(ledger) == before


@pytest.mark.parametrize(
    "raw_targets",
    [
        '["task_v2_history"]',
        '["ordinary","task_v2_history"]',
        "invalid-json",
        '"ordinary"',
        "null",
        "true",
        "{}",
        "[null]",
        '[""]',
    ],
)
def test_stored_host_signals_are_inspectable_but_not_executable(history, raw_targets):
    ledger, _, _, _ = history
    assert ledger.acquire_conductor_lease(
        "observer", force_for_solo_deadlock=True, now=200
    )[0]
    ok, signal, _ = ledger.emit_host_signal(
        "observer", HostSignalType.SCALE_UP, 1, ["ordinary"], "historical", now=201
    )
    assert ok
    with sqlite3.connect(ledger.db_path) as connection:
        connection.execute(
            "UPDATE host_signal_outbox SET target_task_ids = ? WHERE signal_id = ?",
            (raw_targets, signal.signal_id),
        )
    before = database_state(ledger)
    assert ledger.poll_host_signals()[0].signal_id == signal.signal_id
    assert ledger.poll_host_signals(executable_only=True) == []
    result = ledger.acknowledge_host_signal(
        signal.signal_id, HostSignalStatus.FULFILLED, "must not persist", now=202
    )
    assert result[0] is False and "inspection-only" in result[1]
    assert database_state(ledger) == before


@pytest.mark.parametrize("targets", [[], ["ordinary"]])
def test_ordinary_host_signals_keep_poll_and_acknowledgement(history, targets):
    ledger, _, _, _ = history
    assert ledger.acquire_conductor_lease(
        "observer", force_for_solo_deadlock=True, now=200
    )[0]
    ok, signal, _ = ledger.emit_host_signal(
        "observer", HostSignalType.SCALE_UP, 1, targets, "ordinary", now=201
    )
    assert ok
    assert (
        ledger.poll_host_signals(executable_only=True)[0].signal_id == signal.signal_id
    )
    assert ledger.acknowledge_host_signal(
        signal.signal_id, HostSignalStatus.FULFILLED, "done", now=202
    )[0]
    assert (
        ledger.poll_host_signals(HostSignalStatus.FULFILLED)[0].host_response_note
        == "done"
    )


@pytest.mark.parametrize("raw_targets", ["null", '"ab"'])
def test_new_host_signal_never_deduplicates_to_ineligible_history(history, raw_targets):
    ledger, _, _, _ = history
    assert ledger.acquire_conductor_lease(
        "observer", force_for_solo_deadlock=True, now=200
    )[0]
    ok, historical, _ = ledger.emit_host_signal(
        "observer", HostSignalType.SCALE_UP, 1, ["seed"], "historical", now=201
    )
    assert ok
    with sqlite3.connect(ledger.db_path) as connection:
        connection.execute(
            "UPDATE host_signal_outbox SET target_task_ids = ? WHERE signal_id = ?",
            (raw_targets, historical.signal_id),
        )
        original = connection.execute(
            "SELECT * FROM host_signal_outbox WHERE signal_id = ?",
            (historical.signal_id,),
        ).fetchone()
    ok, current, _ = ledger.emit_host_signal(
        "observer", HostSignalType.SCALE_UP, 1, ["a", "b"], "new request", now=202
    )
    assert ok and current.signal_id != historical.signal_id
    assert current.target_task_ids == ["a", "b"]
    assert [
        signal.signal_id for signal in ledger.poll_host_signals(executable_only=True)
    ] == [current.signal_id]
    with sqlite3.connect(ledger.db_path) as connection:
        retained = connection.execute(
            "SELECT * FROM host_signal_outbox WHERE signal_id = ?",
            (historical.signal_id,),
        ).fetchone()
    assert retained == original


@pytest.fixture
def mesh(tmp_path):
    instance = LiminalMesh(
        "refusal-test",
        storage_provider=SQLiteStorageProvider(":memory:"),
        identity_path=str(tmp_path / "identity.pem"),
        snapshot_path=str(tmp_path / "snapshot.json"),
    )
    instance.running = True
    instance.warmup_complete = True
    instance.broadcast = AsyncMock()
    instance.log_audit_event = AsyncMock()
    instance.on_baton_release = AsyncMock()
    return instance


def backlog(mesh, *rows):
    value = ORSet()
    for row in rows:
        value.add(row if isinstance(row, str) else json.dumps(row))
    mesh.kv_store["swarm_backlog"] = value
    mesh._save_kv("swarm_backlog", value)
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["acquire_baton", "release_baton"])
@pytest.mark.parametrize("self_held", [False, True])
async def test_reserved_baton_refuses_even_without_a_task_or_service(
    mesh, operation, self_held
):
    resource = "task:task_v2_unseen"
    if self_held:
        mesh.batons[resource] = mesh.node_id
    with pytest.raises(ManagedTaskRefusal):
        await getattr(mesh, operation)(resource)
    assert resource not in mesh.batons
    assert mesh._lock_requests == {}
    mesh.broadcast.assert_not_called()
    mesh.log_audit_event.assert_not_called()
    mesh.on_baton_release.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema", [2, 3, 0, -1, True, False, 1.0, "1", "2", None, [], {}]
)
async def test_all_matching_backlog_copies_deny_advisory_claims(mesh, schema):
    backlog(
        mesh,
        {"id": "marked", "status": "todo", "schema_version": 1},
        {"id": "marked", "status": "todo", "schema_version": schema},
        "{invalid",
        "null",
        {"id": "unrelated", "schema_version": None},
    )
    original = mesh.get_kv("swarm_backlog").copy()
    with pytest.raises(ManagedTaskRefusal):
        await mesh.acquire_baton("task:marked", timeout=0)
    with pytest.raises(ManagedTaskRefusal):
        await mesh.release_baton("task:marked")
    assert await mesh.autonomously_pick_task() is None
    assert mesh.get_kv("swarm_backlog") == original
    mesh.broadcast.assert_not_called()
    mesh.on_baton_release.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [{}, {"schema_version": 1}])
async def test_ordinary_tasks_and_file_locks_remain_usable(mesh, marker):
    backlog(
        mesh,
        {"id": "ordinary", "status": "todo", **marker},
        "{invalid",
        "null",
        {"id": "unrelated", "schema_version": None},
    )
    assert await mesh.acquire_baton("file.py", timeout=0)
    await mesh.release_baton("file.py")
    assert await mesh.acquire_baton("task:ordinary", timeout=0)
    # The existing self-held path makes the positive picker deterministic.
    picked = await mesh.autonomously_pick_task()
    assert picked["id"] == "ordinary" and picked["status"] == "in_progress"
    await mesh.release_baton("task:ordinary")
    assert mesh.batons == {}
    await asyncio.sleep(0)
    assert mesh.on_baton_release.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_type", ["baton_request", "baton_claim", "baton_release", "baton_deny"]
)
@pytest.mark.parametrize("resource", ["task:task_v2_unseen", "task:marked"])
async def test_incoming_managed_batons_never_assert_ownership(
    mesh, message_type, resource
):
    backlog(mesh, {"id": "marked", "schema_version": 2})
    future = asyncio.get_running_loop().create_future()
    mesh._lock_requests[resource] = future
    mesh.batons[resource] = mesh.node_id
    await mesh._handle_payload(
        {
            "type": message_type,
            "resource": resource,
            "origin": "peer",
            "target": mesh.node_id,
            "force": True,
        }
    )
    assert future.done() and future.result() is False
    assert resource not in mesh.batons and resource not in mesh._lock_requests
    mesh.broadcast.assert_not_called()
    mesh.on_baton_release.assert_not_called()


@pytest.mark.asyncio
async def test_same_welcome_metadata_precedes_baton_import_and_export(mesh):
    incoming = ORSet()
    incoming.add(json.dumps({"id": "marked", "schema_version": 2}))
    await mesh._handle_payload(
        {
            "type": "welcome",
            "origin": "peer",
            "batons": {
                "task:marked": "peer",
                "task:task_v2_x": "peer",
                "file.py": "peer",
            },
            "kv": {"swarm_backlog": incoming.to_dict()},
        }
    )
    assert mesh.batons == {"file.py": "peer"}
    assert json.loads(next(iter(mesh.get_kv("swarm_backlog"))))["id"] == "marked"
    mesh.batons["task:marked"] = "peer"
    await mesh._push_welcome_to_peer("next-peer")
    assert mesh.broadcast.call_args.args[0]["batons"] == {"file.py": "peer"}
    mesh.batons["task:task_v2_x"] = "peer"
    await mesh._handle_payload({"type": "gossip_request", "origin": "peer"})
    assert mesh.broadcast.call_args.args[0]["batons"] == {"file.py": "peer"}
    mesh._save_snapshot()
    with open(mesh.snapshot_path) as stream:
        assert json.load(stream)["batons"] == {"file.py": "peer"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["request-await", "response-await"])
async def test_metadata_arriving_during_acquisition_refuses_without_success(
    mesh, stage
):
    backlog(mesh, {"id": "changing", "status": "todo"})
    requested = asyncio.Event()
    messages = []

    async def broadcast(payload, **_):
        messages.append(payload["type"])
        if payload["type"] == "baton_request":
            requested.set()
            if stage == "request-await":
                backlog(mesh, {"id": "changing", "schema_version": 2})

    mesh.broadcast.side_effect = broadcast
    operation = asyncio.create_task(mesh.acquire_baton("task:changing", timeout=30))
    await requested.wait()
    if stage == "response-await":
        backlog(mesh, {"id": "changing", "schema_version": 2})
    with pytest.raises(ManagedTaskRefusal):
        await operation
    assert messages == ["baton_request"]
    assert mesh.batons == {} and mesh._lock_requests == {}
    mesh.log_audit_event.assert_not_called()
    mesh.on_baton_release.assert_not_called()


@pytest.mark.asyncio
async def test_refused_older_acquire_does_not_delete_or_resolve_a_newer_waiter(mesh):
    backlog(mesh, {"id": "changing", "status": "todo"})
    newer = asyncio.get_running_loop().create_future()
    older = None

    async def broadcast(payload, **_):
        nonlocal older
        older = mesh._lock_requests["task:changing"]
        backlog(mesh, {"id": "changing", "schema_version": 2})
        assert older.done() and older.result() is False
        mesh._lock_requests["task:changing"] = newer

    mesh.broadcast.side_effect = broadcast
    with pytest.raises(ManagedTaskRefusal):
        await mesh.acquire_baton("task:changing")
    assert mesh._lock_requests["task:changing"] is newer
    assert not newer.done()
    assert older.result() is False
    newer.cancel()
    mesh._lock_requests.clear()


@pytest.mark.asyncio
async def test_ordinary_older_acquire_cleanup_preserves_replacement_waiter(mesh):
    newer = asyncio.get_running_loop().create_future()

    async def broadcast(payload, **_):
        older = mesh._lock_requests["file.py"]
        mesh._lock_requests["file.py"] = newer
        older.set_result(False)

    mesh.broadcast.side_effect = broadcast
    assert await mesh.acquire_baton("file.py") is False
    assert mesh._lock_requests["file.py"] is newer and not newer.done()
    newer.cancel()
    mesh._lock_requests.clear()


@pytest.mark.asyncio
async def test_duplicate_denial_does_not_resolve_an_already_done_future(mesh):
    future = asyncio.get_running_loop().create_future()
    future.set_result(False)
    mesh._lock_requests["file.py"] = future
    await mesh._handle_payload(
        {
            "type": "baton_deny",
            "origin": "peer",
            "resource": "file.py",
            "target": mesh.node_id,
        }
    )
    assert future.result() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_type", [[], {}, ["baton_claim"], {"name": "baton_claim"}]
)
async def test_unknown_nonstring_message_types_keep_the_existing_ignored_behavior(
    mesh, message_type
):
    await mesh._handle_payload({"type": message_type, "origin": "peer"})
    assert mesh.batons == {} and mesh._lock_requests == {}
    mesh.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_pulse_rechecks_stale_snapshot_after_each_ordinary_release_await(mesh):
    backlog(mesh, {"id": "changing", "status": "todo"})
    mesh.batons.update(
        {
            "first.py": "peer",
            "task:changing": "peer",
            "last.py": "peer",
        }
    )
    mesh.thoughts["peer"] = {"timestamp": 0}
    sent = []

    async def broadcast(payload, **_):
        sent.append(payload["resource"])
        if payload["resource"] == "first.py":
            backlog(mesh, {"id": "changing", "schema_version": 2})

    mesh.broadcast.side_effect = broadcast
    pulse = object.__new__(Pulse)
    pulse.mesh = mesh
    await pulse.check_baton_health()
    assert sent == ["first.py", "last.py"]
    assert "task:changing" not in mesh.batons
