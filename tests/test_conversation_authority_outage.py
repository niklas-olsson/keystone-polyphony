"""Real authority outage and independent peer chat, without an executor or model."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import sys

from aiohttp import ClientConnectorError, ClientSession, ClientTimeout
import pytest

from src.liminal_bridge.conversation import verify_record
from src.liminal_bridge.mesh import LiminalMesh
from src.liminal_bridge.storage import SQLiteStorageProvider

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "authority-outage-discussion"
BASE = "a" * 40  # Synthetic approved profile; this trial never starts an executor.


def private_json(path, value):
    with path.open("x") as handle:
        path.chmod(0o600)
        json.dump(value, handle)


async def stop_process(process):
    """Reap a real subprocess boundedly, failing if graceful shutdown failed."""
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(process.wait(), 5)
        raise AssertionError(
            "qualification subprocess required forced shutdown"
        ) from None


async def stop_mesh(mesh):
    # Production stop() does not bound its process wait and can swallow cancellation.
    # Retain the actual process so cleanup can still terminate/kill/reap it.
    process = mesh.process
    try:
        async with asyncio.timeout(5) as deadline:
            await mesh.stop()
        assert not deadline.expired(), "mesh shutdown exceeded its deadline"
    finally:
        try:
            await stop_process(process)
        finally:
            mesh.storage.conn.close()


async def wait_for(predicate, description, processes=()):
    try:
        async with asyncio.timeout(35):
            while not predicate():
                assert all(
                    process.returncode is None for process in processes
                ), description
                await asyncio.sleep(0.05)
    except TimeoutError:
        raise AssertionError(description) from None
    assert all(process.returncode is None for process in processes), description


class AuthorityTrial:
    """Actual coordinator CLI and HTTP. Every request uses a fresh connection."""

    def __init__(self, directory, port):
        directory.mkdir(mode=0o700)
        self.directory = directory
        self.url = f"http://127.0.0.1:{port}"
        self.port = port
        self.process = None
        self.scope = {
            "deployment_id": "outage-" + secrets.token_hex(8),
            "repository": "github:github.com/example/outage-trial",
        }
        self.tokens = {role: secrets.token_hex(32) for role in ("ingress", "runtime")}
        private_json(
            directory / "credentials.json",
            {
                token: {
                    "identity": "outage-" + role,
                    "role": role,
                    "deployment_id": self.scope["deployment_id"],
                    "repositories": [self.scope["repository"]],
                }
                for role, token in self.tokens.items()
            },
        )
        private_json(
            directory / "managed.json",
            {
                "schema_version": 1,
                "deployment_id": self.scope["deployment_id"],
                "repositories": {
                    self.scope["repository"]: {
                        "checks": ["tests"],
                        "reviewers": ["independent-reviewer"],
                        "max_attempts": 2,
                        "budget_seconds": 120,
                        "profiles": {
                            "approved": {
                                "base": BASE,
                                "attempt_seconds": 60,
                                "capabilities": [],
                            }
                        },
                    }
                },
            },
        )

        self.log = (directory / "authority.log").open("xb")

    async def request(self, method, path, role="runtime", body=None):
        async with ClientSession(
            timeout=ClientTimeout(total=2),
            trust_env=False,
            headers={
                "Authorization": "Bearer " + self.tokens[role],
                "Content-Type": "application/json",
            },
        ) as client:
            async with client.request(
                method,
                self.url + path,
                data=body,
                params=self.scope if method == "GET" else None,
                allow_redirects=False,
            ) as response:
                assert response.status == 200, "actual managed HTTP operation failed"
                return await response.json()

    async def start(self):
        assert self.process is None or self.process.returncode is not None
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "src.liminal_bridge.coordinator_service",
            "--db",
            str(self.directory / "authority.db"),
            "--credentials",
            str(self.directory / "credentials.json"),
            "--managed-config",
            str(self.directory / "managed.json"),
            "--port",
            str(self.port),
            cwd=ROOT,
            env={
                key: os.environ[key]
                for key in ("PATH", "TMPDIR", "LD_LIBRARY_PATH")
                if key in os.environ
            },
            stdin=asyncio.subprocess.DEVNULL,
            stdout=self.log,
            stderr=self.log,
        )
        async with asyncio.timeout(15):
            while True:
                assert (
                    self.process.returncode is None
                ), "actual authority failed to start"
                try:
                    result = await self.request("GET", "/managed/snapshot")
                except ClientConnectorError:
                    await asyncio.sleep(0.05)
                    continue
                assert all(result[key] == value for key, value in self.scope.items())
                return

    async def offline_claim(self, body):
        assert self.process.returncode is not None, "authority must actually be exited"
        # HTTP error responses and timeouts do not substitute for connector failure.
        with pytest.raises(ClientConnectorError):
            await self.request("POST", "/managed/claim", body=body)

    @asynccontextmanager
    async def outage(self):
        try:
            await self.start()
            task = (
                await self.request(
                    "POST",
                    "/managed/enroll",
                    role="ingress",
                    body=json.dumps(
                        {
                            "schema_version": 1,
                            **self.scope,
                            "source": "qualification:authority-outage",
                            "request_key": "enroll-once",
                            "title": "Synthetic authority outage trial",
                            "requirements": "Recover allocation without starting an executor.",
                        }
                    ).encode(),
                )
            )["task"]
            before = await self.request("GET", "/managed/snapshot")
            assert len(before["task"]) == 1 and before["attempt"] == []
            assert all(before["task"][0][key] == value for key, value in task.items())
            assert task["attempts"] == task["allocated_seconds"] == 0
            assert all(
                before[kind] == []
                for kind in ("candidate", "work", "evidence", "response", "resource")
            )
            # Serialize once; all four HTTP submissions use byte-identical input.
            claim_body = json.dumps(
                {
                    "schema_version": 1,
                    **self.scope,
                    "profile": "approved",
                    "worker_id": "outage-worker",
                    "task_id": task["id"],
                    "request_key": "allocate-once",
                    "base": BASE,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            await stop_process(self.process)
            await self.offline_claim(claim_body)
            yield
            # The complete peer/restart phase precedes this fresh failed connection.
            await self.offline_claim(claim_body)
            await self.start()
            recovered = await self.request("GET", "/managed/snapshot")
            assert (
                recovered == before
            ), "conversation alone must cause no canonical effects"
            first = await self.request("POST", "/managed/claim", body=claim_body)
            retry = await self.request("POST", "/managed/claim", body=claim_body)
            assert first["attempt"] == retry["attempt"]
            assert first["binding"] == retry["binding"]
            assert (
                first["requirements"] == retry["requirements"] == task["requirements"]
            )
            assert first["launch_allowed"] is retry["launch_allowed"] is True
            assert first["attempt"]["state"] == "unknown"
            assert first["attempt"]["termination_confirmed"] is False
            assert first["binding"]["attempt_id"] == first["attempt"]["id"]
            after = await self.request("GET", "/managed/snapshot")
            assert len(after["task"]) == len(after["attempt"]) == 1
            assert after["task"][0]["id"] == task["id"]
            assert after["task"][0]["attempts"] == 1
            assert after["task"][0]["allocated_seconds"] == 60
            assert after["task"][0]["revision"] == task["revision"]
            assert after["attempt"][0]["id"] == first["attempt"]["id"]
            assert after["candidate"] == after["work"] == after["evidence"] == []
        finally:
            try:
                await stop_process(self.process)
            finally:
                self.log.close()


@pytest.mark.asyncio
async def test_actual_authority_exit_and_same_key_allocation_recovery(
    tmp_path, unused_tcp_port
):
    """Actual HTTP/process recovery can run without qualifying peer transport."""
    async with AuthorityTrial(tmp_path / "authority", unused_tcp_port).outage():
        pass


def make_peer(directory, name, swarm_key, bootstrap):
    mesh = LiminalMesh(
        swarm_key,
        storage_provider=SQLiteStorageProvider(str(directory / (name + ".db"))),
        identity_path=str(directory / (name + ".pem")),
        snapshot_path=str(directory / (name + ".snapshot.json")),
        bootstrap=bootstrap,
        mqtt_host=None,
        ble_enabled=False,
    )
    mesh._gossip_interval_seconds = 0.25
    return mesh


def history(mesh):
    return mesh.conversation.store.history(TOPIC, page_limit=10)["messages"]


def connected(a, b):
    return (
        a.peers
        and b.peers
        and a.node_id in b.peer_map.values()
        and b.node_id in a.peer_map.values()
    )


def assert_records(peers, expected):
    for mesh in peers:
        records = history(mesh)
        assert len(records) == len(expected)
        assert {item["record"]["message_id"] for item in records} == set(expected)
        for item in records:
            record = verify_record(item["record"], mesh.topic)
            author, reply, content = expected[record["message_id"]]
            assert record["body"]["author"] == author
            assert record["body"]["reply_to"] == reply
            assert record["body"]["content"] == content
            assert item["reply_state"] == ("resolved" if reply else "none")


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("POLYPHONY_REAL_CHAT_TESTS") != "1",
    reason="real Hyperswarm outage qualification is explicitly enabled and mandatory in CI",
)
async def test_real_peers_keep_conversing_during_authority_outage(
    tmp_path, unused_tcp_port
):
    """Independent real transport; no authority mock, executor, provider or delivery stub."""
    tmp_path.chmod(0o700)
    assert (
        ROOT / "src/liminal_bridge/sidecar/node_modules/hyperswarm"
    ).is_dir(), "install pinned sidecar dependencies with npm ci before qualification"
    swarm_key = secrets.token_hex(32)
    peers, processes = [], []
    bootstrap = None
    with (tmp_path / "bootstrap.log").open("xb") as log:
        try:
            bootstrap = await asyncio.create_subprocess_exec(
                "node",
                str(ROOT / "src/liminal_bridge/sidecar/verify-bootstrap.js"),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=log,
            )
            endpoint = (
                (await asyncio.wait_for(bootstrap.stdout.readline(), 15))
                .decode()
                .strip()
            )
            assert endpoint.startswith(
                "127.0.0.1:"
            ), "actual loopback bootstrap must start"
            for name in ("alice", "bob"):
                mesh = make_peer(tmp_path, name, swarm_key, endpoint)
                peers.append(mesh)
                await asyncio.wait_for(mesh.start(), 15)
                processes.append(mesh.process)
            a, b = peers
            assert a.node_id != b.node_id
            await wait_for(
                lambda: connected(a, b), "both peers must authenticate", processes
            )
            async with AuthorityTrial(tmp_path / "authority", unused_tcp_port).outage():
                proposal = "I propose continuing discussion."
                disagreement = (
                    "I disagree; canonical allocation must wait for its authority."
                )
                followup = "Retain the disagreement while the peer restarts."
                first = await a.conversation.say(
                    TOPIC, proposal, request_key="proposal"
                )
                await wait_for(
                    lambda: len(history(b)) == 1,
                    "the proposal must reach the other peer",
                    processes,
                )
                second = await b.conversation.say(
                    TOPIC,
                    disagreement,
                    request_key="disagreement",
                    reply_to=first["message_id"],
                )
                expected = {
                    first["message_id"]: (a.node_id, None, proposal),
                    second["message_id"]: (
                        b.node_id,
                        first["message_id"],
                        disagreement,
                    ),
                }
                await wait_for(
                    lambda: len(history(a)) == len(history(b)) == 2,
                    "both contributions must arrive",
                    processes,
                )
                assert_records((a, b), expected)
                stream = b.conversation.store.stream_id
                await stop_mesh(b)
                await wait_for(
                    lambda: not a.peers,
                    "peer disconnect must be observed",
                    (processes[0],),
                )
                third = await a.conversation.say(
                    TOPIC,
                    followup,
                    request_key="offline-follow-up",
                    reply_to=second["message_id"],
                )
                restored = make_peer(tmp_path, "bob", swarm_key, endpoint)
                peers.append(restored)
                assert restored.node_id == b.node_id
                assert restored.conversation.store.stream_id == stream
                assert_records((restored,), expected)
                expected[third["message_id"]] = (
                    a.node_id,
                    second["message_id"],
                    followup,
                )
                await asyncio.wait_for(restored.start(), 15)
                processes.append(restored.process)
                await wait_for(
                    lambda: connected(a, restored) and len(history(restored)) == 3,
                    "restarted peer must authenticate and durably catch up during authority outage",
                    (processes[0], processes[2]),
                )
                assert_records((a, restored), expected)
                assert all(mesh._sidecar_restart_count == 0 for mesh in peers)
                assert a.process is processes[0] and restored.process is processes[2]
        finally:
            outcomes = await asyncio.gather(
                *(stop_mesh(mesh) for mesh in peers), return_exceptions=True
            )
            try:
                await stop_process(bootstrap)
            finally:
                for outcome in outcomes:
                    if isinstance(outcome, BaseException):
                        raise outcome
    # Only report success after all process and storage cleanup also succeeded.
    print(
        json.dumps(
            {
                "qualification": "real-peer-conversation-during-authority-process-outage",
                "passed": True,
                "independent_peers": 2,
                "remotely_retained_messages": 3,
                "causal_dissent": True,
                "peer_restart_catch_up_while_authority_offline": True,
                "offline_claim_connector_failures": 2,
                "canonical_attempts_before_online_retry": 0,
                "canonical_attempts_after_duplicate_online_retry": 1,
                "executor_started": False,
                "model_calls": 0,
                "managed_agent_public_chat_bridge_qualified": False,
                "wire_bytes_measured": False,
            },
            sort_keys=True,
        )
    )
