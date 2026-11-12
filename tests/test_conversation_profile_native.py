"""Explicit native qualification of the real restricted module over MCP stdio.

No profile, transport, provider or delivery stub is used. The fixed child PATH
and locked native dependencies are prerequisites, never reasons to silently
skip an enabled trial. This is component evidence, not the installed B/C scene.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time

import pytest

from src.liminal_bridge.conversation import verify_record

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "src/liminal_bridge/sidecar"
TOPIC = "restricted-profile-native-trial"
CHILD_ENVIRONMENT = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "PYTHONNOUSERSITE": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}
TOOLS = {
    "conversation_status",
    "conversation_start",
    "conversation_join",
    "conversation_say",
    "conversation_history",
    "conversation_watch",
}
STATUS_FIELDS = {
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


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")


def process_identity(pid):
    try:
        # The start-time field distinguishes a retained child from PID reuse.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19]
    except FileNotFoundError:
        return None


def same_process(pid, identity):
    return identity is not None and process_identity(pid) == identity


async def stop_bootstrap(process):
    """Bound and reap the separate bootstrap, which creates no child process."""
    forced = False
    if process.stdin is not None:
        process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        forced = True
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
        await asyncio.wait_for(process.wait(), 5)
    assert not forced, "bootstrap exceeded its real cleanup deadline"
    assert process.returncode == 0, "bootstrap did not exit successfully"


def profile_exited(process):
    # Observe exit without reaping the session leader and losing its group ID.
    return (
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        is not None
    )


def live_group(pgid, cutoff):
    with os.scandir("/proc") as entries:
        for entry in entries:
            assert time.monotonic() < cutoff, "process-group observation timed out"
            if not entry.name.isdecimal():
                continue
            try:
                with open(entry.path + "/stat", "rb") as handle:
                    raw = handle.read(4097)
            except FileNotFoundError:
                continue
            assert len(raw) <= 4096
            fields = raw.rsplit(b")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] not in {b"Z", b"X"}:
                return True
    return False


def signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


async def stop_profile(process, descendants):
    """Keep the leader waitable until its original group has stopped.

    Initialization failures may precede the Node PID inventory. Never poll,
    wait or communicate with this leader before the final group signal.
    """
    process.stdin.close()
    cutoff = time.monotonic() + 5
    clean = False
    try:
        while time.monotonic() < cutoff:
            if profile_exited(process) and not live_group(process.pid, cutoff):
                clean = True
                break
            await asyncio.sleep(0.01)
    finally:
        # An exited leader may have left an uninventoried child. Its still
        # reserved group receives this final signal before any waitpid call.
        signal_group(process, signal.SIGKILL)
        forced_cutoff = time.monotonic() + 3
        try:
            while not profile_exited(process) or live_group(process.pid, forced_cutoff):
                assert (
                    time.monotonic() < forced_cutoff
                ), "owned profile group did not stop"
                await asyncio.sleep(0.01)
        finally:
            process.wait(timeout=max(0.001, forced_cutoff - time.monotonic()))
    assert clean, "restricted profile exceeded its real cleanup deadline"
    assert not any(
        same_process(pid, identity) for pid, identity in descendants
    ), "restricted profile did not reap its observed native child"
    assert process.returncode == 0, "restricted profile did not exit successfully"


async def read_pipe(handle, maximum=4096):
    import anyio

    while True:
        try:
            return os.read(handle.fileno(), maximum)
        except BlockingIOError:
            await anyio.wait_readable(handle.fileno())


async def write_pipe(handle, raw):
    import anyio

    view = memoryview(raw)
    while view:
        try:
            size = os.write(handle.fileno(), view)
            assert size > 0
            view = view[size:]
        except BlockingIOError:
            await anyio.wait_writable(handle.fileno())


class NativePeer:
    def __init__(self, session, process, status):
        self.session = session
        self.process = process
        self.status = status

    async def call(self, name, arguments=None):
        async with asyncio.timeout(10):
            result = await self.session.call_tool(name, arguments or {})
        assert not result.isError, "real profile rejected a supported conversation call"
        assert result.structuredContent is None
        assert len(result.content) == 1 and result.content[0].type == "text"
        value = json.loads(result.content[0].text)
        assert isinstance(value, dict)
        return value

    async def say(self, key, content, *, kind="message", reply_to=None):
        receipt = await self.call(
            "conversation_say",
            {
                "topic": TOPIC,
                "request_key": key,
                "message": content,
                "kind": kind,
                "reply_to": reply_to,
                "target": None,
                "references": [],
            },
        )
        assert set(receipt) == {
            "schema_version",
            "message_id",
            "session_id",
            "author",
            "stored",
            "visibility",
            "delivery",
            "execution_authorized",
            "record",
        }
        assert type(receipt["schema_version"]) is int and receipt["schema_version"] == 1
        assert receipt["stored"] is True and receipt["execution_authorized"] is False
        assert receipt["visibility"] == "public" and receipt["delivery"] == "submitted"
        assert receipt["author"] == self.status["author"]
        assert receipt["session_id"] == self.status["session_id"]
        record = verify_record(receipt["record"], self.status["swarm_id"])
        assert record == receipt["record"]
        assert record["message_id"] == receipt["message_id"]
        assert record["body"]["author"] == self.status["author"]
        assert record["body"]["public_key"] == self.status["public_key"]
        assert record["body"]["topic"] == TOPIC
        assert record["body"]["swarm_id"] == self.status["swarm_id"]
        assert record["body"]["session_id"] == self.status["session_id"]
        assert record["body"]["request_key"] == key
        assert record["body"]["content"] == content
        assert record["body"]["kind"] == kind
        assert record["body"]["reply_to"] == reply_to
        assert record["body"]["target"] is None
        assert record["body"]["references"] == []
        return record

    async def history(self, *, after=0, watch=False):
        arguments = {
            "topic": TOPIC,
            "after": after,
            "limit": 100,
            "stream_id": self.status["stream_id"],
        }
        if watch:
            arguments["timeout"] = 2
        page = await self.call(
            "conversation_watch" if watch else "conversation_history", arguments
        )
        assert set(page) == {
            "schema_version",
            "session_id",
            "topic",
            "stream_id",
            "messages",
            "next_cursor",
            "has_more",
            "gap",
            "peer_gaps",
        }
        assert page["session_id"] == self.status["session_id"]
        assert page["stream_id"] == self.status["stream_id"]
        assert page["topic"] == TOPIC and page["gap"] is None
        return page

    async def receive(self, expected):
        """Require original signed remote records, using bounded incremental watch."""
        seen, after = {}, 0
        async with asyncio.timeout(35):
            while True:
                page = await self.history(after=after, watch=True)
                assert not profile_exited(self.process)
                for item in page["messages"]:
                    assert item["cursor"] > after
                    record = verify_record(item["record"], self.status["swarm_id"])
                    assert record["message_id"] not in seen
                    seen[record["message_id"]] = record
                assert page["next_cursor"] >= after
                after = page["next_cursor"]
                if set(expected) <= set(seen):
                    assert all(seen[key] == record for key, record in expected.items())
                    return


@asynccontextmanager
async def running_profile(config, node, outcomes):
    """Actual SDK over exact-environment pipes, without stock env inheritance."""
    import anyio
    from mcp import ClientSession, types
    from mcp.shared.message import SessionMessage

    started = time.monotonic()
    startup_deadline = asyncio.get_running_loop().time() + 10
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "src.liminal_bridge.conversation_server",
            "--config",
            str(config),
        ],
        cwd=ROOT,
        env=CHILD_ENVIRONMENT,
        start_new_session=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        close_fds=True,
    )
    incoming, receive = anyio.create_memory_object_stream(1)
    send, outgoing = anyio.create_memory_object_stream(1)
    errors, descendants = [], []
    wire_bytes, stderr_bytes = 0, 0
    closing = False

    def account(raw):
        nonlocal wire_bytes
        assert len(raw) <= 262144
        wire_bytes += len(raw)
        assert wire_bytes <= 1048576

    async def read_stdout():
        buffer = bytearray()
        async with incoming:
            while chunk := await read_pipe(process.stdout):
                account(chunk)
                buffer.extend(chunk)
                while (boundary := buffer.find(b"\n")) >= 0:
                    raw = bytes(buffer[: boundary + 1])
                    del buffer[: boundary + 1]
                    assert len(raw) <= 262144
                    await incoming.send(
                        SessionMessage(types.JSONRPCMessage.model_validate_json(raw))
                    )
                assert len(buffer) < 262144
            assert not buffer, "unterminated native MCP frame"

    async def write_stdin():
        async with outgoing:
            async for message in outgoing:
                raw = (
                    message.message.model_dump_json(by_alias=True, exclude_none=True)
                    + "\n"
                ).encode()
                account(raw)
                async with asyncio.timeout(2):
                    await write_pipe(process.stdin, raw)

    async def read_stderr():
        nonlocal stderr_bytes
        while raw := await read_pipe(process.stderr):
            stderr_bytes += len(raw)
            assert stderr_bytes <= 65536

    async def guarded(coroutine):
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not closing:
                errors.append(type(error).__name__)
                signal_group(process, signal.SIGTERM)

    readers = []
    try:
        for handle in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(handle.fileno(), False)
        readers = [
            asyncio.create_task(guarded(read_stdout())),
            asyncio.create_task(guarded(write_stdin())),
            asyncio.create_task(guarded(read_stderr())),
        ]
        async with ClientSession(
            receive, send, read_timeout_seconds=timedelta(seconds=10)
        ) as session:
            async with asyncio.timeout_at(startup_deadline):
                initialized = await session.initialize()
                assert initialized.capabilities.model_dump(
                    by_alias=True, exclude_none=True
                ) == {"tools": {"listChanged": False}}
                listed = await session.list_tools()
                assert len(listed.tools) == 6
                assert {tool.name for tool in listed.tools} == TOOLS
                assert listed.nextCursor is None
                for tool in listed.tools:
                    assert set(tool.model_dump(by_alias=True, exclude_none=True)) <= {
                        "name",
                        "description",
                        "inputSchema",
                    }
                peer = NativePeer(session, process, None)
                status = await peer.call("conversation_status")
                assert set(status) == STATUS_FIELDS
                assert status["profile"] == "polyphony-public-conversation"
                assert status["topic"] == TOPIC
                assert status["visibility"] == "public"
                assert status["execution_authorized"] is False
                assert (
                    type(status["schema_version"]) is int
                    and status["schema_version"] == 1
                )
                peer.status = status
            startup_seconds = time.monotonic() - started
            children = (
                Path(f"/proc/{process.pid}/task/{process.pid}/children")
                .read_text()
                .split()
            )
            assert len(children) == 1, "profile must own exactly one native sidecar"
            child = int(children[0])
            identity = process_identity(child)
            assert identity is not None
            descendants.append((child, identity))
            assert os.getpgid(child) == process.pid
            assert Path(f"/proc/{child}/exe").resolve() == Path(node).resolve()
            command = Path(f"/proc/{child}/cmdline").read_bytes().split(b"\0")
            assert command == [
                os.fsencode(node),
                os.fsencode(SIDECAR / "bridge.js"),
                status["swarm_id"].encode(),
                b"--conversation-bootstrap-port",
                str(json.loads(config.read_bytes())["bootstrap"]["port"]).encode(),
                b"",
            ]
            environment = dict(
                entry.split(b"=", 1)
                for entry in Path(f"/proc/{child}/environ").read_bytes().split(b"\0")
                if entry
            )
            expected_environment = {
                b"PATH": CHILD_ENVIRONMENT["PATH"].encode(),
                b"LANG": b"C.UTF-8",
                b"POLYPHONY_CONVERSATION_ONLY": b"1",
            }
            assert set(environment) == set(expected_environment)
            assert all(
                environment[key] == value for key, value in expected_environment.items()
            )
            yield peer
    finally:
        closing = True
        try:
            await stop_profile(process, descendants)
        finally:
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            for handle in (process.stdin, process.stdout, process.stderr):
                handle.close()
            await receive.aclose()
            await send.aclose()
        assert not errors, "owned native MCP relay failed"
    outcomes.append(
        {
            "startup_seconds": startup_seconds,
            "wire_bytes": wire_bytes,
            "stderr_bytes": stderr_bytes,
            "native_children_reaped": len(descendants),
            "exit_code": process.returncode,
        }
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("POLYPHONY_REAL_CHAT_TESTS") != "1",
    reason="actual restricted-profile Hyperswarm qualification is explicitly enabled",
)
async def test_real_restricted_profiles_keep_signed_records_across_restart(
    tmp_path, capsys
):
    assert sys.platform == "linux", "native profile qualification requires Linux"
    node = shutil.which("node", path=CHILD_ENVIRONMENT["PATH"])
    assert node, "install Node in the real fixed child PATH before qualification"
    assert (SIDECAR / "node_modules/hyperswarm/package.json").is_file()
    assert (SIDECAR / "node_modules/hyperdht/package.json").is_file()
    version = await asyncio.create_subprocess_exec(
        node, "--version", env=CHILD_ENVIRONMENT, stdout=asyncio.subprocess.PIPE
    )
    try:
        node_version, _ = await asyncio.wait_for(version.communicate(), 5)
    finally:
        if version.returncode is None:
            version.kill()
            await asyncio.wait_for(version.wait(), 5)
    assert version.returncode == 0 and re.fullmatch(rb"v[0-9.]+\s*", node_version)
    tmp_path.chmod(0o700)
    scope = tmp_path / "scope"
    descriptor = os.open(scope, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(secrets.token_hex(32))
    outcomes, statuses, expected = [], {}, {}
    with (tmp_path / "bootstrap.log").open("xb") as log:
        bootstrap = await asyncio.create_subprocess_exec(
            node,
            str(SIDECAR / "verify-bootstrap.js"),
            cwd=ROOT,
            env=CHILD_ENVIRONMENT,
            start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=log,
            limit=1024,
        )
        try:
            endpoint = await asyncio.wait_for(bootstrap.stdout.readline(), 10)
            matched = re.fullmatch(rb"127\.0\.0\.1:([0-9]{1,5})\n", endpoint)
            assert matched, "actual loopback bootstrap did not become ready"
            port = int(matched[1])
            assert 1 <= port <= 65535
            configs = {}
            for name in ("alpha", "beta"):
                config = tmp_path / f"{name}.json"
                private_json(
                    config,
                    {
                        "schema_version": 1,
                        "topic": TOPIC,
                        "scope_key_file": str(scope),
                        "state_directory": str(tmp_path / name),
                        "bootstrap": {"host": "127.0.0.1", "port": port},
                    },
                )
                configs[name] = config
            original_configs = {
                name: path.read_bytes() for name, path in configs.items()
            }
            async with running_profile(configs["alpha"], node, outcomes) as alpha:
                statuses["alpha"] = alpha.status
                assert (
                    alpha.status["swarm_id"]
                    == hashlib.sha256(scope.read_bytes()).hexdigest()
                )
                async with running_profile(configs["beta"], node, outcomes) as beta:
                    statuses["beta"] = beta.status
                    assert alpha.status["author"] != beta.status["author"]
                    assert alpha.status["public_key"] != beta.status["public_key"]
                    assert alpha.status["stream_id"] != beta.status["stream_id"]
                    assert alpha.status["session_id"] == beta.status["session_id"]
                    assert alpha.status["swarm_id"] == beta.status["swarm_id"]
                    first = await alpha.say(
                        "proposal", "I propose a public discussion."
                    )
                    expected[first["message_id"]] = first
                    await beta.receive(expected)
                    second = await beta.say(
                        "decline",
                        "I decline execution and retain my independent view.",
                        kind="decline",
                        reply_to=first["message_id"],
                    )
                    expected[second["message_id"]] = second
                    await alpha.receive(expected)
                third = await alpha.say(
                    "followup",
                    "Retain Beta's dissent while its process is offline.",
                    reply_to=second["message_id"],
                )
                expected[third["message_id"]] = third
                async with running_profile(configs["beta"], node, outcomes) as reopened:
                    assert reopened.status == statuses["beta"]
                    await reopened.receive(expected)
                    replay = await reopened.say(
                        "decline",
                        second["body"]["content"],
                        kind="decline",
                        reply_to=first["message_id"],
                    )
                    assert replay == second
                    for peer in (alpha, reopened):
                        page = await peer.history()
                        assert page["peer_gaps"] == {"count": 0, "items": []}
                        assert len(page["messages"]) == len(expected)
                        assert {
                            item["record"]["message_id"]: item["record"]
                            for item in page["messages"]
                        } == expected
                        assert all(
                            item["reply_state"]
                            == (
                                "resolved"
                                if item["record"]["body"]["reply_to"]
                                else "none"
                            )
                            for item in page["messages"]
                        )
            async with running_profile(configs["alpha"], node, outcomes) as reopened:
                assert reopened.status == statuses["alpha"]
                await reopened.receive(expected)
                assert await reopened.say("proposal", first["body"]["content"]) == first
            assert {
                name: path.read_bytes() for name, path in configs.items()
            } == original_configs
            assert bootstrap.returncode is None
        finally:
            if bootstrap.returncode is None:
                bootstrap.terminate()
            await stop_bootstrap(bootstrap)
    assert len(outcomes) == 4
    with capsys.disabled():
        print(
            json.dumps(
                {
                    "qualification": "actual-restricted-module-mcp-native-conversation",
                    "passed": True,
                    "topology": "explicit-loopback-bootstrap",
                    "node_executable": node,
                    "node_version": node_version.decode().strip(),
                    "independent_identities": 2,
                    "original_signed_message_ids": sorted(expected),
                    "original_records_and_causal_replies_retained": True,
                    "both_identities_and_streams_reopened": True,
                    "same_key_replay_created_no_duplicate": True,
                    "profile_lifetimes": outcomes,
                    "provider": "not configured or invoked by this native component trial",
                    "installed_openjules_trial": False,
                },
                sort_keys=True,
            ),
            flush=True,
        )
