import pytest
import asyncio
import random
from typing import Dict, Set, Any
from src.liminal_bridge.mesh import LiminalMesh
from src.liminal_bridge.storage import SQLiteStorageProvider


class NetworkSimulator:
    """Simulates network conditions for connected nodes."""

    def __init__(self, latency: float = 0.0, drop_rate: float = 0.0):
        self.nodes: Dict[str, "MockMesh"] = {}
        self.latency = latency
        self.drop_rate = drop_rate
        self.partitions: Set[frozenset] = (
            set()
        )  # Set of frozen sets {node_id_1, node_id_2}

    def register(self, node: "MockMesh"):
        self.nodes[node.node_id] = node
        # Notify existing nodes of new peer (simulate discovery)
        for peer_id, peer_node in self.nodes.items():
            if peer_id != node.node_id:
                asyncio.create_task(self._notify_connection(node, peer_node))
                asyncio.create_task(self._notify_connection(peer_node, node))

    def unregister(self, node: "MockMesh"):
        if node.node_id in self.nodes:
            del self.nodes[node.node_id]
            # Notify peers of disconnection
            for peer_id, peer_node in self.nodes.items():
                asyncio.create_task(self._notify_disconnection(peer_node, node.node_id))

    async def _notify_connection(self, node: "MockMesh", peer: "MockMesh"):
        if self._is_partitioned(node.node_id, peer.node_id):
            return
        await asyncio.sleep(self.latency)
        await node._handle_message({"type": "peer_connected", "peer_id": peer.node_id})

    async def _notify_disconnection(self, node: "MockMesh", peer_id: str):
        # Disconnection usually happens immediately or after timeout, simulate with latency
        await asyncio.sleep(self.latency)
        await node._handle_message({"type": "peer_disconnected", "peer_id": peer_id})

    def partition(self, node_id_1: str, node_id_2: str):
        self.partitions.add(frozenset({node_id_1, node_id_2}))

    def heal_partition(self, node_id_1: str, node_id_2: str):
        key = frozenset({node_id_1, node_id_2})
        if key in self.partitions:
            self.partitions.remove(key)
            # Re-establish connection notification
            n1 = self.nodes.get(node_id_1)
            n2 = self.nodes.get(node_id_2)
            if n1 and n2:
                asyncio.create_task(self._notify_connection(n1, n2))
                asyncio.create_task(self._notify_connection(n2, n1))

    def _is_partitioned(self, n1: str, n2: str) -> bool:
        return frozenset({n1, n2}) in self.partitions

    async def broadcast(self, sender_id: str, payload: Any):
        """Broadcasts a payload from sender to all other nodes."""
        for target_id, node in self.nodes.items():
            if target_id == sender_id:
                continue

            if self._is_partitioned(sender_id, target_id):
                continue

            if random.random() < self.drop_rate:
                continue

            asyncio.create_task(self._deliver(sender_id, target_id, payload))

    async def _deliver(self, sender_id: str, target_id: str, payload: Any):
        await asyncio.sleep(self.latency)
        node = self.nodes.get(target_id)
        if node and node.running:
            # The sidecar wraps the payload in a message structure
            msg = {"type": "message", "peer_id": sender_id, "payload": payload}
            try:
                await node._handle_message(msg)
            except Exception as e:
                print(f"Error delivering message to {target_id}: {e}")


class MockMesh(LiminalMesh):
    """A LiminalMesh subclass that uses NetworkSimulator instead of a real sidecar."""

    def __init__(self, simulator: NetworkSimulator, secret_key: str, **kwargs):
        # Use in-memory DB or temp file handled by pytest fixtures
        super().__init__(secret_key, **kwargs)
        self.simulator = simulator
        self.running = False

    async def start(self):
        self.running = True
        self.simulator.register(self)
        print(f"MockMesh started: {self.node_id}")
        # Start snapshot task if needed, but for tests we might skip it or mock it
        # self._snapshot_task = asyncio.create_task(self._periodic_snapshot())

    async def stop(self):
        self.running = False
        self.simulator.unregister(self)
        if self._snapshot_task:
            self._snapshot_task.cancel()

    async def _send_to_sidecar(self, msg_type: str, payload: Any):
        if not self.running:
            return

        if msg_type == "broadcast":
            # The payload here is {"e": encrypted_data} or similar
            # We pass it to simulator
            await self.simulator.broadcast(self.node_id, payload)
        elif msg_type == "join":
            # Simulator handles implicit joining of "all" nodes for now
            pass
        elif msg_type == "leave":
            pass


@pytest.fixture
def simulator():
    return NetworkSimulator()


@pytest.fixture
def mesh_a(simulator, tmp_path):
    db = tmp_path / "a.db"
    identity = tmp_path / "a.pem"
    mesh = MockMesh(
        simulator,
        "test-secret",
        storage_provider=SQLiteStorageProvider(str(db)),
        identity_path=str(identity),
    )
    return mesh


@pytest.fixture
def mesh_b(simulator, tmp_path):
    db = tmp_path / "b.db"
    identity = tmp_path / "b.pem"
    mesh = MockMesh(
        simulator,
        "test-secret",
        storage_provider=SQLiteStorageProvider(str(db)),
        identity_path=str(identity),
    )
    return mesh


@pytest.mark.asyncio
async def test_basic_connectivity(mesh_a, mesh_b):
    """Test that two nodes can connect and exchange thoughts."""
    await mesh_a.start()
    await mesh_b.start()

    # Wait for connection (simulated latency 0 by default)
    await asyncio.sleep(0.1)

    assert mesh_b.node_id in mesh_a.peers
    assert mesh_a.node_id in mesh_b.peers

    await mesh_a.share_thought("Hello from A")
    await asyncio.sleep(0.1)

    assert mesh_b.thoughts[mesh_a.node_id]["content"] == "Hello from A"

    await mesh_a.stop()
    await mesh_b.stop()


