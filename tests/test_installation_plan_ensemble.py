"""One-time #139 development coordination on the supported native runner.

The messages were independently authored before this branch was published.
This harness transports their exact text through the unmodified ensemble_chat
implementation. It is not an external swarm approval or authority qualification.
"""

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets

import pytest

from src.liminal_bridge.mesh import LiminalMesh
from src.liminal_bridge.storage import SQLiteStorageProvider

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "architecture-kp139-installation-validation"
BASE_REVISION = "d5f0cd14c1940e1bd55db1dc5d4569a5d14fb359"

PROPOSAL = """Architecture plan for KP #139, authored by ChatGPT agent /root/kp_explore on 15 November 2026, before authority implementation, against main d5f0cd14c1940e1bd55db1dc5d4569a5d14fb359 and issue139 comment6869132293.

Expose the producer-owned managed-installation-v1 contract through python -m src.liminal_bridge.managed_cli validate-installation --credentials PATH [--managed-config PATH] --validation-contract managed-installation-v1 --json. Publish contracts/managed-installation-v1.json and matching root-only, child-enabled and child-disabled fixtures. Its new static contract digest uses sorted compact ASCII-escaped JSON, UTF-8 and no nonfinite numbers. Existing profile digest encoding and profile values remain unchanged.

Extract and reuse the existing pure managed-policy, principal-scope and credential-role validation. Use one capability projection for the live endpoint and offline descriptor. Move startup validation before constructing Coordinator, GitWorktreeAdapter or a listener. Keep the current literal-loopback host and credential permission predicate, including private 0400/0600 files. Preserve valid unscoped owner/reviewer/conductor configurations and current role behavior. Validate principal scope syntax; do not reinterpret syntactically valid but currently unauthorized scopes as malformed or require new role coverage.

The closed result keys are schema_version, service, validation_contract, contract_digest, valid, managed_enabled, descriptor and errors. Enabled descriptors contain protocol, deployment_id and profiles sorted by repository/name. Profiles contain exactly repository, profile, base, profile_digest, attempt_seconds, capabilities, managed_dispatch, external_root, children, child_grants, child_initial_outcome, child_policy, profile_supported, max_attempts and budget_seconds. Preserve children:false and existing child policy representations. Omitted managed policy returns valid disabled support and descriptor:null. Invalid input returns managed_enabled:null and descriptor:null with bounded stable errors, never partial approved output. All credential errors use /credentials; no raw values, token-map keys, private paths or JSON/OSError text are exposed.

Read only the explicitly selected inputs, required metadata and static contract. Do not create databases/journals, listeners, subprocesses, provider clients or network/model calls. Resolve no ambient secrets. Static success proves configuration validation only, never availability, permission, execution or browser authority. Emit no credential/configuration digest or producer source-attestation claim. A consumer separately binds the exact producer revision, published contract and private input receipt.

Validate actual CLI output, deterministic repetition and unchanged input bytes, precise descriptors/digests, malformed fields and Boolean numeric aliases, duration/budget/capability bounds, credential role/scope/permission failures, shared live/static projection and startup refusal before stateful constructors. Run required hooks, lint and full deterministic tests. Update the existing managed-runtime guide and source map. Actual pinned producer/consumer composition remains a separate required acceptance step before issue139 closes; no installation or runtime qualification is claimed by this producer slice.

The independent reviewer requirements are incorporated in this proposal. The following review is a separately authored agent contribution. This isolated channel establishes development coordination only, not approval from an existing external swarm or a production deployment."""

REVIEW = """Independent architecture review for KP #139, authored by ChatGPT agent /root/transition_contracts on 15 November 2026, before authority implementation. I reviewed the current native validators, service startup, capability projection and the settled contract in issue139 comment6869132293. I did not author the implementation plan.

The bounded plan is sound: expose managed-installation-v1 through the existing managed CLI, reuse pure producer-owned policy/principal/credential validation and one capability projection, then validate the same inputs before Coordinator, GitWorktreeAdapter or listener creation. Preserve canonical profile serialization and digests, original authority behavior and separate installed acceptance.

The following requirements resolve my review concerns. Preserve existing valid unscoped owner/reviewer/conductor configurations. Reject malformed role/scope data without silently making role coverage or browser-style scope mandatory. Distinguish a syntactically valid credential scope absent from the configured policy, which existing request authorization refuses, from an invalid principal scope; document any intentional compatibility change. Keep the existing credential permission predicate, which admits owner-only 0400 as well as 0600. Safe errors must not include token-map keys, raw configuration values, private filesystem paths or raw JSON/OSError messages; an invalid result contains no partial approved descriptor. Credential errors use only /credentials. Root-only and child-enabled/disabled profiles retain existing capability and digest representations. Omitted managed policy is explicitly disabled.

Tests should exercise actual CLI output, deterministic repeated validation, malformed numeric/Boolean aliases and credentials, shared live/static projection, and startup refusal before either stateful constructor. No database, journal, network, subprocess, provider or model effect belongs to offline validation. Contract digest is a versioned static-artifact identity, not credential/configuration hashing or source attestation. Static success is not execution permission, browser authority or runtime qualification. Issue139 completion still requires the pinned producer/consumer installation acceptance specified in the issue.

With those requirements incorporated, I have no unresolved architectural conflict with this bounded plan. This is independent agent development review. These independently authored messages may be transported through the actual signed ensemble_chat implementation on the supported isolated runner; they do not represent an existing external swarm's approval, human approval or a production deployment."""


