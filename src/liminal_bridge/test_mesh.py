from src.liminal_bridge.storage import SQLiteStorageProvider
import pytest
import asyncio
import json
from unittest.mock import AsyncMock
from src.liminal_bridge.mesh import LiminalMesh


@pytest.fixture
def mesh(tmp_path):
    """Fixture to create a LiminalMesh with temporary DB and Identity."""
    db = tmp_path / "test.db"
    identity = tmp_path / "test.pem"
    mesh = LiminalMesh("test-secret", storage_provider=SQLiteStorageProvider(str(db)), identity_path=str(identity))
    mesh.broadcast = AsyncMock()
    yield mesh
    # Cleanup is handled by tmp_path, but stop() closes DB connection
    # Since we don't start() the mesh in these unit tests, stop() isn't strictly needed for process
    # but good to close DB connection if needed.
    if hasattr(mesh, "conn") and mesh.conn:
        mesh.conn.close()


@pytest.mark.asyncio
async def test_mesh_baton_local(mesh):
    """Test local baton acquisition logic."""
    # Acquire new baton
    success = await mesh.acquire_baton("file.py", timeout=0.1)
    assert success is True
    assert mesh.batons["file.py"] == mesh.node_id

    # Re-acquire same baton (idempotent)
    success = await mesh.acquire_baton("file.py", timeout=0.1)
    assert success is True

    # Release baton
    await mesh.release_baton("file.py")
    assert "file.py" not in mesh.batons


@pytest.mark.asyncio
async def test_mesh_baton_denial(mesh):
    """Test baton denial when another peer holds it."""
    # Simulate another peer holding the lock
    mesh.batons["file.py"] = "other-peer-id"

    # Try to acquire
    success = await mesh.acquire_baton("file.py", timeout=0.1)
    assert success is False


@pytest.mark.asyncio
async def test_kv_store(mesh):
    """Test KV store updates."""
    await mesh.update_kv("key1", "value1")
    assert mesh.get_kv("key1") == "value1"

    # Verify broadcast call using ANY for the timestamp
    call_args = mesh.broadcast.call_args[0][0]
    assert call_args["type"] == "kv_update"
    assert call_args["key"] == "key1"
    assert "crdt" in call_args
    assert call_args["crdt"]["value"] == "value1"
    # vc is inside crdt, and also added by broadcast() which is mocked here
    assert "vc" in call_args["crdt"]


@pytest.mark.asyncio
async def test_snapshot_creation(tmp_path):
    """Test manual snapshot creation."""
    db = tmp_path / "test.db"
    identity = tmp_path / "test.pem"
    snapshot = tmp_path / "snapshot.json"

    mesh = LiminalMesh(
        "test-secret",
        storage_provider=SQLiteStorageProvider(str(db)),
        identity_path=str(identity),
        snapshot_path=str(snapshot),
    )
    mesh.broadcast = AsyncMock()

    # Add some data
    await mesh.update_kv("key1", "value1")
    await mesh.share_thought("hello world")

    # Save snapshot
    mesh._save_snapshot()

    assert snapshot.exists()

    with open(snapshot, "r") as f:
        data = json.load(f)

    assert data["node_id"] == mesh.node_id
    assert data["kv_store"]["key1"]["value"] == "value1"
    assert data["thoughts"][mesh.node_id]["content"] == "hello world"


@pytest.mark.asyncio
async def test_periodic_snapshot_task(tmp_path):
    """Test that the snapshot task runs periodically."""
    db = tmp_path / "test.db"
    identity = tmp_path / "test.pem"
    snapshot = tmp_path / "snapshot.json"

    # Set a very short interval
    mesh = LiminalMesh(
        "test-secret",
        storage_provider=SQLiteStorageProvider(str(db)),
        identity_path=str(identity),
        snapshot_path=str(snapshot),
        snapshot_interval=0.1,
    )

    # Manually start the task without starting the full mesh/sidecar
    mesh.running = True
    task = asyncio.create_task(mesh._periodic_snapshot())

    # Wait long enough for a snapshot to happen
    await asyncio.sleep(0.15)

    # Stop
    mesh.running = False
    await task

    assert snapshot.exists()
