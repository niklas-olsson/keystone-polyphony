"""Real private storage and SDK processes; the Node transport is explicit fixture."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.liminal_bridge import conversation_server as profile
from src.liminal_bridge.conversation import (
    ConversationError,
    ConversationUnsupported,
    canonical,
    verify_record,
)
from src.liminal_bridge.conversation_stdio import StdioError

ROOT = Path(__file__).resolve().parents[1]


def private(path, content):
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    path.chmod(0o600)
    return path


def configuration(tmp_path):
    scope = private(tmp_path / "scope", "test public scope\n")
    value = {
        "schema_version": 1,
        "topic": "architecture",
        "scope_key_file": str(scope),
        "state_directory": str(tmp_path / "state"),
    }
    config = private(tmp_path / "config.json", json.dumps(value))
    return value, config


def files(directory):
    return {
        path.name: path.read_bytes()
        for path in directory.iterdir()
        if path.name != "owner.lock"
    }


def state(value):
    return profile.ProfileState(
        value["state_directory"], value["scope_key_file"], value["topic"]
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        "127.0.0.1:12",
        {},
        {"host": "localhost", "port": 12},
        {"host": "::1", "port": 12},
        {"host": "127.0.0.1", "port": True},
        {"host": "127.0.0.1", "port": "12"},
        {"host": "127.0.0.1", "port": 0},
        {"host": "127.0.0.1", "port": 65536},
        {"host": "127.0.0.1", "port": 12, "extra": False},
    ],
)
def test_invalid_operator_bootstrap_refused_before_state(tmp_path, endpoint):
    value, config = configuration(tmp_path)
    value["bootstrap"] = endpoint
    private(config, json.dumps(value))
    with pytest.raises(profile.ProfileError):
        profile.load_config(str(config))
    assert not Path(value["state_directory"]).exists()


def test_exact_private_config_and_readonly_scope_preserve_bytes(tmp_path):
    value, config = configuration(tmp_path)
    value["bootstrap"] = {"host": "127.0.0.1", "port": 12345}
    raw = json.dumps(value, indent=3).encode()
    private(config, raw).chmod(0o400)
    Path(value["scope_key_file"]).chmod(0o400)
    assert profile.load_config(str(config)) == value
    assert config.read_bytes() == raw
    current = state(value)
    try:
        assert (
            current.binding["swarm_id"]
            == hashlib.sha256(b"test public scope\n").hexdigest()
        )
    finally:
        current.close()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b"\xff",
        b"[]",
        b"{}",
        b" " * 65537,
    ],
)
def test_invalid_private_json_has_no_state_effect(tmp_path, raw):
    value, config = configuration(tmp_path)
    private(config, raw)
    with pytest.raises((profile.ProfileError, StdioError)):
        profile.load_config(str(config))
    assert not Path(value["state_directory"]).exists()


@pytest.mark.parametrize("kind", ["fifo", "symlink", "hardlink", "mode", "directory"])
def test_private_inode_refusal_is_bounded_and_before_read(tmp_path, kind):
    value, config = configuration(tmp_path)
    config.unlink()
    if kind == "fifo":
        os.mkfifo(config, 0o600)
    elif kind == "symlink":
        config.symlink_to(value["scope_key_file"])
    elif kind == "hardlink":
        os.link(value["scope_key_file"], config)
    elif kind == "mode":
        private(config, json.dumps(value)).chmod(0o644)
    else:
        config.mkdir(mode=0o700)
    code = "from src.liminal_bridge.conversation_server import main; import sys; sys.exit(main())"
    result = subprocess.run(
        [sys.executable, "-c", code, "--config", str(config)],
        cwd=ROOT,
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == 2
    assert not result.stdout
    assert str(config).encode() not in result.stderr
    assert not Path(value["state_directory"]).exists()


def test_owner_check_uses_opened_inode_and_canonical_paths(tmp_path, monkeypatch):
    value, config = configuration(tmp_path)
    monkeypatch.setattr(profile.os, "getuid", lambda: config.stat().st_uid + 1)
    with pytest.raises(profile.ProfileError):
        profile.load_config(str(config))
    monkeypatch.undo()
    with pytest.raises(profile.ProfileError):
        profile.load_config(str(tmp_path / ".." / tmp_path.name / config.name))
    assert not Path(value["state_directory"]).exists()


def test_retained_identity_stream_and_original_record_survive_reopen(tmp_path):
    value, _ = configuration(tmp_path)
    first = state(value)
    status = dict(first.binding)
    record = first.mesh.require_conversation().store.post(
        "architecture", "original", request_key="stable"
    )
    first.close()
    second = state(value)
    try:
        assert second.binding == status
        assert (
            second.mesh.require_conversation().store.post(
                "architecture", "original", request_key="stable"
            )
            == record
        )
        with pytest.raises(ConversationError):
            second.mesh.require_conversation().store.post(
                "architecture", "changed", request_key="stable"
            )
        verify_record(record, status["swarm_id"])
    finally:
        second.close()


@pytest.mark.parametrize(
    "damage",
    [
        "identity.pem",
        "conversation.sqlite3",
        "binding.json",
        "stream",
        "scope",
        "topic",
        "old_profile",
        "boolean_schema",
        "extra",
    ],
)
def test_damaged_retained_state_never_autoheals(tmp_path, damage):
    value, _ = configuration(tmp_path)
    initial = state(value)
    directory = initial.directory
    initial.close()
    if damage in {"identity.pem", "conversation.sqlite3", "binding.json"}:
        (directory / damage).unlink()
    elif damage == "stream":
        with sqlite3.connect(directory / "conversation.sqlite3") as db:
            db.execute("DELETE FROM conversation_stream")
    elif damage == "scope":
        private(Path(value["scope_key_file"]), "different scope")
    elif damage == "topic":
        value["topic"] = "different"
    elif damage == "extra":
        private(directory / "unknown", "held")
    else:
        binding = json.loads((directory / "binding.json").read_text())
        if damage == "old_profile":
            binding["profile"] = "polyphony-conversation-only/v1"
        else:
            binding["schema_version"] = True
        private(directory / "binding.json", canonical(binding))
    before = files(directory)
    with pytest.raises(profile.ProfileError):
        state(value)
    assert files(directory) == before


def test_real_second_process_cannot_own_state(tmp_path):
    value, _ = configuration(tmp_path)
    first = state(value)
    try:
        code = (
            "import sys; from src.liminal_bridge.conversation_server import ProfileState, ProfileError; "
            "ProfileState(*sys.argv[1:])"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                value["state_directory"],
                value["scope_key_file"],
                value["topic"],
            ],
            cwd=ROOT,
            capture_output=True,
            timeout=3,
        )
        assert result.returncode != 0
        assert b"conversation_state_already_owned" in result.stderr
    finally:
        first.close()
    reopened = state(value)
    reopened.close()


@pytest.mark.parametrize("previous_success", [False, True])
def test_existing_lock_only_state_never_creates_replacement_identity(
    tmp_path, previous_success
):
    value, _ = configuration(tmp_path)
    directory = Path(value["state_directory"])
    if previous_success:
        original = state(value)
        original.close()
        for name in ("identity.pem", "binding.json", "conversation.sqlite3"):
            (directory / name).unlink()
    else:
        directory.mkdir(mode=0o700)
        private(directory / "owner.lock", b"")
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in directory.iterdir()
    }
    with pytest.raises(
        profile.ProfileError, match="incomplete_state_requires_explicit_recovery"
    ):
        state(value)
    assert {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in directory.iterdir()
    } == before


def test_empty_private_operator_directory_can_initialize_once(tmp_path):
    value, _ = configuration(tmp_path)
    Path(value["state_directory"]).mkdir(mode=0o700)
    current = state(value)
    binding = dict(current.binding)
    current.close()
    reopened = state(value)
    assert reopened.binding == binding
    reopened.close()


async def ready_fixture(value):
    current = profile.ConversationProfile(value)
    current.process = SimpleNamespace(returncode=None)
    current.ready = asyncio.get_running_loop().create_future()
    current.ready.set_result(True)
    sent = []

    async def submit(operation, envelope):
        sent.append((operation, envelope))

    current.mesh._send_to_sidecar = submit
    return current, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["message", "decline", "leave"])
async def test_exact_public_receipt_and_same_key_replay(tmp_path, kind):
    value, _ = configuration(tmp_path)
    current, sent = await ready_fixture(value)
    try:
        status = await current.call("conversation_status", {})
        assert set(status) == {
            "schema_version",
            "profile",
            "topic",
            "swarm_id",
            "session_id",
            "author",
            "public_key",
            "stream_id",
            "visibility",
            "execution_authorized",
        }
        args = {
            "topic": value["topic"],
            "message": "public",
            "kind": kind,
            "request_key": "same",
        }
        first = await current.call("conversation_say", args)
        assert await current.call("conversation_say", args) == first
        assert set(first) == {
            "schema_version",
            "record",
            "message_id",
            "session_id",
            "author",
            "stored",
            "visibility",
            "delivery",
            "execution_authorized",
        }
        assert first["record"]["body"]["kind"] == kind
        verify_record(first["record"], status["swarm_id"])
        assert first["record"]["message_id"] == first["message_id"]
        assert len(sent) == 2 and first["delivery"] == "submitted"
        page = await current.call("conversation_history", {"topic": value["topic"]})
        assert [item["record"] for item in page["messages"]] == [first["record"]]
        assert await current.call("conversation_status", {}) == status
    finally:
        current.state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("conversation_say", {"topic": "elsewhere", "message": "no"}),
        (
            "conversation_say",
            {"topic": "architecture", "message": "no", "tool": "execute"},
        ),
        (
            "conversation_say",
            {"topic": "architecture", "message": "no", "kind": "execute"},
        ),
        (
            "conversation_say",
            {
                "topic": "architecture",
                "message": "no",
                "references": [{"kind": "task", "id": "/private/file"}],
            },
        ),
        ("conversation_history", {"topic": "architecture", "after": True}),
        ("conversation_history", {"topic": "architecture", "after": 1}),
        (
            "conversation_history",
            {"topic": "architecture", "after": 0, "stream_id": "wrong"},
        ),
        ("conversation_watch", {"topic": "architecture", "timeout": float("inf")}),
        ("conversation_watch", {"topic": "architecture", "timeout": True}),
        ("conversation_join", {"topic": "architecture", "decision": "execute"}),
    ],
)
async def test_tool_refusals_precede_publication(tmp_path, name, args):
    value, _ = configuration(tmp_path)
    current, sent = await ready_fixture(value)
    try:
        with pytest.raises((profile.ProfileError, ConversationError)):
            await current.call(name, args)
        assert not sent
        assert current.mesh.require_conversation().store.high_water() == 0
    finally:
        current.state.close()


@pytest.mark.asyncio
async def test_failed_submission_preserves_exact_record_for_retry(tmp_path):
    value, _ = configuration(tmp_path)
    current, sent = await ready_fixture(value)
    normal = current.mesh._send_to_sidecar

    async def broken(*args):
        raise OSError("fixture transport unavailable")

    try:
        current.mesh._send_to_sidecar = broken
        arguments = {"topic": value["topic"], "message": "kept", "request_key": "retry"}
        with pytest.raises(OSError):
            await current.call("conversation_say", arguments)
        retained = current.mesh.require_conversation().store.history(value["topic"])[
            "messages"
        ][0]["record"]
        current.mesh._send_to_sidecar = normal
        result = await current.call("conversation_say", arguments)
        assert result["record"] == retained and len(sent) == 1
    finally:
        current.state.close()


@pytest.mark.asyncio
async def test_actual_sdk_process_uses_exact_tools_and_sanitized_owned_child(tmp_path):
    value, config = configuration(tmp_path)
    observations = tmp_path / "child.json"
    fake_node = tmp_path / "node-fixture"
    fake_node.write_text(
        f"#!{sys.executable}\n"
        + "import json,os,sys\n"
        + f"open({str(observations)!r},'w').write(json.dumps(dict(env=dict(os.environ),pid=os.getpid(),pgid=os.getpgrp(),parent_pgid=os.getpgid(os.getppid()))))\n"
        + "print(json.dumps(dict(type='conversation_ready',topic=sys.argv[2],profile='polyphony-conversation-only/v1')),flush=True)\n"
        + "for line in sys.stdin.buffer: pass\n"
    )
    fake_node.chmod(0o700)
    code = (
        "from src.liminal_bridge import conversation_server as p; "
        f"p.shutil.which=lambda *a,**k: {str(fake_node)!r}; "
        "raise SystemExit(p.main())"
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", code, "--config", str(config)],
        cwd=str(ROOT),
        env={
            "DUCKY_API_KEY": "provider-canary",
            "NODE_OPTIONS": "injection-canary",
            "POLYPHONY_LOCAL_MEASUREMENT": "1",
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            assert initialized.capabilities.model_dump(exclude_none=True) == {
                "tools": {"listChanged": False}
            }
            tools = await client.list_tools()
            assert {
                item.name: item.inputSchema for item in tools.tools
            } == profile.tool_schemas()
            assert all(
                item.outputSchema is None and item.annotations is None
                for item in tools.tools
            )
            result = await client.call_tool("conversation_status", {})
            status = json.loads(result.content[0].text)
            assert status["profile"] == "polyphony-public-conversation"
            posted = await client.call_tool(
                "conversation_say",
                {
                    "topic": value["topic"],
                    "message": "SDK publication",
                    "kind": "decline",
                    "request_key": "sdk",
                },
            )
            acknowledgement = json.loads(posted.content[0].text)
            verify_record(acknowledgement["record"], status["swarm_id"])
            assert acknowledgement["record"]["body"]["kind"] == "decline"
            observed = json.loads(observations.read_text())
            assert observed["env"] == profile.ConversationProfile.child_environment()
            assert observed["pgid"] == observed["parent_pgid"]
    with pytest.raises(ProcessLookupError):
        os.kill(observed["pid"], 0)
    reopened = state(value)
    assert reopened.binding == status
    reopened.close()


@pytest.mark.asyncio
async def test_restricted_mesh_rejects_signed_legacy_effects_and_accepts_real_chat(
    tmp_path, monkeypatch
):
    value, _ = configuration(tmp_path)
    other = dict(value, state_directory=str(tmp_path / "other-state"))
    monkeypatch.setattr(
        profile.LiminalMesh,
        "_load_or_create_identity",
        lambda *args: pytest.fail("ordinary identity loader invoked"),
    )
    receiver, sender = state(value), state(other)
    try:
        callback = AsyncMock()
        receiver.mesh.on_command_request = callback
        receiver.mesh.on_tandem_sync = callback
        receiver.mesh.on_baton_release = callback
        before = list(receiver.mesh.storage.conn.iterdump())
        clock = dict(receiver.mesh.vector_clock)
        for kind in (
            "command",
            "command_request",
            "kv_update",
            "welcome",
            "baton_claim",
            "baton_release",
            "tandem_sync",
            "thought",
            [],
            {},
        ):
            payload = {
                "type": kind,
                "origin": sender.mesh.node_id,
                "command": "must not run",
                "resource": "task:task_v2_reserved",
                "kv": {"swarm_backlog": "poison"},
                "vc": {"remote": 999},
            }
            await receiver.mesh._handle_message(
                {
                    "type": "message",
                    "peer_id": "ab" * 32,
                    "payload": sender.mesh._signed_envelope(payload),
                }
            )
        assert not callback.called
        assert receiver.mesh.vector_clock == clock
        assert (
            not receiver.mesh.kv_store
            and not receiver.mesh.batons
            and not receiver.mesh.peer_map
        )
        assert list(receiver.mesh.storage.conn.iterdump()) == before
        record = sender.mesh.require_conversation().store.post(
            value["topic"], "signed public contribution"
        )
        payload = {
            "type": "chat_delta",
            "origin": sender.mesh.node_id,
            "record": record,
        }
        await receiver.mesh._handle_message(
            {
                "type": "message",
                "peer_id": "ab" * 32,
                "payload": sender.mesh._signed_envelope(payload),
            }
        )
        assert (
            receiver.mesh.require_conversation().store.history(value["topic"])[
                "messages"
            ][0]["record"]
            == record
        )
        assert not callback.called
        with pytest.raises(ConversationUnsupported):
            await receiver.mesh.start()
    finally:
        receiver.close()
        sender.close()


@pytest.mark.parametrize(
    "incoming,cleanup_fault",
    [
        (b"", False),
        (b"{bad}\n", False),
        ("terminate", False),
        (b"", True),
    ],
)
def test_eof_or_malformed_input_during_unready_child_starts_cleanup_immediately(
    tmp_path, incoming, cleanup_fault
):
    value, config = configuration(tmp_path)
    observed = tmp_path / "unready-child.pid"
    fake_node = tmp_path / "unready-node-fixture"
    fake_node.write_text(
        f"#!{sys.executable}\nimport os,time\n"
        + f"open({str(observed)!r},'w').write(str(os.getpid()))\n"
        + "time.sleep(30)\n"
    )
    fake_node.chmod(0o700)
    code = (
        "from src.liminal_bridge import conversation_server as p\n"
        f"p.shutil.which=lambda *a,**k: {str(fake_node)!r}\n"
    )
    if cleanup_fault:
        code += (
            "original_stop=p.ConversationProfile.stop\n"
            "async def fail_after_cleanup(self):\n"
            "    was_closing=self.closing\n"
            "    await original_stop(self)\n"
            "    if not was_closing: raise p.ProfileError('cleanup-fault-private-canary')\n"
            "p.ConversationProfile.stop=fail_after_cleanup\n"
        )
    code += "raise SystemExit(p.main())\n"
    child = subprocess.Popen(
        [sys.executable, "-c", code, "--config", str(config)],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        cutoff = time.monotonic() + 3
        while not observed.exists():
            assert time.monotonic() < cutoff and child.poll() is None
            time.sleep(0.01)
        started = time.monotonic()
        if incoming == "terminate":
            child.send_signal(signal.SIGTERM)
        elif incoming:
            child.stdin.write(incoming)
            child.stdin.flush()
        child.stdin.close()
        assert child.wait(timeout=4) == (2 if incoming or cleanup_fault else 0)
        assert time.monotonic() - started < 4
        if cleanup_fault:
            diagnostic = child.stderr.read()
            assert b"Public conversation unavailable" in diagnostic
            assert b"cleanup-fault-private-canary" not in diagnostic
        with pytest.raises(ProcessLookupError):
            os.kill(int(observed.read_text()), 0)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        child.stdout.close()
        child.stderr.close()
    reopened = state(value)
    reopened.close()
