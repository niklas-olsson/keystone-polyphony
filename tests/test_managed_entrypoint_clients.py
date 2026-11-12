"""Exercise public legacy clients against the managed-task refusal boundary."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.collaboration.adapter import DeterministicExecutionAdapter
from src.core.collaboration.conductor import EnsembleConductor
from src.core.collaboration.coordinator import CollaborationCoordinator
from src.core.collaboration.ledger import FencedLedger
from src.liminal_bridge import server
from src.liminal_bridge.crdt import ORSet
from src.liminal_bridge.dashboard import DashboardServer
from src.liminal_bridge.mesh import LiminalMesh
from src.liminal_bridge.storage import SQLiteStorageProvider

ROOT = Path(__file__).resolve().parents[1]
RESERVED = "task_v2_client_boundary"


def advisory_node(tmp_path, task_id=None):
    mesh = LiminalMesh(
        "managed-client-test-scope",
        storage_provider=SQLiteStorageProvider(":memory:"),
        identity_path=str(tmp_path / "identity.pem"),
        snapshot_path=str(tmp_path / "snapshot.json"),
    )
    if task_id:
        records = ORSet()
        # A managed copy must win over an ordinary-looking duplicate.
        records.add(json.dumps({"id": task_id, "status": "todo"}))
        records.add(json.dumps({"id": task_id, "schema_version": 2, "status": "todo"}))
        mesh.kv_store["swarm_backlog"] = records
        mesh._save_kv("swarm_backlog", records)
    return mesh


def task_cli(database, arguments):
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "TMPDIR", "LANG", "LC_ALL"}
    }
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "scripts/task.py", "--db", str(database), *arguments],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("operation", ["enroll", "claim", "submit", "review"])
def test_cli_refuses_reserved_mutations_before_database_or_input(tmp_path, operation):
    database = tmp_path / "must-not-exist.sqlite3"
    arguments = {
        "enroll": ["--title", "local", "--baseline-sha", "base", "--criteria", "c1"],
        "claim": ["--actor-id", "owner-labelled-caller"],
        "submit": [
            "--assignment-id",
            "unknown",
            "--actor-id",
            "owner-labelled-caller",
            "--fence",
            "1",
            "--diff-file",
            str(tmp_path / "must-not-be-read.diff"),
        ],
        "review": ["--candidate-id", "unknown", "--reviewer-id", "owner"],
    }
    result = task_cli(
        database, [operation, "--task-id", RESERVED, *arguments[operation]]
    )
    assert result.returncode != 0
    response = json.loads(result.stdout)
    assert response["success"] is False
    assert "authenticated coordinator" in response["reason"].lower()
    assert response["managed_authority"] is False
    assert response["release_authorized"] is False
    assert not database.exists()
    assert not (tmp_path / "must-not-be-read.diff").exists()


def test_cli_generated_task_stays_experimental_and_inspectable(tmp_path):
    database = tmp_path / "ordinary.sqlite3"
    created = task_cli(
        database,
        ["enroll", "--title", "local", "--baseline-sha", "base", "--criteria", "c1"],
    )
    assert created.returncode == 0, created.stderr
    task = json.loads(created.stdout)
    assert task["task_id"].startswith("experimental_")
    assert task["schema_version"] == 2
    listed = task_cli(database, ["list"])
    assert listed.returncode == 0, listed.stderr
    history = json.loads(listed.stdout)
    assert len(history) == 1
    # Creation and persisted timestamps need not be identical; the durable
    # identity, requirements and experimental budget must round-trip.
    for field in (
        "task_id",
        "schema_version",
        "title",
        "status",
        "baseline_sha",
        "acceptance_criteria",
        "envelope",
    ):
        assert history[0][field] == task[field]


@pytest.fixture
def experimental_clients(monkeypatch):
    ledger = FencedLedger()
    adapter = DeterministicExecutionAdapter()
    coordinator = CollaborationCoordinator(ledger, adapter)
    conductor = EnsembleConductor(ledger)
    monkeypatch.setattr(server, "collab_ledger", ledger)
    monkeypatch.setattr(server, "collab_coordinator", coordinator)
    monkeypatch.setattr(server, "collab_conductor", conductor)
    yield ledger, adapter, coordinator, conductor
    ledger._mem_conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("operation", ["create", "claim", "submit", "review"])
async def test_enrolled_mcp_refusal_has_no_local_effect(
    experimental_clients, monkeypatch, historical, operation
):
    ledger, adapter, coordinator, conductor = experimental_clients
    if historical:
        coordinator.register_task("experimental_seed", "old", "", "base", ["c1"])
        # Model a retained pre-upgrade row without admitting a new reserved ID.
        ledger._mem_conn.execute(
            "UPDATE tasks SET task_id = ? WHERE task_id = ?",
            (RESERVED, "experimental_seed"),
        )
        ledger._mem_conn.commit()
    before = tuple(ledger._mem_conn.iterdump())

    def no_contention_fallback(*args, **kwargs):
        pytest.fail("Managed refusal must not trigger a conductor nudge")

    monkeypatch.setattr(
        conductor, "observe_contention_and_nudge", no_contention_fallback
    )
    operations = {
        "create": (
            server.create_enrolled_task,
            dict(
                title="new",
                description="",
                baseline_sha="base",
                acceptance_criteria=["c1"],
            ),
        ),
        "claim": (server.claim_enrolled_task, dict(actor_id="owner-labelled-caller")),
        "submit": (
            server.submit_enrolled_contribution,
            dict(
                assignment_id="unknown",
                actor_id="owner-labelled-caller",
                fence_token=1,
                request_id="request",
                diff_text="untrusted contribution",
                actual_cost=1,
            ),
        ),
        "review": (
            server.evaluate_enrolled_candidate,
            dict(
                candidate_id="unknown",
                reviewer_actor_id="owner",
                verdict="PASS",
                blocking_findings=0,
            ),
        ),
    }
    function, arguments = operations[operation]
    response = json.loads(await function(task_id=RESERVED, **arguments))
    assert response["success"] is False
    assert "authenticated coordinator" in response["reason"].lower()
    assert response["managed_authority"] is False
    assert response["release_authorized"] is False
    assert response["identity_source"] == "caller-supplied"
    assert tuple(ledger._mem_conn.iterdump()) == before
    assert adapter.attempts == {}
    assert coordinator.active_handles == {}


@pytest.mark.asyncio
async def test_enrolled_mcp_keeps_ordinary_schema_two_tasks(experimental_clients):
    ledger, adapter, _, _ = experimental_clients
    task = json.loads(
        await server.create_enrolled_task(
            "experimental_client", "local", "", "base", ["c1"]
        )
    )
    assert task["schema_version"] == 2
    assert task["managed_authority"] is False
    claimed = json.loads(
        await server.claim_enrolled_task("experimental_client", "local-actor")
    )
    assert claimed["success"] is True
    assert claimed["managed_authority"] is False
    assert claimed["release_authorized"] is False
    assert claimed["identity_source"] == "caller-supplied"
    assert len(adapter.attempts) == 1
    assert (
        ledger.get_task("experimental_client").current_assignment_id
        == claimed["assignment_id"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["acquire_baton", "release_baton", "claim_task", "complete_task"]
)
@pytest.mark.parametrize("metadata_only", [False, True])
async def test_advisory_mcp_refuses_before_start_or_success_effects(
    tmp_path, monkeypatch, operation, metadata_only
):
    task_id = "ordinary-looking-managed-task" if metadata_only else RESERVED
    mesh = advisory_node(tmp_path, task_id if metadata_only else None)
    mesh.start = AsyncMock(
        side_effect=AssertionError("Refusal must precede mesh startup")
    )
    mesh._send_to_sidecar = AsyncMock()
    mesh.update_set = AsyncMock()
    mesh.log_audit_event = AsyncMock()
    mesh.on_baton_release = AsyncMock()
    monkeypatch.setattr(server, "mesh", mesh)
    monkeypatch.setattr(server, "check_warmup", lambda action: None)
    try:
        argument = f"task:{task_id}" if operation.endswith("baton") else task_id
        response = await getattr(server, operation)(argument)
        assert response.startswith("DENIED:")
        assert "authenticated coordinator" in response.lower()
        for method in (
            mesh.start,
            mesh._send_to_sidecar,
            mesh.update_set,
            mesh.log_audit_event,
            mesh.on_baton_release,
        ):
            method.assert_not_awaited()
        assert mesh.batons == {}
        assert mesh._lock_requests == {}
    finally:
        mesh.storage.conn.close()


@pytest.mark.parametrize("action", ["acquire", "release"])
def test_baton_cli_reserved_id_refuses_without_local_setup(tmp_path, action):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/baton.py"), action, f"task:{RESERVED}"],
        cwd=tmp_path,
        env={
            "PATH": str(tmp_path / "no-native-programs"),
            "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src"))),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stdout.startswith("DENIED:")
    assert "authenticated coordinator" in result.stdout.lower()
    assert "Traceback" not in result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_baton_cli_metadata_refusal_cleans_up_without_starting(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.syspath_prepend(str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location(
        "baton_client_boundary", ROOT / "scripts/baton.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    mesh = advisory_node(tmp_path, "retained-managed-task")
    before = tuple(mesh.storage.conn.iterdump())
    mesh.start = AsyncMock(side_effect=AssertionError("Must not start the native mesh"))
    mesh.stop = AsyncMock(wraps=mesh.stop)
    monkeypatch.setattr(module, "get_or_create_swarm_key", lambda: "fixed-test-scope")
    monkeypatch.setattr(module, "LiminalMesh", lambda **kwargs: mesh)
    try:
        assert await module.main(["acquire", "task:retained-managed-task"]) == 1
        assert "DENIED:" in capsys.readouterr().out
        mesh.start.assert_not_awaited()
        mesh.stop.assert_awaited_once()
        assert tuple(mesh.storage.conn.iterdump()) == before
        assert mesh.batons == {} and mesh._lock_requests == {}
    finally:
        mesh.storage.conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["acquire", "release", "claim", "complete"])
@pytest.mark.parametrize("metadata_only", [False, True])
async def test_dashboard_handlers_refuse_managed_task_effects(
    tmp_path, action, metadata_only
):
    """Handler fixtures exercise refusal separately from unchanged HTTP auth."""
    task_id = "metadata-managed-dashboard-task" if metadata_only else RESERVED
    mesh = advisory_node(tmp_path, task_id if metadata_only else None)
    mesh.broadcast = AsyncMock()
    mesh.update_set = AsyncMock()
    mesh.log_audit_event = AsyncMock()
    mesh.on_baton_release = AsyncMock()
    before = tuple(mesh.storage.conn.iterdump())
    if action in {"acquire", "release"}:
        handler = DashboardServer.handle_post_baton
        payload = {"action": action, "resource": f"task:{task_id}"}
    else:
        handler = DashboardServer.handle_post_backlog
        payload = {"action": action, "task_id": task_id}
    request = SimpleNamespace(json=AsyncMock(return_value=payload))
    try:
        response = await handler(SimpleNamespace(mesh=mesh), request)
        assert response.status == 409
        assert "authenticated coordinator" in json.loads(response.text)["error"].lower()
        assert tuple(mesh.storage.conn.iterdump()) == before
        for method in (
            mesh.broadcast,
            mesh.update_set,
            mesh.log_audit_event,
            mesh.on_baton_release,
        ):
            method.assert_not_awaited()
        assert mesh.batons == {} and mesh._lock_requests == {}
    finally:
        mesh.storage.conn.close()
