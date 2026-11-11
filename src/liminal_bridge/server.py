"""MCP and daemon entrypoints. Supports both package and direct-script execution."""

import sys
from pathlib import Path

# Direct script invocations have no package context. Establish one before relative imports.
if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "src.liminal_bridge"

try:
    from .coordinator import managed_task
except ImportError:
    from coordinator import managed_task

import asyncio
import os
import argparse
import time
import hashlib
import json
import secrets
import hmac

# Get the directory containing this script
current_dir = os.path.dirname(os.path.abspath(__file__))
# Add it to sys.path so we can import 'mesh', 'architect', 'pulse'
sys.path.append(current_dir)

# Handle both relative and absolute imports
try:
    from .mesh import LiminalMesh  # noqa: E402
    from .architect import Architect  # noqa: E402
    from .pulse import Pulse  # noqa: E402
    from .dashboard import DashboardServer  # noqa: E402
    from .auth import get_or_create_swarm_key  # noqa: E402
    from .storage import (
        BaseStorageProvider,
        SQLiteStorageProvider,
        RedisStorageProvider,
        PostgresStorageProvider,
    )  # noqa: E402
except (ImportError, ValueError):
    from mesh import LiminalMesh  # noqa: E402
    from architect import Architect  # noqa: E402
    from pulse import Pulse  # noqa: E402
    from dashboard import DashboardServer  # noqa: E402
    from auth import get_or_create_swarm_key  # noqa: E402
    from storage import (
        BaseStorageProvider,
        SQLiteStorageProvider,
        RedisStorageProvider,
        PostgresStorageProvider,
    )  # noqa: E402

try:
    from src.core.collaboration import (
        FencedLedger,
        CollaborationCoordinator,
        DeterministicExecutionAdapter,
        EnsembleConductor,
        ReviewVerdict,
        HostSignalType,
        HostSignalStatus,
        ConductorLeaseStatus,
        ConductorLease,
        HostTelemetrySignal,
    )
except ImportError:
    from core.collaboration import (
        FencedLedger,
        CollaborationCoordinator,
        DeterministicExecutionAdapter,
        EnsembleConductor,
        ReviewVerdict,
        HostSignalType,
        HostSignalStatus,
        ConductorLeaseStatus,
        ConductorLease,
        HostTelemetrySignal,
    )

from mcp.server.fastmcp import FastMCP  # noqa: E402

# Fenced Collaboration Protocol Engine
COLLAB_DB_PATH = os.getenv("LIMINAL_COLLAB_DB", "collab_ledger.db")
collab_ledger = FencedLedger(COLLAB_DB_PATH)
collab_adapter = DeterministicExecutionAdapter()
collab_coordinator = CollaborationCoordinator(collab_ledger, collab_adapter)
collab_conductor = EnsembleConductor(collab_ledger)

# Initialize components
SWARM_KEY = get_or_create_swarm_key()
HOST_CONTROL_KEY = os.getenv("OPENJULES_HOST_KEY", "")


def _verify_swarm_key(provided_key: str) -> bool:
    if not provided_key or not SWARM_KEY:
        return False
    return hmac.compare_digest(provided_key.encode("utf-8"), SWARM_KEY.encode("utf-8"))


def _verify_host_key(provided_key: str) -> bool:
    if not provided_key or not HOST_CONTROL_KEY:
        return False
    return hmac.compare_digest(
        provided_key.encode("utf-8"), HOST_CONTROL_KEY.encode("utf-8")
    )


# Use environment variables or defaults for persistence
DB_PATH = os.getenv("LIMINAL_DB", "liminal.db")
IDENTITY_PATH = os.getenv("LIMINAL_IDENTITY", "identity.pem")
STORAGE_BACKEND = os.getenv("LIMINAL_STORAGE_BACKEND", "sqlite")
STORAGE_URL = os.getenv("LIMINAL_STORAGE_URL", None)

# Transport Configuration
MQTT_HOST = os.getenv("MQTT_HOST", None)
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
BLE_ENABLED = os.getenv("BLE_ENABLED", "false").lower() == "true"


def _create_storage_provider(
    backend: str, db_path: str, url: str
) -> "BaseStorageProvider":
    if backend == "redis":
        if not url:
            raise ValueError("Redis backend requires LIMINAL_STORAGE_URL")
        return RedisStorageProvider(url)
    elif backend == "postgres":
        if not url:
            raise ValueError("Postgres backend requires LIMINAL_STORAGE_URL")
        return PostgresStorageProvider(url)
    elif backend == "sqlite":
        return SQLiteStorageProvider(db_path)
    else:
        raise ValueError(f"Unknown storage backend: {backend}")


# Derive stable swarm seed from SWARM_KEY if in seed mode (will be set later if needed)
# For now, default mesh initialization
mesh = None
architect = Architect()
pulse = None  # Will be initialized with mesh
dashboard_server = None  # Will be initialized if requested

# Global args placeholder
args = None

# Global command queue
pending_commands = []

# Create MCP Server
mcp = FastMCP("Keystone-Polyphony")