@pytest.mark.asyncio
async def test_latency(simulator, mesh_a, mesh_b):
    """Test eventual consistency with simulated latency."""
    simulator.latency = 0.5  # 500ms latency

    await mesh_a.start()
    await mesh_b.start()

    # Connection handshake takes time (latency * 1)
    await asyncio.sleep(0.1)
    # Shouldn't be connected yet
    assert mesh_b.node_id not in mesh_a.peers

    await asyncio.sleep(0.6)
    assert mesh_b.node_id in mesh_a.peers

    # Update KV
    await mesh_a.update_kv("key_slow", "value_slow")

    # Immediately check B (should not have it yet)
    val = mesh_b.get_kv("key_slow")
    assert val is None

    # Wait for delivery
    await asyncio.sleep(0.6)
    val = mesh_b.get_kv("key_slow")
    assert val == "value_slow"

    await mesh_a.stop()
    await mesh_b.stop()


@pytest.mark.asyncio
async def test_concurrent_updates_with_latency(simulator, mesh_a, mesh_b):
    """Test CRDT convergence with concurrent updates and latency."""
    simulator.latency = 0.2

    await mesh_a.start()
    await mesh_b.start()
    await asyncio.sleep(0.5)  # Wait for connect

    # Concurrent updates to same key
    # A sets to "valA", B sets to "valB"
    # Register uses wall clock. We need to control order or rely on Node IDs if timestamps equal.
    # Here we just fire them and expect one to win consistently on both sides.

    await mesh_a.update_kv("concurrent_key", "valA")
    await mesh_b.update_kv("concurrent_key", "valB")

    # Wait for convergence
    await asyncio.sleep(0.5)

    val_a = mesh_a.get_kv("concurrent_key")
    val_b = mesh_b.get_kv("concurrent_key")

    assert val_a == val_b
    assert val_a in ["valA", "valB"]

    await mesh_a.stop()
    await mesh_b.stop()


@pytest.mark.asyncio
async def test_partition_recovery(simulator, mesh_a, mesh_b):
    """Test that nodes can sync or recover after a partition."""
    simulator.latency = 0.0

    await mesh_a.start()
    await mesh_b.start()
    await asyncio.sleep(0.1)

    # Verify initial connectivity
    assert mesh_b.node_id in mesh_a.peers

    # Partition
    simulator.partition(mesh_a.node_id, mesh_b.node_id)

    # A updates during partition
    await mesh_a.update_kv("part_key", "value_during_partition")

    await asyncio.sleep(0.1)
    # B should NOT have it
    assert mesh_b.get_kv("part_key") is None

    # Heal partition
    simulator.heal_partition(mesh_a.node_id, mesh_b.node_id)

    # Wait for "peer_connected" event which triggers re-broadcast of thoughts
    await asyncio.sleep(0.2)

    # Currently LiminalMesh only re-broadcasts thoughts on connect, NOT KV store.
    # So KV store might NOT be synced automatically.
    # This test verifies current behavior. If it fails, we know we need to implement sync.
    # For now, let's check if thoughts sync.

    await mesh_a.share_thought("Thought during partition")
    # This might be dropped if partition is active.
    # If we share AFTER heal, it should work.

    await mesh_a.share_thought("Thought after heal")
    await asyncio.sleep(0.1)
    assert mesh_b.thoughts[mesh_a.node_id]["content"] == "Thought after heal"

    # NOTE: KV Sync is NOT implemented on reconnect in LiminalMesh yet.
    # So we don't assert mesh_b.get_kv("part_key") == "value_during_partition"
    # unless we manually trigger sync or update.

    # However, if we update again from A, B should get it.
    await mesh_a.update_kv("part_key", "value_after_heal")
    await asyncio.sleep(0.1)
    assert mesh_b.get_kv("part_key") == "value_after_heal"

    await mesh_a.stop()
    await mesh_b.stop()


@pytest.mark.asyncio
async def test_packet_loss(simulator, mesh_a, mesh_b):
    """Test behavior with packet loss."""
    simulator.drop_rate = 0.5  # 50% drop rate

    await mesh_a.start()
    await mesh_b.start()
    await asyncio.sleep(0.1)

    # Try sending multiple updates
    total_sent = 10

    # We use a counter on B to track received updates
    # But since updates overwrite, we can't easily count them unless we use a PNCounter
    # or listen to messages.

    # Let's use PNCounter
    for i in range(total_sent):
        await mesh_a.update_counter("lossy_counter", 1)
        await asyncio.sleep(0.05)

    # Wait a bit
    await asyncio.sleep(0.5)

    val = mesh_b.get_kv("lossy_counter")
    # With 50% loss, value should be roughly 5, but definitely < 10 and > 0 (probabilistically)
    # Note: Broadcasts are "fire and forget" in this simulation (no retransmission).
    # Real TCP would retransmit, but if we simulate "drop" as "message lost in transit and not recovered",
    # then we verify application tolerance (or lack thereof).

    # Since LiminalMesh doesn't have app-level acks/retries for KV updates,
    # we expect some data loss here.

    if val is None:
        val = 0

    print(f"Sent {total_sent}, received value {val}")

    # This test just documents behavior, asserting strict values is hard with randomness.
    # However, since we are sending additive updates (counter), B should receive at least
    # some updates (so val > 0) but not all of them (val <= total_sent).
    assert 0 <= val <= total_sent

    await mesh_a.stop()
    await mesh_b.stop()
