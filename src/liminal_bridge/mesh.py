import asyncio
import json
import hashlib
import time
import os
import base64
from typing import Dict, Optional, Any, Set, Callable, Awaitable
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

try:
    from .observability import LogAggregator
except ImportError:
    from observability import LogAggregator

try:
    from .crdt import CRDT, LWWRegister, PNCounter, GSet, ORSet, RevisionLog
    from .storage import BaseStorageProvider, SQLiteStorageProvider
except ImportError:
    from crdt import CRDT, LWWRegister, PNCounter, GSet, ORSet, RevisionLog
    from storage import BaseStorageProvider, SQLiteStorageProvider


class LiminalMesh:
    def __init__(
        self,
        secret_key: str,
        storage_provider: Optional[BaseStorageProvider] = None,
        identity_path: str = "identity.pem",
        bootstrap: Optional[str] = None,
        swarm_seed: Optional[str] = None,
        snapshot_path: str = ".liminal/snapshot.json",
        snapshot_interval: int = 300,
    ):
        self.secret_key = secret_key
        # Generate topic hash for the swarm
        self.topic = hashlib.sha256(secret_key.encode()).hexdigest()
        self.storage = storage_provider or SQLiteStorageProvider("liminal.db")
        self.identity_path = identity_path
        self.bootstrap = bootstrap
        self.swarm_seed = swarm_seed
        self.snapshot_path = snapshot_path
        self.snapshot_interval = snapshot_interval
        self._snapshot_task: Optional[asyncio.Task] = None

        # Identity Management
        self.private_key = self._load_or_create_identity()
        self.public_key = self.private_key.public_key()

        # Public Key as Hex String for transmission
        pub_bytes = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        self.public_key_hex = pub_bytes.hex()

        # Stable Node ID derived from Public Key
        self.node_id = hashlib.sha256(pub_bytes).hexdigest()[:16]

        # Encryption Setup
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"liminal-mesh-encryption",
        )
        enc_key = base64.urlsafe_b64encode(hkdf.derive(self.secret_key.encode()))
        self.fernet = Fernet(enc_key)

        self.peers: Set[str] = set()
        self.peer_distances: Dict[str, float] = {}  # peer_id -> distance in meters
        self.capabilities: list[str] = []
        self.thoughts: Dict[str, Any] = {}
        self.batons: Dict[str, str] = {}  # resource -> owner_id

        # KV Store now holds CRDT objects
        self.kv_store: Dict[str, CRDT] = {}
        self.vector_clock: Dict[str, int] = {self.node_id: 0}

        # Network Graph
        self.peer_map: Dict[str, str] = {}  # transport_id -> node_id
        self.network_map: Dict[str, list[str]] = {}  # node_id -> [connected_node_ids]

        # Warmup / Idle Detection
        self.join_time: float = 0
        self.last_activity_time: float = 0
        self.warmup_complete: bool = False
        self._idle_threshold_seconds: float = 300  # 5 min = idle
        self._warmup_silent_timeout: float = 120  # max 120s if no conversation
        self._conversation_inactive_timeout: float = (
            30  # no messages = conversation ended
        )
        self._gossip_interval_seconds: float = 30  # gossip heartbeat

        # Persistence
        self.storage.init_db()
        self._load_state()

        self.process: Optional[asyncio.subprocess.Process] = None
        self.running = False
        self._sidecar_dead = False
        self._sidecar_restart_count = 0
        self._sidecar_max_restarts = 100
        self._sidecar_restart_delay = 5  # seconds
        self._monitor_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None

        # Pending lock requests
        self._lock_requests: Dict[str, asyncio.Future] = {}

        # Callbacks for Pulse
        self.on_baton_release: Optional[Callable[[str, str], Awaitable[None]]] = None
        self.on_tandem_sync: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self.on_command_request: Optional[
            Callable[[str, Dict[str, Any]], Awaitable[None]]
        ] = None

        # Observability
        self.log_aggregator = LogAggregator()

    async def _periodic_snapshot(self):
        """Periodically saves a snapshot of the mesh state."""
        loop = asyncio.get_running_loop()
        while self.running:
            try:
                await asyncio.sleep(self.snapshot_interval)
                await loop.run_in_executor(None, self._save_snapshot)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error saving snapshot: {e}")

            # Also periodically broadcast network state
            await self.broadcast_network_state()

    async def _periodic_gossip(self):
        """Periodically broadcasts gossip to keep state in sync."""
        while self.running:
            try:
                await asyncio.sleep(self._gossip_interval_seconds)
                if self.peers:
                    await self.broadcast(
                        {
                            "type": "gossip_request",
                            "origin": self.node_id,
                            "vc": self.vector_clock,
                        },
                        urgency="low",
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in gossip heartbeat: {e}")

    def is_warming_up(self) -> bool:
        """Check if node is still in warmup period."""
        if self.warmup_complete:
            return False

        elapsed = time.time() - self.join_time
        has_conversation = (
            time.time() - self.last_activity_time
        ) < self._conversation_inactive_timeout

        # Timeout after _warmup_silent_timeout if no conversation
        if elapsed > self._warmup_silent_timeout and not has_conversation:
            self.warmup_complete = True
            print(f"[Warmup] Complete (timeout). Node: {self.node_id}")
            return False

        return True

    def get_warmup_status(self) -> Dict[str, Any]:
        """Get current warmup status info."""
        # First check/update warmup state
        self.is_warming_up()

        elapsed = time.time() - self.join_time
        has_conversation = (
            time.time() - self.last_activity_time
        ) < self._conversation_inactive_timeout

        if self.warmup_complete:
            return {"warming_up": False, "reason": "complete"}

        remaining = max(0, int(self._warmup_silent_timeout - elapsed))
        return {
            "warming_up": True,
            "elapsed_seconds": int(elapsed),
            "remaining_silent_timeout": remaining,
            "has_conversation": has_conversation,
        }

    async def _periodic_idle_check(self):
        """Periodically checks if the node has become idle."""
        while self.running:
            try:
                await asyncio.sleep(60)  # Check every minute
                current_time = time.time()
                # If we were busy and now exceed idle threshold
                if getattr(self, "status", "unknown") == "busy":
                    if (
                        current_time - self.last_activity_time
                    ) > self._idle_threshold_seconds:
                        print(
                            f"[Idle] Node {self.node_id} idle for >{self._idle_threshold_seconds}s. Auto-setting status to idle."
                        )
                        await self.set_status("idle")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in idle check: {e}")

    def touch_activity(self):
        """Update last activity time - call on any peer message."""
        self.last_activity_time = time.time()

    def _save_snapshot(self):
        """Saves the current state to a JSON file."""
        # Convert CRDTs to dicts for snapshot
        kv_snapshot = {k: v.to_dict() for k, v in self.kv_store.items()}

        data = {
            "timestamp": time.time(),
            "node_id": self.node_id,
            "kv_store": kv_snapshot,
            "thoughts": self.thoughts,
            "batons": self.batons,
            "vector_clock": self.vector_clock,
        }

        # Ensure directory exists
        dirname = os.path.dirname(self.snapshot_path)
        if dirname and not os.path.exists(dirname):
            os.makedirs(dirname, exist_ok=True)

        # Write to a temp file then rename for atomicity
        temp_path = self.snapshot_path + ".tmp"
        with open(temp_path, "w") as f:
            json.dump(data, f, indent=2)

        os.replace(temp_path, self.snapshot_path)

    def _load_or_create_identity(self) -> ed25519.Ed25519PrivateKey:
        """Loads the identity key pair or creates a new one."""
        if os.path.exists(self.identity_path):
            try:
                with open(self.identity_path, "rb") as f:
                    return serialization.load_pem_private_key(f.read(), password=None)
            except Exception as e:
                print(f"Error loading identity: {e}. Generating new one.")

        # Generate new key
        private_key = ed25519.Ed25519PrivateKey.generate()

        # Save it
        with open(self.identity_path, "wb") as f:
            f.write(
                private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )

        return private_key

    def _deserialize_crdt(self, data: Any) -> CRDT:
        """Helper to deserialize JSON data into a CRDT object."""
        if isinstance(data, dict):
            crdt_type = data.get("type")
            if crdt_type == "lww-register":
                return LWWRegister.from_dict(data)
            elif crdt_type == "pn-counter":
                return PNCounter.from_dict(data)
            elif crdt_type == "g-set":
                return GSet.from_dict(data)
            elif crdt_type == "or-set":
                return ORSet.from_dict(data)
            elif crdt_type == "revision-log":
                return RevisionLog.from_dict(data)
            elif "vc" in data and "value" in data:
                # Legacy wrapped format -> LWWRegister
                return LWWRegister(
                    value=data["value"],
                    timestamp=data.get("timestamp", 0),
                    origin=data.get("origin", "unknown"),
                    vc=data["vc"],
                )

        # Raw value -> LWWRegister with empty VC (should ideally not happen in new system)
        return LWWRegister(value=data, timestamp=0, origin="unknown", vc={})

    def _load_state(self):
        """Loads state from the database."""
        # Load KV
        self.kv_store.update(self.storage.get_all_kv())

        # Load Thoughts
        self.thoughts.update(self.storage.get_all_thoughts())

        # Load Vector Clock
        vc_meta = self.storage.get_metadata("vector_clock")
        if vc_meta is not None:
            if isinstance(vc_meta, str):
                try:
                    self.vector_clock = json.loads(vc_meta)
                except json.JSONDecodeError:
                    self.vector_clock = {self.node_id: 0}
            elif isinstance(vc_meta, dict):
                self.vector_clock = vc_meta
            else:
                self.vector_clock = {self.node_id: 0}
        else:
            self.vector_clock = {self.node_id: 0}

        # Ensure we always have our own entry
        if self.node_id not in self.vector_clock:
            self.vector_clock[self.node_id] = 0

    def _save_kv(self, key: str, value: CRDT):
        """Persists a KV update."""
        self.storage.save_kv(key, value)

    def _save_clock(self):
        """Persists the vector clock."""
        self.storage.save_metadata("vector_clock", self.vector_clock)

    def _save_thought(self, node_id: str, content: Any):
        """Persists a thought."""
        self.storage.save_thought(node_id, content)

    async def join_swarms(self, keys: list[str]):
        """Joins additional swarm topics."""
        for key in keys:
            topic_hex = hashlib.sha256(key.encode()).hexdigest()
            await self._send_to_sidecar("join", {"topic": topic_hex})

    async def leave_swarms(self, keys: list[str]):
        """Leaves swarm topics."""
        for key in keys:
            topic_hex = hashlib.sha256(key.encode()).hexdigest()
            await self._send_to_sidecar("leave", {"topic": topic_hex})

    async def rotate_key(self, new_key: str, grace_period: float = 0):
        """Rotates the swarm key."""
        old_key = self.secret_key

        # Join new swarm immediately
        await self.join_swarms([new_key])

        # Update current key
        self.secret_key = new_key
        self.topic = hashlib.sha256(new_key.encode()).hexdigest()

        # Schedule leave of old swarm
        if grace_period > 0:
            asyncio.create_task(self._delayed_leave(old_key, grace_period))
        else:
            await self.leave_swarms([old_key])

    async def _delayed_leave(self, key: str, delay: float):
        await asyncio.sleep(delay)
        await self.leave_swarms([key])

    async def _ensure_sidecar_deps(self, sidecar_dir: str):
        """Ensures Node.js dependencies are installed asynchronously."""
        node_modules = os.path.join(sidecar_dir, "node_modules")
        if not os.path.exists(node_modules):
            print(f"Installing sidecar dependencies in {sidecar_dir}...")
            try:
                # check if npm is installed
                proc = await asyncio.create_subprocess_exec(
                    "npm",
                    "--version",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if proc.returncode != 0:
                    print("Warning: npm not found. Sidecar may fail to start.")
                    return

                # install dependencies
                install_proc = await asyncio.create_subprocess_exec(
                    "npm",
                    "install",
                    cwd=sidecar_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await install_proc.communicate()

                if install_proc.returncode == 0:
                    print("Dependencies installed successfully.")
                else:
                    print(
                        f"Warning: Failed to install dependencies. Sidecar may fail to start.\nError: {stderr.decode()}"
                    )
            except Exception as e:
                print(f"Error during dependency check: {e}")

    async def start(self):
        """Starts the Node.js sidecar and begins listening."""
        self.running = True

        # Check for idle wakeup and trigger warmup if needed
        current_time = time.time()
        # last_activity_time is 0 on fresh boot
        was_idle = (
            self.join_time > 0
            and self.last_activity_time > 0
            and (current_time - self.last_activity_time) > self._idle_threshold_seconds
        )

        if was_idle or self.join_time == 0:
            self.join_time = current_time
            self.warmup_complete = False
            print(
                f"[Warmup] Warming up after {'idle wake' if was_idle else 'fresh join'}... Node: {self.node_id}"
            )

        # Now initialize activity time for the current session
        self.last_activity_time = current_time

        # Locate the sidecar script
        current_dir = os.path.dirname(os.path.abspath(__file__))
        sidecar_dir = os.path.join(current_dir, "sidecar")
        sidecar_path = os.path.join(sidecar_dir, "bridge.js")

        # Ensure dependencies
        await self._ensure_sidecar_deps(sidecar_dir)

        # Start Node.js process
        args = ["node", sidecar_path, self.topic]
        if self.bootstrap:
            args.extend(["--bootstrap", self.bootstrap])
        if self.swarm_seed:
            args.extend(["--seed", self.swarm_seed])

        self.process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Start reading stdout in background
        asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._read_stderr())

        # Start snapshot task
        self._snapshot_task = asyncio.create_task(self._periodic_snapshot())

        # Start sidecar monitor task for auto-restart
        self._monitor_task = asyncio.create_task(self._monitor_sidecar())

        # Start gossip heartbeat task
        self._gossip_task = asyncio.create_task(self._periodic_gossip())

        # Start idle detection task
        self._idle_task = asyncio.create_task(self._periodic_idle_check())

        # Broadcast initial state (so dashboard sees us)
        asyncio.create_task(self.broadcast_network_state())

        # Register identity
        await self.update_set(
            "identity_registry",
            json.dumps({"node_id": self.node_id, "public_key": self.public_key_hex}),
        )

        print(
            f"LiminalMesh started. Node ID: {self.node_id}. Topic: {self.topic[:8]}..."
        )

    async def restart_sidecar(self):
        """Restarts the Node.js sidecar after a failure."""
        print("Attempting to restart sidecar...")

        if self.process:
            try:
                self.process.terminate()
                await self.process.wait()
            except Exception:
                pass

        current_dir = os.path.dirname(os.path.abspath(__file__))
        sidecar_dir = os.path.join(current_dir, "sidecar")
        sidecar_path = os.path.join(sidecar_dir, "bridge.js")

        args = ["node", sidecar_path, self.topic]
        if self.bootstrap:
            args.extend(["--bootstrap", self.bootstrap])
        if self.swarm_seed:
            args.extend(["--seed", self.swarm_seed])

        self.process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._read_stderr())

        self._sidecar_dead = False
        self.peers.clear()

        print(f"Sidecar restarted. Node ID: {self.node_id}")

        asyncio.create_task(self.broadcast_network_state())

    async def _monitor_sidecar(self):
        """Background task that monitors sidecar health and auto-restarts if dead."""
        while self.running:
            await asyncio.sleep(5)

            if self._sidecar_dead:
                if self._sidecar_restart_count < self._sidecar_max_restarts:
                    print(f"Auto-restart: Sidecar dead (count: {
                            self._sidecar_restart_count + 1}/{
                            self._sidecar_max_restarts}). Restarting in {
                            self._sidecar_restart_delay}s...")
                    await asyncio.sleep(self._sidecar_restart_delay)
                    await self.restart_sidecar()
                    self._sidecar_restart_count += 1
                else:
                    print(
                        f"Auto-restart: Max restarts ({self._sidecar_max_restarts}) reached. Giving up."
                    )

    async def stop(self):
        """Stops the sidecar."""
        self.running = False

        # Cancel all background tasks
        tasks_to_cancel = [
            self._snapshot_task,
            self._monitor_task,
            self._idle_task,
            self._gossip_task,
        ]

        for task in tasks_to_cancel:
            if task:
                task.cancel()

        # Await them to clean up
        for task in tasks_to_cancel:
            if task:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        if self.process:
            try:
                self.process.terminate()
                await self.process.wait()
            except BaseException:
                pass
            self.process = None

    async def _read_stdout(self):
        """Reads JSON messages from the sidecar."""
        if not self.process or not self.process.stdout:
            return

        while self.running:
            line = await self.process.stdout.readline()
            if not line:
                break

            try:
                msg = json.loads(line.decode().strip())
                await self._handle_message(msg)
            except json.JSONDecodeError:
                print(f"DEBUG: Malformed line from sidecar: {line}")
            except Exception as e:
                print(f"Error handling message: {e}")

    async def _read_stderr(self):
        """Reads stderr from the sidecar (logs)."""
        if not self.process or not self.process.stderr:
            return

        while self.running:
            line = await self.process.stderr.readline()
            if not line:
                break
            print(f"SIDECAR LOG: {line.decode().strip()}")

    async def _send_to_sidecar(self, msg_type: str, payload: Any):
        """Sends a JSON command to the sidecar."""
        if not self.process or not self.process.stdin:
            return

        try:
            msg = json.dumps({"type": msg_type, "payload": payload}) + "\n"
            self.process.stdin.write(msg.encode())
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError) as e:
            print(f"Warning: Sidecar disconnected ({e}). Marking peer count as 0.")
            self.peers.clear()
            self._sidecar_dead = True
        except Exception as e:
            print(f"Error sending to sidecar: {e}")

    def _encrypt(self, payload: Any) -> str:
        """Encrypts a payload dictionary to a base64 string."""
        data = json.dumps(payload).encode()
        return self.fernet.encrypt(data).decode()

    def _decrypt(self, encrypted_b64: str) -> Any:
        """Decrypts a base64 string to a payload dictionary."""
        data = self.fernet.decrypt(encrypted_b64.encode())
        return json.loads(data.decode())

    async def log_audit_event(self, event_type: str, details: Dict[str, Any]):
        """Logs an audit event to the persistent CRDT."""
        event = {
            "type": event_type,
            "details": details,
            "timestamp": time.time(),
            "origin": self.node_id,
        }
        await self.update_set("audit_log", json.dumps(event))

    async def broadcast(self, payload: Any, urgency: str = "low"):
        """Broadcasts a payload to all peers."""

        # Attach origin info
        payload["origin"] = self.node_id
        payload["timestamp"] = time.time()
        payload["urgency"] = urgency
        # Include public key for identification (could be optimized to only send once, but simplified here)
        payload["sender_pubkey"] = self.public_key_hex

        # Attach vector clock if not already present (some calls might add it explicitly)
        if "vc" not in payload:
            payload["vc"] = self.vector_clock

        max_dist = max(self.peer_distances.values()) if self.peer_distances else 0.0

        if urgency == "high" or max_dist > 5.0:
            await self.broadcast_macro(payload)
        elif max_dist < 1.0 and self.peer_distances:
            await self.broadcast_micro(payload)
        else:
            # Default fallback to Hyperswarm (DHT)
            encrypted_data = self._encrypt(payload)
            signature = self.private_key.sign(encrypted_data.encode())
            msg = {"e": encrypted_data, "s": signature.hex(), "p": self.public_key_hex}
            await self._send_to_sidecar("broadcast", msg)

    async def broadcast_macro(self, payload: Any):
        """Routes high urgency messages via macro transport (e.g., MQTT/5G).
        Defaults to Hyperswarm if no macro transport is configured."""
        encrypted_data = self._encrypt(payload)
        signature = self.private_key.sign(encrypted_data.encode())
        msg = {"e": encrypted_data, "s": signature.hex(), "p": self.public_key_hex}
        # Placeholder for actual MQTT/5G client integration
        # For now, default to hyperswarm by sending 'broadcast' to sidecar
        await self._send_to_sidecar("broadcast", msg)

    async def broadcast_micro(self, payload: Any):
        """Routes low urgency messages via micro transport (e.g., BLE/Visual).
        Defaults to Hyperswarm if no micro transport is configured."""
        encrypted_data = self._encrypt(payload)
        signature = self.private_key.sign(encrypted_data.encode())
        msg = {"e": encrypted_data, "s": signature.hex(), "p": self.public_key_hex}
        # Placeholder for actual BLE/Visual client integration
        # For now, default to hyperswarm by sending 'broadcast' to sidecar
        await self._send_to_sidecar("broadcast", msg)

    def _increment_clock(self):
        """Increments the local logical clock."""
        self.vector_clock[self.node_id] = self.vector_clock.get(self.node_id, 0) + 1
        self._save_clock()

    def _merge_clock(self, remote_clock: Dict[str, int]):
        """Merges a remote vector clock into the local one."""
        for node, count in remote_clock.items():
            self.vector_clock[node] = max(self.vector_clock.get(node, 0), count)
        self._save_clock()

    def update_peer_distance(self, peer_id: str, distance: float):
        """Updates the tracked distance to a peer."""
        self.peer_distances[peer_id] = distance

    async def _push_welcome_to_peer(self, peer_id: str):
        """Send full swarm state to new/waking peer via broadcast."""
        # Serialize KV store to JSON-friendly format
        kv_serialized = {}
        for k, v in self.kv_store.items():
            try:
                kv_serialized[k] = v.to_dict()
            except BaseException:
                kv_serialized[k] = str(v)

        payload = {
            "type": "welcome",
            "target": peer_id,  # Only intended peer should process this
            "thoughts": self.thoughts,
            "batons": self.batons,
            "kv": kv_serialized,
            "vector_clock": self.vector_clock,
            "origin": self.node_id,
        }
        await self.broadcast(payload, urgency="high")

    async def _handle_message(self, msg: Dict[str, Any]):
        """Dispatches incoming messages."""
        msg_type = msg.get("type")

        if msg_type == "peer_connected":
            print(f"DEBUG: Node {self.node_id} connected to peer {msg.get('peer_id')}")
            peer_id = msg.get("peer_id")
            self.peers.add(peer_id)
            # Re-broadcast my current thought to new peer (as a raw broadcast)
            if self.node_id in self.thoughts:
                thought = self.thoughts[self.node_id]
                await self.broadcast(
                    {
                        "type": "thought",
                        "origin": self.node_id,
                        "content": thought.get("content"),
                        "status": thought.get("status"),
                        "capabilities": thought.get("capabilities"),
                        "timestamp": thought.get("timestamp", time.time()),
                    }
                )
            # Push full state welcome packet to new peer
            await self._push_welcome_to_peer(peer_id)
            await self.broadcast_network_state()

        elif msg_type == "peer_disconnected":
            pid = msg.get("peer_id")
            if pid in self.peers:
                self.peers.remove(pid)
            await self.broadcast_network_state()

        elif msg_type == "message":
            # Track activity from any peer message
            self.touch_activity()

            payload = msg.get("payload", {})

            # Verify signature (must be present for all messages now)
            if "e" in payload and "s" in payload and "p" in payload:
                try:
                    sender_pubkey = ed25519.Ed25519PublicKey.from_public_bytes(
                        bytes.fromhex(payload["p"])
                    )
                    sender_pubkey.verify(
                        bytes.fromhex(payload["s"]), payload["e"].encode()
                    )
                except Exception as e:
                    print(
                        f"Signature verification failed from {msg.get('peer_id')}: {e}"
                    )
                    return
            else:
                print(
                    f"Warning: Message from {msg.get('peer_id')} is missing encrypted payload or signature. Dropping."
                )
                return

            try:
                decrypted_payload = self._decrypt(payload["e"])

                # Verify identity registry
                origin = decrypted_payload.get("origin")
                if origin:
                    derived_node_id = hashlib.sha256(
                        bytes.fromhex(payload["p"])
                    ).hexdigest()[:16]
                    if origin != derived_node_id:
                        print(
                            f"Identity mismatch from {msg.get('peer_id')}: claimed origin {origin} does not match pubkey {derived_node_id}."
                        )
                        return

                    registry_raw = self.get_kv("identity_registry") or []
                    registry_map = {}
                    for item in registry_raw:
                        try:
                            reg_entry = json.loads(item)
                            if "node_id" in reg_entry and "public_key" in reg_entry:
                                registry_map[reg_entry["node_id"]] = reg_entry[
                                    "public_key"
                                ]
                        except Exception:
                            pass

                    if origin in registry_map and registry_map[origin] != payload["p"]:
                        print(
                            f"Identity spoofing detected from {msg.get('peer_id')}: public key does not match registry."
                        )
                        return

                payload = decrypted_payload
            except Exception as e:
                print(f"Error decrypting message from {msg.get('peer_id')}: {e}")
                return

            # Update peer map
            origin = payload.get("origin")
            peer_id = msg.get("peer_id")
            if origin and peer_id:
                self.peer_map[peer_id] = origin

            await self._handle_payload(payload)

    async def _handle_payload(self, payload: Dict[str, Any]):
        """Handles application-level logic."""
        p_type = payload.get("type")
        origin = payload.get("origin")
        remote_vc = payload.get("vc", {})
        urgency = payload.get("urgency", "low")
        timestamp = payload.get("timestamp", time.time())

        # Telemetry
        latency = time.time() - timestamp
        self.log_aggregator.add_telemetry("latency", latency)
        self.log_aggregator.add_telemetry("message_count", 1)

        # Contextual Attenuation Filtering
        if urgency == "low" and origin in self.peer_distances:
            distance = self.peer_distances[origin]
            if distance > 5.0:
                # Silently drop low-urgency messages from far peers
                return

        # Merge clocks on receive
        if remote_vc:
            self._merge_clock(remote_vc)

        if p_type == "thought":
            content = payload.get("content")
            status = payload.get("status")
            capabilities = payload.get("capabilities")

            thought_data = {
                "content": content,
                "status": status,
                "capabilities": capabilities,
                "timestamp": payload.get("timestamp"),
            }

            self.thoughts[origin] = thought_data
            self._save_thought(origin, json.dumps(thought_data))

        elif p_type == "kv_update":
            key = payload.get("key")
            crdt_data = payload.get("crdt")

            if crdt_data:
                # New CRDT-based update
                remote_crdt = self._deserialize_crdt(crdt_data)

                # Check if we have a local CRDT for this key
                current = self.kv_store.get(key)

                if current and isinstance(current, type(remote_crdt)):
                    # Merge
                    current.merge(remote_crdt)
                    self._save_kv(key, current)
                else:
                    # Overwrite or New
                    # For LWWRegister, we might want to check causality vs current if types mismatch?
                    # For simplicity, if types mismatch, we overwrite with the incoming one
                    # (assuming the network converges to the new type eventually).
                    self.kv_store[key] = remote_crdt
                    self._save_kv(key, remote_crdt)

            else:
                # Legacy update (LWWRegister implicit)
                value = payload.get("value")
                remote_ts = payload.get("timestamp", 0)

                remote_lww = LWWRegister(value, remote_ts, origin, remote_vc)

                current = self.kv_store.get(key)
                if current and isinstance(current, LWWRegister):
                    current.merge(remote_lww)
                    self._save_kv(key, current)
                elif current is None:
                    self.kv_store[key] = remote_lww
                    self._save_kv(key, remote_lww)
                else:
                    # Type mismatch (Current is Counter/Set, incoming is LWW).
                    # We keep current? Or overwrite?
                    # Assuming migration to CRDTs, LWW is the 'default'.
                    # If we have a Counter, we probably don't want to overwrite with a Register.
                    # But for now, let's assume strict typing per key.
                    pass

        elif p_type == "baton_request":
            resource = payload.get("resource")
            # If I hold the lock, deny
            if self.batons.get(resource) == self.node_id:
                await self.broadcast(
                    {
                        "type": "baton_deny",
                        "resource": resource,
                        "target": origin,
                        "reason": "I hold the lock",
                    }
                )

        elif p_type == "baton_claim":
            resource = payload.get("resource")
            self.batons[resource] = origin

        elif p_type == "baton_release":
            resource = payload.get("resource")
            force = payload.get("force", False)
            if self.batons.get(resource) == origin or force:
                if resource in self.batons:
                    del self.batons[resource]

        elif p_type == "baton_deny":
            resource = payload.get("resource")
            target = payload.get("target")

            # Telemetry for contention
            self.log_aggregator.add_telemetry("contentions", 1)

            if target == self.node_id:
                if resource in self._lock_requests:
                    self._lock_requests[resource].set_result(False)

        elif p_type == "log":
            # Add remote log to aggregator
            self.log_aggregator.add_log(payload)

        elif p_type == "peer_update":
            # Update network map
            node = payload.get("node")
            peers = payload.get("peers", [])
            if node:
                self.network_map[node] = peers

        elif p_type == "tandem_sync":
            target = payload.get("target")
            state = payload.get("state")
            if target == self.node_id and self.on_tandem_sync:
                self.on_tandem_sync(origin, state)

        elif p_type == "command_request":
            target = payload.get("target")
            capabilities = payload.get("capabilities", [])
            status_filter = payload.get("status_filter")
            command = payload.get("command")

            # Check if we are the target
            is_target = target == self.node_id
            if not is_target and capabilities:
                # Check if we have the required capabilities or properties
                # Supports both boolean flags ("gpu") and kv tags ("role=gpu" or "parallel_group=ring_1")
                is_target = all(cap in self.capabilities for cap in capabilities)

            # If no target/capabilities, it might be a general broadcast
            if target is None and not capabilities:
                is_target = True

            # If status_filter is provided, we must match it
            if status_filter and getattr(self, "status", "unknown") != status_filter:
                is_target = False

            if is_target and self.on_command_request:
                # Trigger callback (async)
                asyncio.create_task(self.on_command_request(origin, command))

        elif p_type == "ping":
            target = payload.get("target")
            if target == self.node_id:
                message = payload.get("message", "Ping!")
                print(f">>> [Ping] from {origin}: {message}")
                # Log it
                asyncio.create_task(
                    self.log("info", f"Received ping from {origin}: {message}")
                )
                # Respond with a thought
                asyncio.create_task(
                    self.share_thought(f"Responding to ping from {origin}")
                )

        elif p_type == "welcome":
            # Handle welcome message from peer (state sync)
            target = payload.get("target")
            # Only process if directed at us (or no target = broadcast welcome)
            if target is None or target == self.node_id:
                welcome_thoughts = payload.get("thoughts", {})
                welcome_batons = payload.get("batons", {})
                welcome_kv = payload.get("kv", {})
                welcome_vc = payload.get("vector_clock", {})

                # Merge thoughts
                for node_id, thought in welcome_thoughts.items():
                    self._save_thought(node_id, thought)

                # Merge batons
                for resource, owner in welcome_batons.items():
                    self.batons[resource] = owner

                # Merge KV
                for key, crdt_data in welcome_kv.items():
                    try:
                        remote_crdt = self._deserialize_crdt(crdt_data)
                        current = self.kv_store.get(key)
                        if current and isinstance(current, type(remote_crdt)):
                            current.merge(remote_crdt)
                            self._save_kv(key, current)
                        else:
                            self.kv_store[key] = remote_crdt
                            self._save_kv(key, remote_crdt)
                    except Exception as e:
                        print(f"Error merging welcome KV for key {key}: {e}")

                # Merge vector clock
                self._merge_clock(welcome_vc)

                print(
                    f"[Warmup] Received welcome state from {origin}. KV keys: {list(welcome_kv.keys())}"
                )

        elif p_type == "gossip_request":
            # Respond to gossip request with our state
            # The gossip_request comes with the sender's VC and origin
            # Send back our state (peers will merge)
            await self.broadcast(
                {
                    "type": "welcome",
                    "thoughts": self.thoughts,
                    "batons": self.batons,
                    "kv": {
                        k: v.to_dict() if hasattr(v, "to_dict") else str(v)
                        for k, v in self.kv_store.items()
                    },
                    "vector_clock": self.vector_clock,
                    "origin": self.node_id,
                },
                urgency="low",
            )

    async def broadcast_network_state(self):
        """Broadcasts the current list of connected peers (Node IDs)."""
        # Resolve peers to node IDs
        connected_nodes = []
        for pid in self.peers:
            if pid in self.peer_map:
                connected_nodes.append(self.peer_map[pid])

        # Update local map
        self.network_map[self.node_id] = connected_nodes

        # Broadcast
        await self.broadcast(
            {"type": "peer_update", "node": self.node_id, "peers": connected_nodes}
        )

    # --- Public API ---

    async def log(self, level: str, message: str):
        """Broadcasts a log message."""
        entry = {
            "type": "log",
            "level": level,
            "message": message,
            "origin": self.node_id,
            "timestamp": time.time(),
        }
        self.log_aggregator.add_log(entry)
        await self.broadcast(entry)

    async def share_thought(self, content: str, urgency: str = "low"):
        # Enrich thought with status and capabilities
        full_thought = {
            "content": content,
            "status": getattr(self, "status", "unknown"),
            "capabilities": self.capabilities,
        }
        self.thoughts[self.node_id] = full_thought
        self._save_thought(self.node_id, json.dumps(full_thought))
        await self.broadcast(
            {
                "type": "thought",
                "content": content,
                "status": full_thought["status"],
                "capabilities": self.capabilities,
            },
            urgency=urgency,
        )

    async def set_status(self, status: str):
        """Sets the node status (e.g., 'idle', 'busy') and broadcasts a thought."""
        self.status = status
        # Update KV too for easier querying
        await self.update_kv(f"status:{self.node_id}", status, urgency="high")
        await self.share_thought(f"Status update: {status}")

    async def broadcast_command(
        self,
        command: Dict[str, Any],
        target: str = None,
        capabilities: list[str] = None,
        status_filter: str = None,
    ):
        """Broadcasts a command execution request to the swarm."""
        payload = {
            "type": "command_request",
            "command": command,
            "target": target,
            "capabilities": capabilities or [],
            "status_filter": status_filter,
            "origin": self.node_id,
        }
        await self.broadcast(payload, urgency="high")

    async def delegate_inference_task(self, prompt: str, target: str = None):
        """Helper to quickly delegate an inference task to a Pollen-capable node."""
        command = {"type": "run_inference", "prompt": prompt}
        # Require the 'pollen_compute' capability so it only hits capable shards
        await self.broadcast_command(
            command, target=target, capabilities=["pollen_compute"]
        )

    async def ping(self, target_node_id: str, message: str = "Ping!"):
        """Sends a direct ping message to a specific node."""
        await self.broadcast(
            {"type": "ping", "target": target_node_id, "message": message},
            urgency="high",
        )

    async def tandem_sync(self, target_node_id: str, state: Dict[str, Any]):
        """
        Sends a high-urgency physical state update for tandem action.
        Note: In this implementation, it uses broadcast with a target filter.
        """
        payload = {"type": "tandem_sync", "target": target_node_id, "state": state}
        await self.broadcast(payload, urgency="high")

    async def leave_marker(
        self,
        marker_id: str,
        marker_type: str,
        location: str,
        payload: Optional[Dict[str, Any]] = None,
    ):
        """Leaves a stigmergic marker in the environment (KV store)."""
        marker = {
            "id": marker_id,
            "type": marker_type,
            "location": location,
            "payload": payload or {},
            "origin": self.node_id,
            "timestamp": time.time(),
        }
        await self.update_kv(f"marker:{marker_id}", marker, urgency="high")

    def get_markers(
        self, marker_type: Optional[str] = None, location: Optional[str] = None
    ) -> list[Dict[str, Any]]:
        """Retrieves and filters markers from the KV store."""
        markers = []
        for key, crdt in self.kv_store.items():
            if key.startswith("marker:"):
                val = crdt.value()
                if not isinstance(val, dict):
                    continue

                if marker_type and val.get("type") != marker_type:
                    continue
                if location and val.get("location") != location:
                    continue
                markers.append(val)
        return markers

    async def advertise_capabilities(self, capabilities: list[str]):
        """Advertises node capabilities to the swarm via KV store."""
        self.capabilities = capabilities
        await self.update_kv(
            f"capabilities:{self.node_id}", capabilities, urgency="high"
        )

    async def autonomously_pick_task(self) -> Optional[Dict[str, Any]]:
        """
        Reads the 'swarm_backlog' from KV store and picks the highest priority
        pending task matching this node's capabilities.
        """
        backlog_raw = self.get_kv("swarm_backlog")
        if not backlog_raw:
            return None

        # backlog_raw is a set of JSON strings (from ORSet)
        tasks = []
        for item in backlog_raw:
            try:
                tasks.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                continue

        # Filter: todo AND capabilities match AND dependencies met
        available = []

        # Build a fast lookup for task statuses
        task_statuses = {t.get("id"): t.get("status") for t in tasks if t.get("id")}

        for task in tasks:
            if task.get("status") == "todo":
                # Check dependencies first
                blocked_by = task.get("blocked_by", [])
                dependencies_met = True
                for dep_id in blocked_by:
                    if task_statuses.get(dep_id) != "done":
                        dependencies_met = False
                        break

                if not dependencies_met:
                    continue

                required = task.get("required", [])
                # If node has all required capabilities (or none required)
                if not required or all(cap in self.capabilities for cap in required):
                    available.append(task)

        if not available:
            return None

        # Sort by priority: high > medium > low
        priority_map = {"high": 3, "medium": 2, "low": 1}
        available.sort(
            key=lambda x: priority_map.get(x.get("priority", "medium"), 0), reverse=True
        )
        picked = available[0]

        # Claim the task
        task_id = picked.get("id")
        if not task_id:
            return None

        # Try to acquire baton first to avoid races
        success = await self.acquire_baton(f"task:{task_id}")
        if not success:
            return None

        # Update the task status in the ORSet
        # Note: ORSet.remove/add is not atomic at the KV level, but the baton helps.
        # We need to find the OLD task string to remove it.
        old_task_str = None
        for item in backlog_raw:
            try:
                if json.loads(item).get("id") == task_id:
                    old_task_str = item
                    break
            except BaseException:
                continue

        if old_task_str:
            await self.update_set("swarm_backlog", old_task_str, remove=True)

        picked["status"] = "in_progress"
        picked["owner"] = self.node_id
        await self.update_set("swarm_backlog", json.dumps(picked), urgency="high")

        return picked

    async def update_kv(self, key: str, value: Any, urgency: str = "low"):
        """Updates a Key-Value pair using LWWRegister."""
        self._increment_clock()
        timestamp = time.time()

        current = self.kv_store.get(key)

        if current and not isinstance(current, LWWRegister):
            # If it was another CRDT type, we are overwriting it with a Register
            pass

        new_register = LWWRegister(
            value, timestamp, self.node_id, self.vector_clock.copy()
        )

        # If we had a previous LWWRegister, we might want to merge just to be safe (idempotency),
        # but here we are originating a new value, so we just set it.
        # Actually, if we want to preserve history? LWWRegister doesn't preserve history, just latest.

        self.kv_store[key] = new_register
        self._save_kv(key, new_register)

        # Broadcast
        await self.broadcast(
            {"type": "kv_update", "key": key, "crdt": new_register.to_dict()},
            urgency=urgency,
        )

    async def update_counter(self, key: str, delta: int = 1, urgency: str = "low"):
        """Updates a PNCounter."""
        self._increment_clock()  # Counters don't strictly use VC for merge, but good to track causality in mesh

        current = self.kv_store.get(key)
        if current is None:
            current = PNCounter()
            self.kv_store[key] = current
        elif not isinstance(current, PNCounter):
            raise ValueError(f"Key {key} is not a PNCounter")

        if delta > 0:
            current.inc(self.node_id, delta)
        elif delta < 0:
            current.dec(self.node_id, -delta)

        self._save_kv(key, current)

        await self.broadcast(
            {"type": "kv_update", "key": key, "crdt": current.to_dict()},
            urgency=urgency,
        )

    async def update_set(
        self, key: str, element: Any, remove: bool = False, urgency: str = "low"
    ):
        """Updates an ORSet (or GSet if remove=False and fallback)."""
        self._increment_clock()

        current = self.kv_store.get(key)
        if current is None:
            current = ORSet()
            self.kv_store[key] = current
        elif not isinstance(current, ORSet):
            raise ValueError(f"Key {key} is not an ORSet")

        if remove:
            current.remove(element)
        else:
            current.add(element)

        self._save_kv(key, current)

        await self.broadcast(
            {"type": "kv_update", "key": key, "crdt": current.to_dict()},
            urgency=urgency,
        )

    def get_kv(self, key: str):
        val = self.kv_store.get(key)
        if val is None:
            return None
        return val.value()

    def get_all_kv(self) -> Dict[str, Any]:
        """Returns a copy of the KV store with values unwrapped."""
        return {k: v.value() for k, v in self.kv_store.items()}

    async def save_revision(self, key: str, diff: str) -> None:
        """Appends a code or state revision diff to the log and broadcasts it."""
        import uuid
        import time
        from .crdt import RevisionLog

        current = self.kv_store.get(key)
        if current and not isinstance(current, RevisionLog):
            # Overwrite if it was a different CRDT
            current = RevisionLog()
        elif not current:
            current = RevisionLog()

        revision_id = str(uuid.uuid4())
        current.append(time.time(), self.node_id, revision_id, diff)

        self.kv_store[key] = current
        self._save_kv(key, current)

        # Broadcast the update
        self._increment_clock()
        payload = {
            "type": "kv_update",
            "key": key,
            "crdt": current.to_dict(),
        }
        await self.broadcast(payload)

    def get_revisions(self, key: str) -> list:
        """Retrieves the chronological list of revisions for a key."""
        from .crdt import RevisionLog

        crdt = self.kv_store.get(key)
        if crdt and isinstance(crdt, RevisionLog):
            return crdt.value()
        return []

    def get_health_status(self) -> Dict[str, Any]:
        """Returns the current operational health of the node."""
        if not self.running:
            return {"status": "offline", "reason": "Mesh daemon not running"}

        if self._sidecar_dead:
            return {
                "status": "degraded",
                "reason": "Sidecar connection lost",
                "mode": "sidecar_dead",
            }

        if not self.peers:
            return {
                "status": "degraded",
                "reason": "No global peers discovered",
                "mode": "local_fallback",
            }

        return {
            "status": "healthy",
            "peer_count": len(self.peers),
            "mode": "global_mesh",
        }

    async def warm_up(self, timeout: int = 30) -> Dict[str, Any]:
        """
        Warms up the node by ensuring mesh is running and waiting for peer discovery.
        Returns the warm-up status including discovered peers.
        """
        # Start mesh if not running
        if not self.running:
            await self.start()

        # Wait for peers
        start_time = time.time()
        initial_peers = len(self.peers)

        while time.time() - start_time < timeout:
            if len(self.peers) > initial_peers:
                break
            await asyncio.sleep(1)

        return {
            "node_id": self.node_id,
            "peers": list(self.peers),
            "peer_count": len(self.peers),
            "health": self.get_health_status(),
            "warm_up_duration": int(time.time() - start_time),
        }

    async def acquire_baton(self, resource: str, timeout: float = 2.0) -> bool:
        """Tries to acquire a lock on a resource."""
        if resource in self.batons:
            if self.batons[resource] == self.node_id:
                return True
            return False

        future = asyncio.get_running_loop().create_future()
        self._lock_requests[resource] = future

        await self.broadcast({"type": "baton_request", "resource": resource})

        try:
            await asyncio.wait_for(future, timeout=timeout)
            result = future.result()
            del self._lock_requests[resource]
            if result:
                asyncio.create_task(
                    self.log_audit_event("baton_acquired", {"resource": resource})
                )
            return result
        except asyncio.TimeoutError:
            del self._lock_requests[resource]
            self.batons[resource] = self.node_id
            await self.broadcast({"type": "baton_claim", "resource": resource})
            asyncio.create_task(
                self.log_audit_event("baton_acquired", {"resource": resource})
            )
            return True

    async def release_baton(self, resource: str):
        if self.batons.get(resource) == self.node_id:
            del self.batons[resource]
            await self.broadcast({"type": "baton_release", "resource": resource})
            asyncio.create_task(
                self.log_audit_event("baton_released", {"resource": resource})
            )
            # Trigger Pulse
            if self.on_baton_release:
                # Pass resource and my identity
                await self.on_baton_release(resource, self.node_id)