async def handle_command_request(origin: str, command: dict):
    """Callback for when this node receives a command request."""

    # RBAC Check
    if mesh:
        roles_raw = mesh.get_kv("roles") or []
        origin_role = "user"  # default
        for item in roles_raw:
            try:
                role_entry = json.loads(item)
                if role_entry.get("node_id") == origin:
                    origin_role = role_entry.get("role", "user")
                    break
            except Exception:
                continue

        cmd_type = command.get("type")
        if cmd_type in ["execute", "broadcast"] and origin_role != "admin":
            await mesh.log(
                "warn", f"Unauthorized command execution request from {origin}"
            )
            return

    print(f">>> [Command Request] Received from {origin}: {command}")

    # --- Agent Task Handoff & Context Sync Loop for Pollen Inference ---
    if cmd_type == "run_inference":
        prompt = command.get("prompt", "")
        if prompt:
            print(f">>> [Pollen Compute] Executing inference task for {origin}...")
            try:
                # Set status to busy while processing
                await mesh.set_status("busy")

                # Execute via the Architect (which points to Pollen WASM if configured)
                result = await architect.refine_issue(prompt)

                # Context Sync Loop: stream the result back to the mesh as a Thought
                await mesh.share_thought(f"Inference Result for {origin}:\n{result}")

            except Exception as e:
                await mesh.share_thought(f"Inference Failed: {e}")
            finally:
                await mesh.set_status("idle")
        return

    # Store standard commands in queue
    pending_commands.append(
        {
            "origin": origin,
            "command": command,
            "timestamp": time.time(),
            "id": hashlib.sha256(f"{origin}:{time.time()}".encode()).hexdigest()[:8],
        }
    )

    # Limit queue size
    if len(pending_commands) > 50:
        pending_commands.pop(0)

    # In MCP mode, we mainly log this so the human/agent can see it in logs/stderr.
    if mesh:
        await mesh.log(
            "info", f"Received command request from {origin}: {command.get('type')}"
        )


def ensure_mesh():
    global mesh, pulse
    if mesh is None:
        storage = _create_storage_provider(STORAGE_BACKEND, DB_PATH, STORAGE_URL)
        mesh = LiminalMesh(
            secret_key=SWARM_KEY,
            storage_provider=storage,
            identity_path=IDENTITY_PATH,
            mqtt_host=MQTT_HOST,
            mqtt_port=MQTT_PORT,
            ble_enabled=BLE_ENABLED,
        )
        pulse = Pulse(mesh, architect)
        mesh.on_baton_release = pulse.on_baton_release
        mesh.on_command_request = handle_command_request


def check_warmup(action_type: str) -> str | None:
    """Check if node is still warming up. Returns error message if blocked, None if OK."""
    if mesh is None:
        return None

    if mesh.warmup_complete:
        return None

    status = mesh.get_warmup_status()
    if not status.get("warming_up", False):
        return None

    # Block work actions during warmup
    work_actions = {"claim_task", "complete_task", "acquire_baton", "release_baton"}
    if action_type not in work_actions:
        return None

    remaining = status.get("remaining_silent_timeout", 0)
    elapsed = status.get("elapsed_seconds", 0)

    if status.get("has_conversation"):
        return f"Warming up... Conversation active. Elapsed: {elapsed}s. (Work blocked until conversation ends or 120s timeout)"
    else:
        return f"Warming up... {remaining}s remaining (max 120s silent timeout). Use ensemble_chat to start a conversation."


# --- MCP Tools ---
@mcp.tool()
async def set_status(status: str) -> str:
    """Sets the node status (e.g., 'idle', 'busy') and broadcasts it."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()
    await mesh.set_status(status)
    return f"Status set to: {status}"


@mcp.tool()
async def save_revision(key: str, diff: str) -> str:
    """
    Appends a unified diff text to the granular revision history of a specific key.
    This allows agents to track incremental changesets over time, similar to Etherpad's timeslider.
    """
    ensure_mesh()
    await mesh.save_revision(key, diff)
    return f"Revision successfully saved and broadcasted to mesh for key: {key}"


@mcp.tool()
async def get_revisions(key: str) -> str:
    """
    Retrieves the chronological list of all revisions for a specific key.
    Each revision is a dictionary containing timestamp, author, revision_id, and diff text.
    """
    ensure_mesh()
    revisions = mesh.get_revisions(key)
    return json.dumps(revisions, indent=2)


@mcp.tool()
async def broadcast_command(
    command: str,
    target: str = None,
    capabilities: list[str] = None,
    status_filter: str = None,
) -> str:
    """Sends a command execution request to the swarm. command should be a JSON string or description."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()

    cmd_obj = {"type": "execute", "payload": command}
    await mesh.broadcast_command(
        cmd_obj, target=target, capabilities=capabilities, status_filter=status_filter
    )
    return f"Command broadcasted: {command}"


@mcp.tool()
async def ping(target_node_id: str, message: str = "Ping!") -> str:
    """Sends a direct ping/notification to a specific agent."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()
    await mesh.ping(target_node_id, message)
    return f"Ping sent to {target_node_id}."


@mcp.tool()
async def list_idle_agents() -> str:
    """Lists all nodes currently in 'idle' status."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()

    idle = []
    for node_id, thought in mesh.thoughts.items():
        # mesh.thoughts might be enriched dict or raw string
        if isinstance(thought, dict) and thought.get("status") == "idle":
            idle.append(
                {"node_id": node_id, "capabilities": thought.get("capabilities", [])}
            )
        elif isinstance(thought, str):
            try:
                # Try parsing as JSON first
                data = json.loads(thought)
                if data.get("status") == "idle":
                    idle.append(
                        {
                            "node_id": node_id,
                            "capabilities": data.get("capabilities", []),
                        }
                    )
            except BaseException:
                if "status: idle" in thought.lower():
                    idle.append({"node_id": node_id})

    return json.dumps(idle, indent=2)


