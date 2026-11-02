import pytest
from unittest.mock import AsyncMock
from src.liminal_bridge.mesh import LiminalMesh
from src.liminal_bridge.storage import SQLiteStorageProvider
from src.liminal_bridge.crdt import Register, PNCounter, ORSet


@pytest.fixture
def mesh_a(tmp_path):
    db = tmp_path / "a.db"
    identity = tmp_path / "a.pem"
    mesh = LiminalMesh(
        "test-secret",
        storage_provider=SQLiteStorageProvider(str(db)),
        identity_path=str(identity),
    )
    mesh.broadcast = AsyncMock()
    return mesh


@pytest.fixture
def mesh_b(tmp_path):
    db = tmp_path / "b.db"
    identity = tmp_path / "b.pem"
    mesh = LiminalMesh(
        "test-secret",
        storage_provider=SQLiteStorageProvider(str(db)),
        identity_path=str(identity),
    )
    mesh.broadcast = AsyncMock()
    return mesh


@pytest.mark.asyncio
async def test_update_kv_lww(mesh_a):
    """Test standard KV update uses Register."""
    await mesh_a.update_kv("key1", "value1")

    val = mesh_a.get_kv("key1")
    assert val == "value1"

    # Check internal storage type
    internal = mesh_a.kv_store["key1"]
    assert isinstance(internal, Register)
    assert internal.value() == "value1"


@pytest.mark.asyncio
async def test_update_counter(mesh_a, mesh_b):
    """Test PNCounter updates and merging."""
    # A increments
    await mesh_a.update_counter("cnt", delta=10)
    assert mesh_a.get_kv("cnt") == 10

    # B receives update from A
    # Manually trigger receive on B
    # Payload sent by A would be:
    # { "type": "kv_update", "key": "cnt", "crdt": {type: "pn-counter", p: {A: 10}, n: {}}, origin: A, vc: {A:1} }

    crdt_payload = mesh_a.kv_store["cnt"].to_dict()

    msg_from_a = {
        "type": "kv_update",
        "key": "cnt",
        "crdt": crdt_payload,
        "origin": mesh_a.node_id,
        "vc": mesh_a.vector_clock,
    }

    await mesh_b._handle_payload(msg_from_a)
    assert mesh_b.get_kv("cnt") == 10

    # B increments
    await mesh_b.update_counter("cnt", delta=5)
    assert mesh_b.get_kv("cnt") == 15

    # A receives update from B
    crdt_payload_b = mesh_b.kv_store["cnt"].to_dict()
    msg_from_b = {
        "type": "kv_update",
        "key": "cnt",
        "crdt": crdt_payload_b,
        "origin": mesh_b.node_id,
        "vc": mesh_b.vector_clock,
    }

    await mesh_a._handle_payload(msg_from_b)
    assert mesh_a.get_kv("cnt") == 15


@pytest.mark.asyncio
async def test_update_set(mesh_a, mesh_b):
    """Test ORSet updates and merging."""
    # A adds 'apple'
    await mesh_a.update_set("fruits", "apple")
    assert mesh_a.get_kv("fruits") == {"apple"}

    # A adds 'banana'
    await mesh_a.update_set("fruits", "banana")
    assert mesh_a.get_kv("fruits") == {"apple", "banana"}

    # Simulate B receiving state from A
    crdt_payload = mesh_a.kv_store["fruits"].to_dict()
    msg_from_a = {
        "type": "kv_update",
        "key": "fruits",
        "crdt": crdt_payload,
        "origin": mesh_a.node_id,
        "vc": mesh_a.vector_clock,
    }

    await mesh_b._handle_payload(msg_from_a)
    assert mesh_b.get_kv("fruits") == {"apple", "banana"}

    # B removes 'apple'
    # Wait, update_set(key, element, remove=True)
    # But update_set signature in mesh.py:
    # async def update_set(self, key: str, element: Any, remove: bool = False):

    # However, ORSet needs to know which UUID to remove?
    # ORSet implementation: remove(element) removes ALL instances of element not in removed set.
    # So just passing element works.

    await mesh_b.update_set("fruits", "apple", remove=True)
    assert mesh_b.get_kv("fruits") == {"banana"}

    # A receives update from B
    crdt_payload_b = mesh_b.kv_store["fruits"].to_dict()
    msg_from_b = {
        "type": "kv_update",
        "key": "fruits",
        "crdt": crdt_payload_b,
        "origin": mesh_b.node_id,
        "vc": mesh_b.vector_clock,
    }

    await mesh_a._handle_payload(msg_from_b)
    assert mesh_a.get_kv("fruits") == {"banana"}

    # A adds 'apple' again (re-add)
    await mesh_a.update_set("fruits", "apple")
    assert mesh_a.get_kv("fruits") == {"banana", "apple"}


@pytest.mark.asyncio
async def test_persistence_crdt(mesh_a):
    """Test that CRDTs are persisted correctly."""
    await mesh_a.update_counter("cnt", 10)
    await mesh_a.update_set("set", "item")

    # Force save (update_counter/update_set calls save)

    # Create new mesh instance pointing to same DB
    mesh_new = LiminalMesh(
        "test-secret",
        storage_provider=SQLiteStorageProvider(mesh_a.storage.db_path),
        identity_path=mesh_a.identity_path,
    )

    assert mesh_new.get_kv("cnt") == 10
    assert mesh_new.get_kv("set") == {"item"}

    assert isinstance(mesh_new.kv_store["cnt"], PNCounter)
    assert isinstance(mesh_new.kv_store["set"], ORSet)
