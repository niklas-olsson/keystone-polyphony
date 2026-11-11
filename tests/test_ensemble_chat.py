"""Strict conversation checks, including mandatory real transport qualification in CI."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from src.liminal_bridge.mesh import LiminalMesh
from src.liminal_bridge.storage import SQLiteStorageProvider

ROOT = Path(__file__).resolve().parents[1]


def make_node(directory, name, bootstrap=None):
    return LiminalMesh(
        "isolated-real-conversation-qualification",
        storage_provider=SQLiteStorageProvider(str(directory / (name + ".db"))),
        identity_path=str(directory / (name + ".pem")),
        bootstrap=bootstrap,
        snapshot_path=str(directory / (name + ".snapshot.json")),
    )


def messages(mesh):
    return mesh.conversation.store.history("qualified-peer-discussion")["messages"]


async def wait_for(predicate, description, seconds=35):
    try:
        async with asyncio.timeout(seconds):
            while not predicate():
                await asyncio.sleep(0.05)
    except TimeoutError:
        raise AssertionError(description) from None


@pytest.mark.asyncio
async def test_signed_transport_fixture_requires_both_contributions(tmp_path):
    """The in-process transport is explicit; signatures and stores are real."""
    a, b = make_node(tmp_path, "a"), make_node(tmp_path, "b")
    queue = []
    for sender, receiver in ((a, b), (b, a)):

        async def send(operation, envelope, sender=sender, receiver=receiver):
            assert operation == "broadcast"
            queue.append(
                (
                    receiver,
                    {
                        "type": "message",
                        "peer_id": sender.node_id,
                        "payload": json.loads(json.dumps(envelope)),
                    },
                )
            )

        sender._send_to_sidecar = send
    first = await a.conversation.say(
        "qualified-peer-discussion", "proposal", request_key="proposal"
    )
    second = await b.conversation.say(
        "qualified-peer-discussion",
        "disagreement",
        reply_to=first["message_id"],
        request_key="criticism",
    )
    while queue:
        receiver, payload = queue.pop()
        await receiver._handle_message(payload)
    for mesh in (a, b):
        actual = messages(mesh)
        assert {item["record"]["message_id"] for item in actual} == {
            first["message_id"],
            second["message_id"],
        }
        assert {item["record"]["body"]["author"] for item in actual} == {
            a.node_id,
            b.node_id,
        }
        reply = next(
            item
            for item in actual
            if item["record"]["message_id"] == second["message_id"]
        )
        assert reply["reply_state"] == "resolved"
    a.storage.conn.close()
    b.storage.conn.close()


@pytest.mark.asyncio
async def test_missing_discovery_cannot_report_integration_success():
    with pytest.raises(AssertionError, match="discovery must succeed"):
        await wait_for(lambda: False, "discovery must succeed", seconds=0.01)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("POLYPHONY_REAL_CHAT_TESTS") != "1",
    reason="real Hyperswarm qualification is explicitly enabled and mandatory in CI",
)
async def test_real_two_node_hyperswarm_conversation_and_restart(tmp_path):
    """No transport stubs or public DHT: two real sidecars and a local bootstrap."""
    bootstrap = await asyncio.create_subprocess_exec(
        "node",
        str(ROOT / "src/liminal_bridge/sidecar/verify-bootstrap.js"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    nodes = []
    try:
        line = await asyncio.wait_for(bootstrap.stdout.readline(), 15)
        endpoint = line.decode().strip()
        if not endpoint.startswith("127.0.0.1:"):
            error = (await bootstrap.stderr.read(4096)).decode(errors="replace")
            pytest.fail("real bootstrap did not start: " + error)
        for name in ("alice", "bob"):
            mesh = make_node(tmp_path, name, endpoint)
            mesh._gossip_interval_seconds = 0.25
            nodes.append(mesh)
            await mesh.start()
        a, b = nodes
        assert a.node_id != b.node_id
        await wait_for(
            lambda: a.peers
            and b.peers
            and b.node_id in a.peer_map.values()
            and a.node_id in b.peer_map.values(),
            "both real nodes must discover and authenticate each other",
        )
        first = await a.conversation.say(
            "qualified-peer-discussion", "proposal", request_key="proposal"
        )
        await wait_for(
            lambda: len(messages(b)) == 1, "proposal must reach the second node"
        )
        second = await b.conversation.say(
            "qualified-peer-discussion",
            "I disagree",
            request_key="criticism",
            reply_to=first["message_id"],
        )
        expected = {first["message_id"], second["message_id"]}
        await wait_for(
            lambda: all(
                {item["record"]["message_id"] for item in messages(mesh)} == expected
                for mesh in nodes
            ),
            "both real nodes must retain both original contributions",
        )
        for mesh in nodes:
            reply = next(
                item
                for item in messages(mesh)
                if item["record"]["message_id"] == second["message_id"]
            )
            assert reply["reply_state"] == "resolved"
            assert reply["record"]["body"]["author"] == b.node_id
        stream = b.conversation.store.stream_id
        await b.stop()
        b.storage.conn.close()
        await wait_for(lambda: not a.peers, "disconnect must be observed")
        third = await a.conversation.say(
            "qualified-peer-discussion",
            "follow-up while peer is offline",
            request_key="follow-up",
            reply_to=second["message_id"],
        )
        expected.add(third["message_id"])
        restored = make_node(tmp_path, "bob", endpoint)
        restored._gossip_interval_seconds = 0.25
        nodes[1] = restored
        assert (
            restored.node_id == b.node_id
            and restored.conversation.store.stream_id == stream
        )
        await restored.start()
        await wait_for(
            lambda: restored.peers
            and {item["record"]["message_id"] for item in messages(restored)}
            == expected,
            "restarted peer must recover the missed contribution without duplicate IDs",
        )
        await a.conversation.announce()
        await restored.conversation.announce()
        await asyncio.sleep(0.5)
        for mesh in nodes:
            actual = messages(mesh)
            assert (
                len(actual)
                == len(expected)
                == len({item["record"]["message_id"] for item in actual})
            )
            assert {item["record"]["message_id"] for item in actual} == expected
        print(
            json.dumps(
                {
                    "qualification": "real-loopback-hyperswarm",
                    "passed": True,
                    "nodes": 2,
                    "original_contributions": 2,
                    "offline_followups": 1,
                    "distinct_retained_messages": 3,
                    "discovery_required": True,
                    "restart_catch_up": True,
                    "model_calls": 0,
                },
                sort_keys=True,
            )
        )
    finally:
        for mesh in nodes:
            await mesh.stop()
            try:
                mesh.storage.conn.close()
            except Exception:
                pass
        if bootstrap.returncode is None:
            bootstrap.terminate()
            await asyncio.wait_for(bootstrap.wait(), 10)