@mcp.tool()
async def register_to_swarm(github_secret: str = None) -> str:
    """Initializes the connection to the P2P swarm."""
    global mesh, pulse, dashboard_server

    if github_secret and (not mesh or mesh.secret_key != github_secret):
        if mesh and mesh.running:
            await mesh.stop()
        storage = _create_storage_provider(STORAGE_BACKEND, DB_PATH, STORAGE_URL)
        mesh = LiminalMesh(
            secret_key=github_secret,
            storage_provider=storage,
            identity_path=IDENTITY_PATH,
            mqtt_host=MQTT_HOST,
            mqtt_port=MQTT_PORT,
            ble_enabled=BLE_ENABLED,
        )
        pulse = Pulse(mesh, architect)
        mesh.on_baton_release = pulse.on_baton_release
        mesh.on_command_request = handle_command_request

    ensure_mesh()

    if not mesh.running:
        await mesh.start()

    # Start dashboard if requested and not running
    if args and args.dashboard_port and not dashboard_server:
        dashboard_server = DashboardServer(mesh, port=args.dashboard_port)
        await dashboard_server.start()

    return f"Connected to Liminal Swarm. Node ID: {mesh.node_id}. Topic: {mesh.topic[:8]}..."


@mcp.tool()
async def share_thought(thought: str) -> str:
    """Broadcasts a 'thought'."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()
    await mesh.share_thought(thought)
    return "Thought streamed."


@mcp.tool()
async def acquire_baton(file_path: str) -> str:
    """Acquires an exclusive lock on a file."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()

    err = check_warmup("acquire_baton")
    if err:
        return err

    success = await mesh.acquire_baton(file_path)
    if success:
        return f"SUCCESS: Baton acquired for {file_path}."
    else:
        owner = mesh.batons.get(file_path, "unknown")
        return f"DENIED: {file_path} is currently locked by {owner}."


@mcp.tool()
async def release_baton(file_path: str) -> str:
    """Releases the lock on a file."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()

    err = check_warmup("release_baton")
    if err:
        return err

    await mesh.release_baton(file_path)
    # pulse.on_baton_release is now triggered via callback in mesh
    return f"Baton released for {file_path}."


@mcp.tool()
async def peek_liminal(key: str = None) -> str:
    """Reads the shared state."""
    ensure_mesh()
    if key:
        val = mesh.get_kv(key)
        return str(val) if val else "Key not found."
    return str({"thoughts": mesh.thoughts, "batons": mesh.batons, "kv": mesh.kv_store})


@mcp.tool()
async def consult_architect(context: str) -> str:
    """Manually triggers the Architect."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()
    await pulse.trigger(context=f"manual:{context}")
    plan = mesh.get_kv("master_plan")
    return f"Architect consulted. Plan: {plan}"


@mcp.tool()
async def conversation_say(
    topic: str,
    message: str,
    request_key: str = None,
    reply_to: str = None,
    kind: str = "message",
    target: str = None,
    references: list[dict] = None,
) -> str:
    """Post approved PUBLIC content; references and invitations confer no execution authority.

    Reuse request_key for an identical retry. reply_to is a signed message ID.
    target addresses a peer in a public message; it does not make content private.
    """
    ensure_mesh()
    conversation = mesh.require_conversation()
    if not mesh.running:
        await mesh.start()
    result = await conversation.say(
        topic,
        message,
        request_key=request_key,
        reply_to=reply_to,
        kind=kind,
        target=target,
        references=references,
    )
    return json.dumps(result)


@mcp.tool()
async def conversation_start(topic: str, request_key: str = None) -> str:
    """Start an ordinary public topic, without managed task enrollment or peer control."""
    return await conversation_say(topic, "", request_key=request_key, kind="start")


@mcp.tool()
async def conversation_join(
    topic: str,
    decision: str = "join",
    message: str = "",
    request_key: str = None,
) -> str:
    """Voluntarily join, decline or leave a public conversation; no process is controlled."""
    if not isinstance(decision, str) or decision not in {"join", "decline", "leave"}:
        raise ValueError("conversation participation must be join, decline or leave")
    return await conversation_say(
        topic, message, request_key=request_key, kind=decision
    )


@mcp.tool()
async def conversation_history(
    topic: str,
    after: int = 0,
    limit: int = 100,
    stream_id: str = None,
) -> str:
    """Read a bounded page with local cursor, signed records, causal gaps and continuation.

    Persist stream_id with next_cursor when resuming. Reads do not mark entries read.
    """
    ensure_mesh()
    return json.dumps(
        mesh.require_conversation().store.history(topic, after, limit, stream_id)
    )


@mcp.tool()
async def conversation_watch(
    topic: str,
    after: int = 0,
    limit: int = 100,
    stream_id: str = None,
    timeout: float = 30,
) -> str:
    """Wait up to 30 seconds for a bounded history page, without inference or execution."""
    ensure_mesh()
    conversation = mesh.require_conversation()
    if not mesh.running:
        await mesh.start()
    return json.dumps(await conversation.watch(topic, after, limit, stream_id, timeout))


@mcp.tool()
async def ensemble_chat(
    topic: str,
    message: str,
    request_key: str = None,
    reply_to: str = None,
) -> str:
    """Compatibility wrapper for signed, durable PUBLIC conversation posts."""
    result = json.loads(await conversation_say(topic, message, request_key, reply_to))
    return f"Message {result['message_id']} stored for topic '{topic}'; transport submitted."


