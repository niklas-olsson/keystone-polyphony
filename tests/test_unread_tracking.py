"""Exercise the actual MCP wrappers, not a duplicated read-marker algorithm."""

import json
from unittest.mock import AsyncMock

import pytest

from src.liminal_bridge import server
from src.liminal_bridge.conversation import ConversationUnsupported
from src.liminal_bridge.mesh import LiminalMesh
from src.liminal_bridge.storage import SQLiteStorageProvider


@pytest.fixture
def mesh(tmp_path, monkeypatch):
    result = LiminalMesh(
        "unread-wrapper-fixture",
        storage_provider=SQLiteStorageProvider(str(tmp_path / "node.db")),
        identity_path=str(tmp_path / "node.pem"),
        snapshot_path=str(tmp_path / "snapshot.json"),
    )
    result.running = True  # Transport availability is a labelled fixture here.
    result._send_to_sidecar = AsyncMock()
    monkeypatch.setattr(server, "mesh", result)
    return result


@pytest.mark.asyncio
async def test_real_wrappers_preserve_unseen_holes_and_idempotent_posts(mesh):
    one = json.loads(await server.conversation_say("general", "one", request_key="one"))
    await server.conversation_say("general", "two", request_key="two")
    await server.conversation_say("work", "work", request_key="work")
    assert json.loads(await server.get_unread_chats()) == ["general", "work"]
    result = json.loads(await server.mark_chat_read("general"))
    assert result == {"session_id": one["session_id"], "marked": 0, "unread": 2}
    page = json.loads(await server.get_ensemble_chat("general", after=1, limit=1))
    assert page[0]["content"] == "two" and page[0]["authenticated"] is True
    assert json.loads(await server.mark_chat_read("general"))["unread"] == 1
    page = json.loads(await server.conversation_history("general", limit=1))
    assert page["messages"][0]["record"]["message_id"] == one["message_id"]
    assert (
        json.loads(await server.mark_chat_read("general", through=page["next_cursor"]))[
            "unread"
        ]
        == 0
    )
    assert json.loads(await server.get_unread_chats()) == ["work"]
    replay = json.loads(
        await server.conversation_say("general", "one", request_key="one")
    )
    assert replay["message_id"] == one["message_id"]
    assert json.loads(await server.list_ensemble_chats(limit=1)) == ["general"]
    assert json.loads(await server.list_ensemble_chats(after="general", limit=1)) == [
        "work"
    ]
    assert (
        json.loads(await server.conversation_watch("general", after=3, timeout=0.001))
    )["messages"] == []


@pytest.mark.asyncio
async def test_participation_is_voluntary_public_data_without_task_enrollment(mesh):
    command = AsyncMock()
    mesh.on_command_request = command
    await server.conversation_start("debate", request_key="start")
    await server.conversation_join(
        "debate", "decline", "I will not participate", request_key="decline"
    )
    await server.ensemble_chat(
        "debate", "A preserved alternative", request_key="alternative"
    )
    history = json.loads(await server.conversation_history("debate"))
    assert [entry["record"]["body"]["kind"] for entry in history["messages"]] == [
        "start",
        "decline",
        "message",
    ]
    assert not command.called
    assert not mesh.get_kv("swarm_backlog")


@pytest.mark.asyncio
async def test_unsupported_backend_is_explicit_and_never_falls_back_to_unsigned_chat(
    mesh,
):
    mesh.conversation = None
    with pytest.raises(ConversationUnsupported):
        await server.ensemble_chat("unsupported", "do not silently publish")
    with pytest.raises(ConversationUnsupported):
        await server.conversation_history("unsupported")
    assert not mesh._send_to_sidecar.called
