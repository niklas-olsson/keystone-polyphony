from src.liminal_bridge.storage import SQLiteStorageProvider
import pytest
import asyncio
import hashlib
from unittest.mock import AsyncMock
from src.liminal_bridge.mesh import LiminalMesh


@pytest.mark.asyncio
async def test_rotate_key_logic(tmp_path):
    # Setup
    db = tmp_path / "test.db"
    identity = tmp_path / "test.pem"
    mesh = LiminalMesh("old-key", storage_provider=SQLiteStorageProvider(str(db)), identity_path=str(identity))

    # Mock sidecar communication
    mesh._send_to_sidecar = AsyncMock()

    # Rotate key with grace period
    new_key = "new-key"
    grace_period = 0.1

    # Calculate expected topics
    old_topic = hashlib.sha256("old-key".encode()).hexdigest()
    new_topic = hashlib.sha256("new-key".encode()).hexdigest()

    await mesh.rotate_key(new_key, grace_period=grace_period)

    # Verify join was called immediately for new topic
    mesh._send_to_sidecar.assert_any_call("join", {"topic": new_topic})

    # Verify key update
    assert mesh.secret_key == new_key
    assert mesh.topic == new_topic

    # Verify leave was NOT called yet (grace period)
    # Expected calls: join(new_topic)
    calls = mesh._send_to_sidecar.call_args_list
    assert len(calls) == 1
    assert calls[0][0] == ("join", {"topic": new_topic})

    # Wait for grace period
    await asyncio.sleep(grace_period + 0.1)

    # Verify leave was called for old topic
    mesh._send_to_sidecar.assert_called_with("leave", {"topic": old_topic})

    # Verify final state
    assert mesh.secret_key == new_key


@pytest.mark.asyncio
async def test_rotate_key_immediate(tmp_path):
    # Setup
    db = tmp_path / "test.db"
    identity = tmp_path / "test.pem"
    mesh = LiminalMesh("old-key", storage_provider=SQLiteStorageProvider(str(db)), identity_path=str(identity))

    # Mock sidecar communication
    mesh._send_to_sidecar = AsyncMock()

    # Rotate key without grace period
    new_key = "new-key"

    # Calculate expected topics
    old_topic = hashlib.sha256("old-key".encode()).hexdigest()
    new_topic = hashlib.sha256("new-key".encode()).hexdigest()

    await mesh.rotate_key(new_key, grace_period=0)

    # Verify calls happen in order: join new, leave old
    calls = mesh._send_to_sidecar.call_args_list
    assert len(calls) == 2
    assert calls[0][0] == ("join", {"topic": new_topic})
    assert calls[1][0] == ("leave", {"topic": old_topic})

    assert mesh.secret_key == new_key