@mcp.tool()
async def get_ensemble_chat(topic: str, after: int = 0, limit: int = 100) -> str:
    """Return at most 100 authenticated entries. Continue after the last entry's cursor.

    Use conversation_history for explicit stream/reset and causal-gap metadata.
    Retained unsigned history is available only through get_legacy_ensemble_chat.
    """
    page = json.loads(await conversation_history(topic, after, limit))
    result = []
    for item in page["messages"]:
        body = item["record"]["body"]
        result.append(
            {
                "sender": body["author"],
                "timestamp": body["timestamp"],
                "content": body["content"],
                "message_id": item["record"]["message_id"],
                "cursor": item["cursor"],
                "session_id": body["session_id"],
                "kind": body["kind"],
                "reply_to": body["reply_to"],
                "reply_state": item["reply_state"],
                "authenticated": True,
            }
        )
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_legacy_ensemble_chat(topic: str, limit: int = 100) -> str:
    """Inspect a bounded local legacy ORSet sample, explicitly without verified attribution."""
    ensure_mesh()
    return json.dumps(mesh.legacy_chat(topic, limit))


@mcp.tool()
async def list_ensemble_chats(after: str = "", limit: int = 100) -> str:
    """List at most 100 public topics; continue after the last returned topic name."""
    ensure_mesh()
    page = mesh.require_conversation().store.topics(after, limit)
    return json.dumps([item["topic"] for item in page["topics"]])


@mcp.tool()
async def mark_chat_read(topic: str, through: int = None) -> str:
    """Acknowledge only entries actually returned by local history reads.

    Unseen holes remain unread. This is local display state, never an effect receipt.
    """
    ensure_mesh()
    result = mesh.require_conversation().store.mark_read(topic, through)
    return json.dumps(result)


@mcp.tool()
async def get_unread_chats(after: str = "", limit: int = 100) -> str:
    """List a bounded page of topics with unseen contributions, independent of timestamps."""
    ensure_mesh()
    page = mesh.require_conversation().store.topics(after, limit, unread_only=True)
    return json.dumps([item["topic"] for item in page["topics"]])


@mcp.tool()
async def list_peers() -> str:
    """Lists connected peers in the mesh."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()
    return json.dumps({"peers": list(mesh.peers), "count": len(mesh.peers)})


@mcp.tool()
async def get_health_status() -> str:
    """Returns the operational health of the mesh node."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()
    return json.dumps(mesh.get_health_status())


@mcp.tool()
async def get_backlog() -> str:
    """Returns all tasks from the swarm backlog."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()
    backlog = mesh.get_kv("swarm_backlog") or []
    tasks = []
    for item in backlog:
        try:
            tasks.append(json.loads(item))
        except BaseException:
            continue
    return json.dumps(tasks, indent=2)


@mcp.tool()
async def add_task(title: str, description: str = "", priority: str = "medium") -> str:
    """Adds a new task to the swarm backlog."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()
    import uuid

    task = {
        "id": str(uuid.uuid4()),
        "title": title,
        "description": description,
        "owner": None,
        "status": "todo",
        "priority": priority,
    }
    await mesh.update_set("swarm_backlog", json.dumps(task))
    return f"Task added: {title} (ID: {task['id']})"


@mcp.tool()
async def claim_task(task_id: str) -> str:
    """Claims a task by ID, acquiring a baton for it."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()

    err = check_warmup("claim_task")
    if err:
        return err

    # Check dependencies before claiming
    backlog = mesh.get_kv("swarm_backlog") or []
    tasks = []
    target_task = None
    for item in backlog:
        try:
            t = json.loads(item)
            tasks.append(t)
            if t.get("id") == task_id:
                target_task = t
        except BaseException:
            continue

    if target_task:
        task_statuses = {t.get("id"): t.get("status") for t in tasks if t.get("id")}
        blocked_by = target_task.get("blocked_by", [])
        for dep_id in blocked_by:
            if task_statuses.get(dep_id) != "done":
                return f"Failed to claim task {task_id} - unmet dependency: {dep_id}"

    success = await mesh.acquire_baton(f"task:{task_id}")
    if success:
        backlog = mesh.get_kv("swarm_backlog") or []
        for item in backlog:
            try:
                t = json.loads(item)
                if t.get("id") == task_id:
                    t["owner"] = mesh.node_id
                    t["status"] = "in_progress"
                    await mesh.update_set("swarm_backlog", json.dumps(t))
                    break
            except BaseException:
                continue
        return f"Task {task_id} claimed."
    else:
        return f"Failed to claim task {task_id} - baton denied."


def _task_matches(item, task_id):
    try:
        return json.loads(item).get("id") == task_id
    except (ValueError, AttributeError):
        return False


@mcp.tool()
async def complete_task(task_id: str) -> str:
    """Marks a task as completed and releases its baton."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()

    err = check_warmup("complete_task")
    if err:
        return err

    # Guardrail: Check if task is enrolled under schema_version >= 2
    try:
        enrolled_task = collab_ledger.get_task(task_id)
        if enrolled_task and enrolled_task.schema_version >= 2:
            # Enrolled task cannot be bypassed with simple complete_task call without accepted candidate receipt
            if not enrolled_task.accepted_candidate_id:
                return (
                    f"DENIED: Task {task_id} is enrolled under Collaboration Protocol (schema v{enrolled_task.schema_version}). "
                    "Completion requires an accepted candidate receipt and passing review evidence."
                )
    except Exception:
        pass

    backlog = mesh.get_kv("swarm_backlog") or []
    if managed_task({"id": task_id}) or any(
        managed_task(json.loads(item))
        for item in backlog
        if isinstance(item, str) and _task_matches(item, task_id)
    ):
        return "Managed tasks require coordinator candidate acceptance."
    await mesh.release_baton(f"task:{task_id}")
    for item in backlog:
        try:
            t = json.loads(item)
            if t.get("id") == task_id:
                t["status"] = "done"
                t["owner"] = mesh.node_id
                await mesh.update_set("swarm_backlog", json.dumps(t))
                asyncio.create_task(
                    mesh.log_audit_event("task_completed", {"task_id": task_id})
                )
                break
        except BaseException:
            continue
    return f"Task {task_id} completed."