async def wait_for(predicate, description):
    try:
        async with asyncio.timeout(45):
            while not predicate():
                await asyncio.sleep(0.05)
    except TimeoutError:
        raise AssertionError(description) from None


def load_server(directory, name, endpoint, key, monkeypatch):
    monkeypatch.setenv("SWARM_KEY", key)
    monkeypatch.setenv("LIMINAL_COLLAB_DB", str(directory / (name + "-ledger.db")))
    monkeypatch.setenv("DUCKY_API_KEY", "")
    monkeypatch.setenv("DUCKY_PROVIDER", "disabled")
    monkeypatch.setenv("MQTT_HOST", "")
    monkeypatch.setenv("BLE_ENABLED", "false")
    spec = importlib.util.spec_from_file_location(
        "src.liminal_bridge.coordination_" + name,
        ROOT / "src/liminal_bridge/server.py",
    )
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    server.mesh = LiminalMesh(
        key,
        storage_provider=SQLiteStorageProvider(str(directory / (name + ".db"))),
        identity_path=str(directory / (name + ".pem")),
        bootstrap=endpoint,
        snapshot_path=str(directory / (name + ".snapshot.json")),
    )
    server.mesh._gossip_interval_seconds = 0.25
    return server


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("POLYPHONY_REAL_CHAT_TESTS") != "1",
    reason="actual #139 ensemble coordination requires the opt-in native runner",
)
async def test_actual_installation_plan_and_independent_review(
    tmp_path, monkeypatch, capsys
):
    # This opt-in pins real Hyperswarm sockets to loopback. No transport stub,
    # dependency patch, fake running flag or alternate channel is installed.
    monkeypatch.setenv("POLYPHONY_LOCAL_MEASUREMENT", "1")
    monkeypatch.setenv("POLYPHONY_MEASUREMENT_LOOPBACK", "1")
    bootstrap = await asyncio.create_subprocess_exec(
        "node",
        str(ROOT / "src/liminal_bridge/sidecar/verify-bootstrap.js"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    servers = []
    try:
        endpoint = (
            (await asyncio.wait_for(bootstrap.stdout.readline(), 15)).decode().strip()
        )
        assert endpoint.startswith("127.0.0.1:"), (
            await bootstrap.stderr.read(4096)
        ).decode(errors="replace")
        key = secrets.token_hex(32)
        for name in ("proposer", "reviewer"):
            server = load_server(tmp_path, name, endpoint, key, monkeypatch)
            servers.append(server)
            await server.mesh.start()
            await server.set_status("busy")
        proposer, reviewer = servers
        assert proposer.mesh.node_id != reviewer.mesh.node_id
        await wait_for(
            lambda: reviewer.mesh.node_id in proposer.mesh.peer_map.values()
            and proposer.mesh.node_id in reviewer.mesh.peer_map.values(),
            "both real peers must discover and authenticate each other",
        )
        await proposer.ensemble_chat(TOPIC, PROPOSAL, request_key="kp139-plan-20261115")
        await wait_for(
            lambda: len(reviewer.mesh.conversation.store.history(TOPIC)["messages"])
            == 1,
            "actual proposal must arrive at the independent reviewer node",
        )
        received = json.loads(await reviewer.get_ensemble_chat(TOPIC))
        assert received[0]["content"] == PROPOSAL
        assert received[0]["sender"] == proposer.mesh.node_id
        proposal_id = received[0]["message_id"]
        await reviewer.ensemble_chat(
            TOPIC, REVIEW, request_key="kp139-review-20261115", reply_to=proposal_id
        )
        await wait_for(
            lambda: all(
                len(server.mesh.conversation.store.history(TOPIC)["messages"]) == 2
                for server in servers
            ),
            "both real nodes must retain the exact independent contributions",
        )
        histories = [
            json.loads(await server.get_ensemble_chat(TOPIC)) for server in servers
        ]
        assert histories[0] == histories[1]
        entries = {item["content"]: item for item in histories[0]}
        assert set(entries) == {PROPOSAL, REVIEW}
        assert all(item["authenticated"] for item in entries.values())
        assert entries[REVIEW]["sender"] == reviewer.mesh.node_id
        assert entries[REVIEW]["reply_to"] == proposal_id
        assert entries[REVIEW]["reply_state"] == "resolved"
        proof = {
            "coordination": "kp139-actual-ensemble-chat",
            "passed": True,
            "base_revision": BASE_REVISION,
            "runner_revision": os.environ.get("GITHUB_SHA"),
            "topic": TOPIC,
            "server_source_sha256": hashlib.sha256(
                (ROOT / "src/liminal_bridge/server.py").read_bytes()
            ).hexdigest(),
            "transport": "real-loopback-hyperswarm",
            "model_calls": 0,
            "external_swarm_approval": False,
            "authority_implementation_in_branch": False,
            "messages": [
                {
                    "agent": agent,
                    "node_id": entries[message]["sender"],
                    "message_id": entries[message]["message_id"],
                    "reply_to": entries[message]["reply_to"],
                    "content_sha256": hashlib.sha256(message.encode()).hexdigest(),
                }
                for agent, message in (
                    ("/root/kp_explore", PROPOSAL),
                    ("/root/transition_contracts", REVIEW),
                )
            ],
        }
        with capsys.disabled():
            print(
                "KP139_ENSEMBLE_REPORT " + json.dumps(proof, sort_keys=True), flush=True
            )
    finally:
        for server in servers:
            await server.set_status("idle")
            await server.mesh.stop()
            server.mesh.storage.conn.close()
        if bootstrap.returncode is None:
            bootstrap.terminate()
            await asyncio.wait_for(bootstrap.wait(), 10)
