"""Real journal/signature tests with an explicitly simulated mesh transport."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.liminal_bridge.conversation import (
    ConversationError,
    ConversationStore,
    ConversationUnsupported,
    MAX_PAGE_BYTES,
    canonical,
    digest,
    verify_record,
)
from src.liminal_bridge.crdt import ORSet
from src.liminal_bridge.mesh import LiminalMesh
from src.liminal_bridge.storage import SQLiteStorageProvider


def node(tmp_path, name):
    return LiminalMesh(
        "isolated-conversation-fixture",
        storage_provider=SQLiteStorageProvider(str(tmp_path / (name + ".db"))),
        identity_path=str(tmp_path / (name + ".pem")),
        snapshot_path=str(tmp_path / (name + ".json")),
    )


@pytest.mark.asyncio
async def test_original_receipt_shape_stays_default_and_opt_in_returns_original_record(
    tmp_path,
):
    current = node(tmp_path, "receipt")
    current._send_to_sidecar = AsyncMock()
    try:
        receipt = await current.conversation.say(
            "design", "original", request_key="one"
        )
        assert set(receipt) == {
            "message_id",
            "session_id",
            "author",
            "stored",
            "visibility",
            "delivery",
            "execution_authorized",
        }
        original = current.conversation.store.history("design")["messages"][0]["record"]
        extended = await current.conversation.say(
            "design", "original", request_key="one", include_record=True
        )
        assert extended == receipt | {"schema_version": 1, "record": original}
        assert current.conversation.store.high_water() == 1
        with pytest.raises(ConversationError):
            await current.conversation.say("design", "no", include_record="yes")
        assert current._send_to_sidecar.await_count == 2
    finally:
        current.storage.conn.close()


class SignedTransportFixture:
    """Deliver real encrypted signed envelopes; discovery/network are fixtures."""

    def __init__(self, *nodes):
        self.nodes, self.queue, self.sent = list(nodes), [], []
        self.connected = set(nodes)
        for sender in nodes:

            async def send(operation, envelope, sender=sender):
                assert operation == "broadcast"
                self.sent.append((sender, deepcopy(envelope)))
                for target in self.connected:
                    if target is not sender and sender in self.connected:
                        self.queue.append(
                            (
                                target,
                                {
                                    "type": "message",
                                    "peer_id": sender.node_id,
                                    "payload": deepcopy(envelope),
                                },
                            )
                        )

            sender._send_to_sidecar = send

    async def flush(self):
        count = 0
        while self.queue:
            count += 1
            assert count < 10000, "protocol loop in signed transport fixture"
            target, message = self.queue.pop(0)
            if target in self.connected:
                await target._handle_message(message)


def records(mesh, topic="design"):
    return [
        item["record"] for item in mesh.conversation.store.history(topic)["messages"]
    ]


def sign_changed(mesh, record, **changes):
    body = record["body"] | changes
    return {
        "body": body,
        "message_id": digest(body),
        "signature": mesh.private_key.sign(canonical(body).encode()).hex(),
    }


def test_signed_retry_preserves_identity_and_rejects_semantic_change(
    tmp_path, monkeypatch
):
    first = node(tmp_path, "one")
    store = first.conversation.store
    created = store.post("design", "proposal", request_key="request-1")
    monkeypatch.setattr("src.liminal_bridge.conversation.time.time", lambda: 999999)
    replay = store.post("design", "proposal", request_key="request-1")
    assert replay == created
    assert store.high_water() == 1
    verify_record(replay, first.topic)
    with pytest.raises(ConversationError, match="changed"):
        store.post("design", "changed proposal", request_key="request-1")
    restored = node(tmp_path, "one")
    assert (
        restored.conversation.store.post("design", "proposal", request_key="request-1")
        == created
    )
    assert restored.conversation.store.stream_id == store.stream_id


def test_reusing_a_node_database_for_another_swarm_cannot_mix_conversation_history(
    tmp_path,
):
    original = node(tmp_path, "one")
    store = original.conversation.store
    record = store.post("private-to-this-swarm", "public only to its current swarm")
    with pytest.raises(ConversationUnsupported, match="scope"):
        ConversationStore(original.storage.conn, original.private_key, "b" * 64)
    reopened = node(tmp_path, "one")
    assert reopened.conversation.store.stream_id == store.stream_id
    assert reopened.conversation.store.page(0)["records"][0]["record"] == record


@pytest.mark.asyncio
async def test_key_rotation_disables_unsupported_chat_scope_and_retains_old_history(
    tmp_path,
):
    original = node(tmp_path, "one")
    old_api = original.conversation
    record = old_api.store.post("design", "retained original signed content")
    original._send_to_sidecar = AsyncMock()
    await original.rotate_key("another-swarm")
    with pytest.raises(ConversationUnsupported, match="scope"):
        original.require_conversation()
    with pytest.raises(ConversationUnsupported):
        await old_api.say("design", "old API reference must not publish after rotation")
    with pytest.raises(ConversationUnsupported):
        await old_api.announce()
    different = LiminalMesh(
        "another-swarm",
        SQLiteStorageProvider(str(tmp_path / "one.db")),
        str(tmp_path / "one.pem"),
        snapshot_path=str(tmp_path / "another.json"),
    )
    with pytest.raises(ConversationUnsupported, match="scope"):
        different.require_conversation()
    assert different.conversation is None
    different.storage.save_metadata("non-chat-still-works", True)
    assert different.storage.get_metadata("non-chat-still-works") == "True"
    restored = node(tmp_path, "one")
    assert records(restored) == [record]


@pytest.mark.asyncio
async def test_relay_retains_original_signature_and_outer_peer_cannot_forge_author(
    tmp_path,
):
    author, relay, recipient = (
        node(tmp_path, name) for name in ("author", "relay", "recipient")
    )
    wire = SignedTransportFixture(relay, recipient)
    original = author.conversation.store.post("design", "independent dissent")
    await relay.broadcast({"type": "chat_delta", "record": original})
    await wire.flush()
    received = records(recipient)
    assert received == [original]
    assert received[0]["body"]["author"] == author.node_id
    assert received[0]["body"]["author"] != relay.node_id
    forged = sign_changed(relay, original, content="claimed by the relay")
    await relay.broadcast({"type": "chat_delta", "record": forged})
    await wire.flush()
    assert records(recipient) == [original]
    tampered = deepcopy(original)
    tampered["body"]["content"] = "tampered after signing"
    with pytest.raises(ConversationError):
        recipient.conversation.store.receive(tampered)


def test_causal_reply_survives_reordering_and_cross_session_reference_is_explicit(
    tmp_path,
):
    author, receiver = node(tmp_path, "a"), node(tmp_path, "b")
    source, target = author.conversation.store, receiver.conversation.store
    parent = source.post("design", "proposal")
    reply = source.post("design", "criticism", reply_to=parent["message_id"])
    target.receive(reply)
    assert target.history("design")["messages"][0]["reply_state"] == "unresolved"
    target.receive(parent)
    page = target.history("design")
    assert [entry["record"]["message_id"] for entry in page["messages"]] == [
        reply["message_id"],
        parent["message_id"],
    ]
    assert page["messages"][0]["reply_state"] == "resolved"
    other = source.post("other", "separate conversation")
    cross = source.post(
        "design", "not a same-session reply", reply_to=other["message_id"]
    )
    target.receive(cross)
    target.receive(other)
    assert target.history("design")["messages"][-1]["reply_state"] == "cross_session"


def test_unread_holes_and_old_timestamps_survive_restart(tmp_path):
    first = node(tmp_path, "first")
    store = first.conversation.store
    messages = [store.post("design", str(i)) for i in range(3)]
    page = store.history("design", after=1, page_limit=1)
    assert page["messages"][0]["record"] == messages[1]
    assert store.mark_read("design")["unread"] == 2
    restored = node(tmp_path, "first").conversation.store
    assert restored.topics(unread_only=True)["topics"][0]["unread"] == 2
    restored.history("design", page_limit=1)
    assert restored.mark_read("design", through=1)["unread"] == 1
    delayed = sign_changed(first, store.post("design", "delayed draft"), timestamp=1)
    delayed["body"]["request_key"] = "old-arrival"
    delayed = sign_changed(first, delayed)
    restored.receive(delayed)
    assert restored.topics(unread_only=True)["topics"][0]["unread"] == 3
    assert restored.mark_read("design")["unread"] == 3


@pytest.mark.asyncio
async def test_bounded_catch_up_repairs_lost_last_delta_without_reconnect(tmp_path):
    a, b = node(tmp_path, "a"), node(tmp_path, "b")
    wire = SignedTransportFixture(a, b)
    first = await a.conversation.say("design", "one", request_key="first")
    await wire.flush()
    assert [item["message_id"] for item in records(b)] == [first["message_id"]]
    await a.conversation.announce()
    await wire.flush()
    second = await a.conversation.say(
        "design", "lost final delta", request_key="second"
    )
    wire.queue.clear()  # Explicit loss fixture; both peers stay connected.
    assert len(records(b)) == 1
    await a.conversation.announce()  # Same production hook used by periodic gossip.
    await wire.flush()
    assert {item["message_id"] for item in records(b)} == {
        first["message_id"],
        second["message_id"],
    }
    assert (
        b.conversation.store.db.execute(
            "SELECT cursor FROM conversation_peers WHERE peer=?", (a.node_id,)
        ).fetchone()[0]
        == 2
    )


@pytest.mark.asyncio
async def test_large_history_uses_bounded_pages_and_deduplicates_after_disconnect(
    tmp_path,
):
    a, b = node(tmp_path, "a"), node(tmp_path, "b")
    wire = SignedTransportFixture(a, b)
    wire.connected.remove(b)
    for i in range(45):
        await a.conversation.say("design", "x" * 5000, request_key=f"message-{i}")
    wire.connected.add(b)
    await a.conversation.announce()
    await wire.flush()
    assert b.conversation.store.high_water() == 45
    pages = [
        sender._decrypt(envelope["e"])
        for sender, envelope in wire.sent
        if sender._decrypt(envelope["e"])["type"] == "chat_page"
    ]
    assert len(pages) > 1
    assert all(
        len(canonical(page["records"]).encode()) <= MAX_PAGE_BYTES + 66
        for page in pages
    )
    assert all(len(canonical(envelope).encode()) < 65536 for _, envelope in wire.sent)
    await a.conversation.announce()
    await wire.flush()
    assert b.conversation.store.high_water() == 45
    after, all_ids = 0, []
    while True:
        page = b.conversation.store.history("design", after=after, page_limit=100)
        assert len(canonical(page["messages"]).encode()) <= MAX_PAGE_BYTES + 202
        all_ids.extend(item["record"]["message_id"] for item in page["messages"])
        after = page["next_cursor"]
        if not page["has_more"]:
            break
    assert len(all_ids) == len(set(all_ids)) == 45


def test_invalid_partial_page_rolls_back_records_and_peer_cursor(tmp_path):
    a, b = node(tmp_path, "a"), node(tmp_path, "b")
    source, target = a.conversation.store, b.conversation.store
    source.post("design", "one")
    source.post("design", "two")
    request = target.request_page(a.node_id, source.stream_id, 2)
    page = source.page(0) | {"request_id": request["request_id"]}
    invalid = deepcopy(page)
    invalid["records"][1]["record"]["body"]["content"] = "invalid signature"
    with pytest.raises(ConversationError):
        target.accept_page(a.node_id, invalid)
    assert target.high_water() == 0
    assert target.db.execute("SELECT cursor FROM conversation_peers").fetchone()[0] == 0
    target.accept_page(a.node_id, page)
    assert target.high_water() == 2
    with pytest.raises(ConversationError, match="stale"):
        target.accept_page(a.node_id, page)
    assert target.high_water() == 2


def test_semantic_conflict_in_late_page_record_rolls_back_earlier_valid_insert(
    tmp_path,
):
    a, b = node(tmp_path, "a"), node(tmp_path, "b")
    source, target = a.conversation.store, b.conversation.store
    source.post("design", "one", request_key="one")
    original = source.post("design", "two", request_key="two")
    target.receive(sign_changed(a, original, content="equivocated signed content"))
    request = target.request_page(a.node_id, source.stream_id, 2)
    page = source.page(0) | {"request_id": request["request_id"]}
    with pytest.raises(ConversationError, match="changed"):
        target.accept_page(a.node_id, page)
    assert target.high_water() == 1
    assert target.db.execute("SELECT cursor FROM conversation_peers").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_wrong_target_and_stale_page_cannot_move_cursor(tmp_path):
    a, b, c = (node(tmp_path, name) for name in ("a", "b", "c"))
    wire = SignedTransportFixture(a, b, c)
    a.conversation.store.post("design", "one")
    request = b.conversation.store.request_page(
        a.node_id, a.conversation.store.stream_id, 1
    )
    page = a.conversation.store.page(0) | {
        "type": "chat_page",
        "target": c.node_id,
        "request_id": request["request_id"],
    }
    await a.broadcast(page)
    await wire.flush()
    assert b.conversation.store.high_water() == c.conversation.store.high_water() == 0
    page["target"], page["request_id"] = b.node_id, "0" * 32
    await a.broadcast(page)
    await wire.flush()
    assert b.conversation.store.high_water() == 0


def test_stream_reset_is_visible_and_old_hello_cannot_restore_retired_stream(tmp_path):
    a, b, fresh = (node(tmp_path, name) for name in ("a", "b", "fresh"))
    source, target = a.conversation.store, b.conversation.store
    source.post("design", "retained historical content")
    request = target.request_page(a.node_id, source.stream_id, 1)
    target.accept_page(
        a.node_id, source.page(0) | {"request_id": request["request_id"]}
    )
    new_stream = fresh.conversation.store.stream_id
    target.request_page(a.node_id, new_stream, 0)
    gap = target.history("design")["peer_gaps"]["items"][0]
    assert (
        gap["reason"] == "remote_stream_reset"
        and gap["previous_stream"] == source.stream_id
    )
    assert target.request_page(a.node_id, source.stream_id, 1) is None
    assert target.db.execute(
        "SELECT stream_id,cursor FROM conversation_peers"
    ).fetchone() == (new_stream, 0)
    assert target.history("design", stream_id="0" * 32)["gap"]
    assert target.history("design", after=999)["gap"]


@pytest.mark.asyncio
async def test_watch_is_bounded_and_discussion_never_dispatches_commands(tmp_path):
    a, b = node(tmp_path, "a"), node(tmp_path, "b")
    wire = SignedTransportFixture(a, b)
    b.on_command_request = AsyncMock()
    waiting = asyncio.create_task(b.conversation.watch("design", timeout=1))
    await asyncio.sleep(0)
    await a.conversation.say(
        "design", "execute a command", kind="invite", target=b.node_id
    )
    await wire.flush()
    assert len((await waiting)["messages"]) == 1
    assert not b.on_command_request.called
    result = await b.conversation.watch("design", after=1, timeout=0.001)
    assert result["messages"] == []
    with pytest.raises(ConversationError):
        await b.conversation.watch("design", timeout=float("inf"))


@pytest.mark.parametrize(
    "change",
    [
        {"content": "x" * 8193},
        {"timestamp": float("nan")},
        {"kind": []},
        {"request_key": {}},
        {"reply_to": "not-a-hash"},
        {"extra": "unapproved"},
        {"references": [{"kind": "artifact", "id": "https://example.invalid/private"}]},
        {"visibility": "private"},
        {"author": "0" * 16},
    ],
)
def test_invalid_records_never_persist(tmp_path, change):
    a, b = node(tmp_path, "a"), node(tmp_path, "b")
    original = a.conversation.store.post("design", "valid")
    changed = deepcopy(original)
    changed["body"].update(change)
    with pytest.raises(ConversationError):
        b.conversation.store.receive(changed)
    assert b.conversation.store.high_water() == 0


@pytest.mark.asyncio
async def test_legacy_chat_is_not_in_welcome_or_gossip_and_cannot_be_republished(
    tmp_path,
):
    a = node(tmp_path, "a")
    legacy = ORSet()
    legacy.add(json.dumps({"sender": "unverified", "content": "old", "timestamp": 1}))
    a.kv_store["chat:old"] = legacy
    a.storage.save_kv("chat:old", legacy)
    a._send_to_sidecar = AsyncMock()
    await a._push_welcome_to_peer("fixture-peer")
    await a._handle_payload({"type": "gossip_request", "origin": "a" * 16})
    for call in a._send_to_sidecar.call_args_list:
        payload = a._decrypt(call.args[1]["e"])
        assert "chat:old" not in payload.get("kv", {})
    assert a.legacy_chat("old")["messages"][0]["legacy_unverified"] is True
    with pytest.raises(ConversationError, match="read-only"):
        await a.update_set("chat:old", "new")
    for mutation in (
        a.update_kv("chat:old", "new"),
        a.update_counter("chat:old", 1),
        a.save_revision("chat:old", "new"),
    ):
        with pytest.raises(ConversationError, match="read-only"):
            await mutation
    remote = ORSet()
    remote.add(json.dumps({"sender": "forged", "content": "remote legacy injection"}))
    await a._handle_payload(
        {
            "type": "kv_update",
            "origin": "b" * 16,
            "key": "chat:old",
            "crdt": remote.to_dict(),
        }
    )
    assert a.kv_store["chat:old"].value() == legacy.value()