@mcp.tool()
async def create_enrolled_task(
    task_id: str,
    title: str,
    description: str,
    baseline_sha: str,
    acceptance_criteria: list[str],
    max_budget: float = 100.0,
    max_attempts: int = 5,
) -> str:
    """Creates a durable, fenced task enrolled in the collaboration protocol."""
    task = collab_coordinator.register_task(
        task_id=task_id,
        title=title,
        description=description,
        baseline_sha=baseline_sha,
        acceptance_criteria=acceptance_criteria,
        max_budget=max_budget,
        max_attempts=max_attempts,
    )
    return json.dumps(task.to_dict(), indent=2)


@mcp.tool()
async def claim_enrolled_task(
    task_id: str,
    actor_id: str,
    reserve_cost: float = 10.0,
    lease_duration: float = 300.0,
) -> str:
    """Atomically acquires a bounded assignment lease and monotonic fence token for an enrolled task."""
    ok, handle, reason = collab_coordinator.dispatch_attempt(
        task_id=task_id,
        actor_id=actor_id,
        reserve_cost=reserve_cost,
        lease_duration=lease_duration,
    )
    if not ok or not handle:
        # Contested or rejected: Conductor evaluates nudge
        nudge = collab_conductor.observe_contention_and_nudge(task_id, actor_id)
        result = {
            "success": False,
            "reason": reason,
            "nudge": nudge.to_dict() if nudge else None,
        }
        return json.dumps(result, indent=2)

    return json.dumps(
        {
            "success": True,
            "assignment_id": handle.attempt_id,
            "fence_token": handle.fence_token,
            "actor_id": handle.actor_id,
            "baseline_ref": handle.baseline_ref,
        },
        indent=2,
    )


@mcp.tool()
async def submit_enrolled_contribution(
    task_id: str,
    assignment_id: str,
    actor_id: str,
    fence_token: int,
    request_id: str,
    diff_text: str,
    actual_cost: float,
) -> str:
    """Atomically validates fence token, records candidate, and settles attempt budget."""
    ok, sub, reason = collab_coordinator.submit_attempt(
        task_id=task_id,
        assignment_id=assignment_id,
        actor_id=actor_id,
        fence_token=fence_token,
        request_id=request_id,
        diff_text=diff_text,
        actual_cost=actual_cost,
    )
    if not ok or not sub:
        return json.dumps({"success": False, "reason": reason}, indent=2)

    return json.dumps({"success": True, "submission": sub.to_dict()}, indent=2)


@mcp.tool()
async def evaluate_enrolled_candidate(
    candidate_id: str,
    task_id: str,
    reviewer_actor_id: str,
    verdict: str,
    blocking_findings: int,
    notes: str = "",
) -> str:
    """Records an independent review evaluation for a candidate (enforcing author != reviewer)."""
    try:
        v = ReviewVerdict(verdict.upper())
    except ValueError:
        return json.dumps(
            {"success": False, "reason": f"Invalid verdict: {verdict}"}, indent=2
        )

    ok, receipt, reason = collab_coordinator.record_review(
        candidate_id=candidate_id,
        task_id=task_id,
        reviewer_actor_id=reviewer_actor_id,
        verdict=v,
        blocking_findings=blocking_findings,
        notes=notes,
    )
    if not ok or not receipt:
        return json.dumps({"success": False, "reason": reason}, indent=2)

    return json.dumps(
        {
            "success": True,
            "review_id": receipt.review_id,
            "verdict": receipt.verdict.value,
            "blocking_findings": receipt.blocking_findings,
        },
        indent=2,
    )


# --- Emergent Conductor & OpenJules Host Gated Tools ---


@mcp.tool()
async def claim_emergent_conductor(
    actor_id: str, swarm_key: str, lease_ttl: float = 90.0
) -> str:
    """Emergent claim of singleton Conductor podium by an idle agent inside the swarm."""
    if not _verify_swarm_key(swarm_key):
        return json.dumps(
            {"success": False, "error": "Unauthorized: invalid swarm key"}, indent=2
        )

    ok, lease, reason = collab_ledger.acquire_conductor_lease(
        actor_id=actor_id, lease_ttl=lease_ttl
    )
    if not ok or not lease:
        return json.dumps({"success": False, "reason": reason}, indent=2)

    return json.dumps(
        {
            "success": True,
            "message": reason,
            "data": lease.to_dict(),
        },
        indent=2,
    )


@mcp.tool()
async def step_down_conductor(actor_id: str, fence_token: int, swarm_key: str) -> str:
    """Steps down from the Conductor role so the agent can claim coding or review tasks."""
    if not _verify_swarm_key(swarm_key):
        return json.dumps(
            {"success": False, "error": "Unauthorized: invalid swarm key"}, indent=2
        )

    ok, reason = collab_ledger.release_conductor_lease(
        actor_id=actor_id, fence_token=fence_token
    )
    if not ok:
        return json.dumps({"success": False, "reason": reason}, indent=2)

    return json.dumps(
        {
            "success": True,
            "message": reason,
        },
        indent=2,
    )


@mcp.tool()
async def request_host_scaling(
    actor_id: str,
    signal_type: str,
    requested_worker_delta: int,
    target_task_ids: list[str],
    rationale: str,
    swarm_key: str,
) -> str:
    """Emits a host scaling signal into host_signal_outbox for external execution host (OpenJules) polling."""
    if not _verify_swarm_key(swarm_key):
        return json.dumps(
            {"success": False, "error": "Unauthorized: invalid swarm key"}, indent=2
        )

    try:
        sig_type = HostSignalType(signal_type.upper())
    except ValueError:
        return json.dumps(
            {"success": False, "error": f"Invalid signal type: {signal_type}"},
            indent=2,
        )

    ok, signal, reason = collab_ledger.emit_host_signal(
        actor_id=actor_id,
        signal_type=sig_type,
        requested_worker_delta=requested_worker_delta,
        target_task_ids=target_task_ids,
        rationale=rationale,
    )
    if not ok or not signal:
        return json.dumps({"success": False, "reason": reason}, indent=2)

    return json.dumps(
        {
            "success": True,
            "message": reason,
            "data": signal.to_dict(),
        },
        indent=2,
    )


@mcp.tool()
async def openjules_poll_outbox(host_key: str, status_filter: str = "PENDING") -> str:
    """External execution host (OpenJules) endpoint to poll pending signals. Requires OPENJULES_HOST_KEY."""
    if not HOST_CONTROL_KEY:
        return json.dumps(
            {
                "success": False,
                "error": "Host authentication disabled: OPENJULES_HOST_KEY unset",
            },
            indent=2,
        )

    if not _verify_host_key(host_key):
        return json.dumps(
            {"success": False, "error": "Unauthorized: invalid host key"}, indent=2
        )

    try:
        status_enum = HostSignalStatus(status_filter.upper())
    except ValueError:
        return json.dumps(
            {"success": False, "error": f"Invalid status filter: {status_filter}"},
            indent=2,
        )

    signals = collab_ledger.poll_host_signals(status=status_enum)
    return json.dumps(
        {
            "success": True,
            "data": [s.to_dict() for s in signals],
        },
        indent=2,
    )


@mcp.tool()
async def openjules_ack_signal(
    signal_id: str, new_status: str, response_note: str, host_key: str
) -> str:
    """External execution host (OpenJules) endpoint to acknowledge/fulfill host signals. Requires OPENJULES_HOST_KEY."""
    if not HOST_CONTROL_KEY:
        return json.dumps(
            {
                "success": False,
                "error": "Host authentication disabled: OPENJULES_HOST_KEY unset",
            },
            indent=2,
        )

    if not _verify_host_key(host_key):
        return json.dumps(
            {"success": False, "error": "Unauthorized: invalid host key"}, indent=2
        )

    try:
        status_enum = HostSignalStatus(new_status.upper())
    except ValueError:
        return json.dumps(
            {"success": False, "error": f"Invalid status: {new_status}"}, indent=2
        )

    ok, reason = collab_ledger.acknowledge_host_signal(
        signal_id=signal_id, new_status=status_enum, response_note=response_note
    )
    if not ok:
        return json.dumps({"success": False, "reason": reason}, indent=2)

    return json.dumps(
        {
            "success": True,
            "message": reason,
        },
        indent=2,
    )


@mcp.tool()
async def get_my_node_id() -> str:
    """Returns this node's unique identifier."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()
    return mesh.node_id


@mcp.tool()
async def get_warmup_status() -> str:
    """Returns the current warmup status of the node."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()
    status = mesh.get_warmup_status()
    return json.dumps(status, indent=2)


@mcp.tool()
async def get_pending_commands(clear: bool = False) -> str:
    """Retrieves all pending command execution requests sent to this node."""
    global pending_commands
    result = list(pending_commands)
    if clear:
        pending_commands = []
    return json.dumps(result, indent=2)


@mcp.tool()
async def broadcast_message(message: str, urgency: str = "low") -> str:
    """Broadcasts a raw message to all peers."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()
    await mesh.broadcast({"type": "raw", "content": message}, urgency=urgency)
    return f"Broadcast sent: {message}"


@mcp.tool()
async def notify_agent(target_node_id: str, message: str = "ping") -> str:
    """Sends a high-urgency notification to a specific agent."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()

    cmd_obj = {"type": "ping", "payload": message}
    await mesh.broadcast_command(cmd_obj, target=target_node_id)
    return f"Ping sent to {target_node_id}."


@mcp.tool()
async def restart_sidecar() -> str:
    """Restarts the Node.js sidecar if it has crashed."""
    ensure_mesh()
    if not mesh.running:
        await mesh.start()
    await mesh.restart_sidecar()
    return "Sidecar restarted."


# --- Seed Mode ---
async def run_seed_mode(
    timeout: int = None,
    dashboard_port: int = None,
    node_name: str = None,
    explicit_seed: str = None,
    props: list[str] = None,
):
    """Runs the mesh in seed mode (no MCP server)."""
    global mesh, pulse

    # Determine swarm_seed for identity
    # Priority: 1) explicit seed arg, 2) node-name arg (derived), 3) None (random for AI)
    if explicit_seed:
        swarm_seed = explicit_seed
    elif node_name:
        swarm_seed = hashlib.sha256(f"{SWARM_KEY}:{node_name}".encode()).hexdigest()
    else:
        swarm_seed = None  # Random identity for AI nodes

    storage = _create_storage_provider(STORAGE_BACKEND, DB_PATH, STORAGE_URL)
    mesh = LiminalMesh(
        secret_key=SWARM_KEY,
        storage_provider=storage,
        identity_path=IDENTITY_PATH,
        swarm_seed=swarm_seed,
        mqtt_host=MQTT_HOST,
        mqtt_port=MQTT_PORT,
        ble_enabled=BLE_ENABLED,
    )
    pulse = Pulse(mesh, architect)
    mesh.on_baton_release = pulse.on_baton_release

    if swarm_seed:
        print(
            f"Starting Liminal Swarm Seed Node (Key: {SWARM_KEY[:8]}..., Node Seed: {swarm_seed[:8]}...)"
        )
    else:
        print(
            f"Starting Liminal Swarm Seed Node (Key: {SWARM_KEY[:8]}..., Random Identity)"
        )
    await mesh.start()

    # Process capability tags
    capabilities = ["seed", "compute", "pollen_compute"]
    if props:
        capabilities.extend(props)
    await mesh.advertise_capabilities(capabilities)

    dashboard = None
    if dashboard_port:
        dashboard = DashboardServer(mesh, port=dashboard_port)
        await dashboard.start()

    start_time = time.time()
    last_thought_content = {}

    try:
        while True:
            await asyncio.sleep(1)

            # Check timeout
            if timeout and (time.time() - start_time > timeout):
                print(f"Seed node timeout reached ({timeout}s).")
                break

            # Print flowing thoughts to console
            for node_id, thought in mesh.thoughts.items():
                if isinstance(thought, dict):
                    content = thought.get("content")
                    if content and content != last_thought_content.get(node_id):
                        status = thought.get("status", "unknown")
                        content_str = str(content)
                        short_content = content_str.replace("\n", " ")
                        if len(short_content) > 120:
                            short_content = short_content[:120] + "..."
                        print(
                            f"~~~ [Polyphony] Node {node_id[:8]} ({status}): {short_content}"
                        )
                        last_thought_content[node_id] = content

            # Periodically pulse (every 60s check, but trigger respects cooldown)
            if int(time.time()) % 60 == 0:
                await pulse.trigger(context="seed_heartbeat")
                await pulse.check_baton_health()

            # Autonomous Swarm Behaviors (every 30s)
            if int(time.time()) % 30 == 0:
                # In a real deployment, these would be read from node config
                await mesh.advertise_capabilities(["worker", "compute"])
                task = await mesh.autonomously_pick_task()
                if task:
                    print(f">>> [Autonomy] Node picked task: {task['id']}")

    except asyncio.CancelledError:
        pass
    finally:
        print("Stopping seed node...")
        if dashboard:
            await dashboard.stop()
        await mesh.stop()


# --- Verify Mode ---
async def run_verify_mode():
    """Verify peer discovery and messaging on an isolated local DHT."""
    print("Starting Verification Mode...")

    # Generate random test DB/Identity paths to avoid conflict
    import tempfile
    import shutil

    tmp_dir = tempfile.mkdtemp()
    print(f"Using temp dir: {tmp_dir}")

    bootstrap = None
    try:
        # Node 1: Seed/Anchor
        seed_db = os.path.join(tmp_dir, "seed.db")
        seed_id = os.path.join(tmp_dir, "seed.pem")
        # Use a stable key for the seed to test that part too
        swarm_seed = hashlib.sha256(SWARM_KEY.encode()).hexdigest()

        storage1 = SQLiteStorageProvider(seed_db)
        mesh1 = LiminalMesh(
            secret_key=SWARM_KEY,
            storage_provider=storage1,
            identity_path=seed_id,
            swarm_seed=swarm_seed,
            snapshot_path=os.path.join(tmp_dir, "seed-snapshot.json"),
        )

        # Node 2: Client
        client_db = os.path.join(tmp_dir, "client.db")
        client_id = os.path.join(tmp_dir, "client.pem")
        storage2 = SQLiteStorageProvider(client_db)
        mesh2 = LiminalMesh(
            secret_key=SWARM_KEY,
            storage_provider=storage2,
            identity_path=client_id,
            snapshot_path=os.path.join(tmp_dir, "client-snapshot.json"),
        )

        sidecar_dir = os.path.join(os.path.dirname(__file__), "sidecar")
        await mesh1._ensure_sidecar_deps(sidecar_dir)
        bootstrap = await asyncio.create_subprocess_exec(
            "node",
            os.path.join(sidecar_dir, "verify-bootstrap.js"),
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            address = await asyncio.wait_for(bootstrap.stdout.readline(), timeout=15)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Local DHT bootstrap did not become ready") from exc
        if not address:
            raise RuntimeError("Local DHT bootstrap exited before becoming ready")
        bootstrap_address = address.decode().strip()
        mesh1.bootstrap = mesh2.bootstrap = bootstrap_address
        print(f"Local DHT bootstrap: {bootstrap_address}")

        print(f"Starting Node 1 (Anchor)... ID: {mesh1.node_id}")
        await mesh1.start()

        print(f"Starting Node 2 (Client)... ID: {mesh2.node_id}")
        await mesh2.start()

        print("Waiting for discovery...")
        # Give it up to 30 seconds
        for i in range(30):
            print(
                f"Check {i + 1}/30... Peers: Node1={len(mesh1.peers)}, Node2={len(mesh2.peers)}"
            )
            if len(mesh1.peers) > 0 and len(mesh2.peers) > 0:
                print("SUCCESS: Peers discovered each other!")
                # Optional: Test messaging
                await mesh1.share_thought("Hello from Anchor")
                await asyncio.sleep(1)
                if mesh1.node_id in mesh2.thoughts:
                    print("SUCCESS: Message received!")
                    sys.exit(0)
                else:
                    print("WARNING: Peer connected but message not synced yet.")
                    # Keep waiting a bit
            await asyncio.sleep(1)

        print("FAILURE: Peers did not discover each other within timeout.")
        sys.exit(1)

    finally:
        if "mesh1" in locals() and mesh1.running:
            await mesh1.stop()
        if "mesh2" in locals() and mesh2.running:
            await mesh2.stop()
        if bootstrap and bootstrap.returncode is None:
            try:
                bootstrap.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(bootstrap.wait(), timeout=5)
            except asyncio.TimeoutError:
                bootstrap.kill()
                await bootstrap.wait()
        shutil.rmtree(tmp_dir)


# --- Daemon Mode ---
async def run_daemon_mode(
    node_name: str = None, explicit_seed: str = None, props: list[str] = None
):
    """Runs the mesh as a headless background worker waiting for commands."""
    global mesh, pulse

    # Determine swarm_seed for identity if provided
    if explicit_seed:
        swarm_seed = explicit_seed
    elif node_name:
        swarm_seed = hashlib.sha256(f"{SWARM_KEY}:{node_name}".encode()).hexdigest()
    else:
        swarm_seed = None  # Random identity

    storage = _create_storage_provider(STORAGE_BACKEND, DB_PATH, STORAGE_URL)
    mesh = LiminalMesh(
        secret_key=SWARM_KEY,
        storage_provider=storage,
        identity_path=IDENTITY_PATH,
        swarm_seed=swarm_seed,
        mqtt_host=MQTT_HOST,
        mqtt_port=MQTT_PORT,
        ble_enabled=BLE_ENABLED,
    )
    pulse = Pulse(mesh, architect)
    mesh.on_baton_release = pulse.on_baton_release
    mesh.on_command_request = handle_command_request

    print(f"Starting Liminal Swarm Daemon Node (Key: {SWARM_KEY[:8]}...)")
    await mesh.start()

    # Worker Node Pattern: register, go idle, and listen
    capabilities = ["coder", "tester", "researcher"]
    if props:
        capabilities.extend(props)
    await mesh.advertise_capabilities(capabilities)

    await mesh.set_status("idle")

    try:
        while True:
            await asyncio.sleep(5)
            # In a full agent setup, the local agent process would poll
            # get_pending_commands() via MCP. Here we just keep the mesh
            # alive so the MCP server or agent process can query it.

            # Periodically ensure we broadcast our status if idle too long
            pass
    except asyncio.CancelledError:
        pass
    finally:
        print("Stopping daemon node...")
        await mesh.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["mcp", "seed", "verify", "daemon"],
        default="mcp",
        help="Run mode: 'mcp' server, 'seed' node, 'verify' test, or 'daemon' worker",
    )
    parser.add_argument(
        "--timeout", type=int, default=None, help="Timeout in seconds for seed mode"
    )
    parser.add_argument(
        "--dashboard-port", type=int, default=None, help="Port for the swarm dashboard"
    )
    parser.add_argument(
        "--node-name",
        type=str,
        default=None,
        help="Node name for stable identity (default: random for AI nodes)",
    )
    parser.add_argument(
        "--seed",
        type=str,
        default=None,
        help="Explicit seed for stable identity (overrides --node-name)",
    )
    parser.add_argument(
        "--prop",
        type=str,
        action="append",
        help="Advertise a capability or property tag (e.g. '--prop role=gpu' or '--prop parallel_group=ring_1')",
    )

    parser.add_argument(
        "--storage-backend",
        type=str,
        default=STORAGE_BACKEND,
        choices=["sqlite", "redis", "postgres"],
        help="The storage backend to use for the mesh (default: sqlite)",
    )
    parser.add_argument(
        "--storage-url",
        type=str,
        default=STORAGE_URL,
        help="The connection URL for the storage backend (required for redis/postgres)",
    )

    # FastMCP uses click/typer which might grab args, so we use parse_known_args
    # Use global args so MCP tools can access it
    args, unknown = parser.parse_known_args()

    # Apply cli overrides
    STORAGE_BACKEND = args.storage_backend
    STORAGE_URL = args.storage_url

    if args.mode == "seed":
        try:
            asyncio.run(
                run_seed_mode(
                    args.timeout,
                    args.dashboard_port,
                    args.node_name,
                    args.seed,
                    args.prop,
                )
            )
        except KeyboardInterrupt:
            pass
    elif args.mode == "verify":
        try:
            asyncio.run(run_verify_mode())
        except KeyboardInterrupt:
            pass
    elif args.mode == "daemon":
        try:
            asyncio.run(run_daemon_mode(args.node_name, args.seed, args.prop))
        except KeyboardInterrupt:
            pass
    else:
        # Clean up argv for FastMCP
        ensure_mesh()
        sys.argv = [sys.argv[0]] + unknown
        mcp.run()
