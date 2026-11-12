try:
    from src.core.collaboration.experimental_boundary import (
        MANAGED_TASK_REFUSAL,
        ManagedTaskRefusal,
        managed_backlog_ids,
        managed_task_resource,
    )
except ImportError:
    from core.collaboration.experimental_boundary import (
        MANAGED_TASK_REFUSAL,
        ManagedTaskRefusal,
        managed_backlog_ids,
        managed_task_resource,
    )

import asyncio
import json
import hashlib
import secrets
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
    from .crdt import CRDT, Register, PNCounter, GSet, ORSet, RevisionLog
    from .storage import BaseStorageProvider, SQLiteStorageProvider
    from .conversation import Conversation, ConversationError, ConversationUnsupported
except ImportError:
    from crdt import CRDT, Register, PNCounter, GSet, ORSet, RevisionLog
    from storage import BaseStorageProvider, SQLiteStorageProvider
    from conversation import Conversation, ConversationError, ConversationUnsupported


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
        mqtt_host: Optional[str] = None,
        mqtt_port: int = 1883,
        ble_enabled: bool = False,
        conversation_only: bool = False,
        conversation_identity: Optional[ed25519.Ed25519PrivateKey] = None,
    ):
        if type(conversation_only) is not bool:
            raise ValueError("conversation_only must be a boolean")
        if conversation_only and not isinstance(
            conversation_identity, ed25519.Ed25519PrivateKey
        ):
            raise ValueError("conversation-only transport requires its retained key")
        self.conversation_only = conversation_only
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
        self.private_key = (
            conversation_identity
            if conversation_only
            else self._load_or_create_identity()
        )
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
        if not conversation_only:
            self._load_state()
        self.conversation_error = (
            "durable conversation requires the qualified SQLite storage backend"
        )
        try:
            store = self.storage.conversation_store(self.private_key, self.topic)
        except ConversationUnsupported as error:
            store = None
            self.conversation_error = str(error)
        self.conversation = (
            Conversation(store, self.broadcast) if store is not None else None
        )

        self.process: Optional[asyncio.subprocess.Process] = None
        self.running = False
        self._sidecar_dead = False
        self._sidecar_restart_count = 0
        self._sidecar_max_restarts = 100
        self._sidecar_restart_delay = 5  # seconds
        self._monitor_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._gossip_task: Optional[asyncio.Task] = None

        # Pending lock requests
        self._lock_requests: Dict[str, asyncio.Future] = {}

        # Transport settings
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.ble_enabled = ble_enabled
        self._mqtt_task: Optional[asyncio.Task] = None
        self._ble_task: Optional[asyncio.Task] = None

        self._mqtt_client = None

        # BLE Server specific attributes
        self._ble_server = None
        self._ble_characteristic_uuid = (
            f"0000{self.topic[:4]}-0000-1000-8000-00805f9b34fb"
        )
        self._ble_service_uuid = f"{self.topic[:8]}-{self.topic[8:12]}-{self.topic[12:16]}-{self.topic[16:20]}-{self.topic[20:32]}"

        # Callbacks for Pulse
        self.on_baton_release: Optional[Callable[[str, str], Awaitable[None]]] = None
        self.on_tandem_sync: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self.on_command_request: Optional[
            Callable[[str, Dict[str, Any]], Awaitable[None]]
        ] = None

        # Observability
        self.log_aggregator = LogAggregator()

    def _signed_envelope(self, payload: Dict[str, Any]) -> Dict[str, str]:
        """Sign one encrypted envelope for all transports on this broadcast."""
        payload.setdefault("message_id", secrets.token_hex(16))
        encrypted_data = self._encrypt(payload)
        signature = self.private_key.sign(encrypted_data.encode())
        return {"e": encrypted_data, "s": signature.hex(), "p": self.public_key_hex}

    def _command_is_duplicate(self, origin: str, message_id: Any) -> bool:
        """Persist a bounded transport receipt before dispatching a command.

        This deduplicates repeated envelopes; it is not a coordinator execution
        receipt. Durable tasks still use the authenticated service.
        """
        if message_id is None:
            return False  # Legacy sender without a transport-level message ID.
        if (
            not isinstance(message_id, str)
            or len(message_id) != 32
            or any(c not in "0123456789abcdef" for c in message_id)
        ):
            return True  # Do not execute commands with malformed message IDs.

        now = time.time()
        stored = self.storage.get_metadata("command_message_receipts_v1")
        if not isinstance(stored, dict):
            stored = {}
        cutoff = now - 86400
        recent = {
            key: stamp
            for key, stamp in stored.items()
            if isinstance(stamp, (int, float)) and stamp >= cutoff
        }
        key = f"{origin}:{message_id}"
        if key in recent:
            return True

        recent[key] = now
        if len(recent) > 4096:
            recent = dict(sorted(recent.items(), key=lambda item: item[1])[-4096:])
        self.storage.save_metadata("command_message_receipts_v1", recent)
        return False

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
                    if self.conversation:
                        await self.conversation.announce()
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
            "batons": self.advisory_batons(),
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
            if crdt_type in ["register", "lww-register"]:
                return Register.from_dict(data)
            elif crdt_type == "pn-counter":
                return PNCounter.from_dict(data)
            elif crdt_type == "g-set":
                return GSet.from_dict(data)
            elif crdt_type == "or-set":
                return ORSet.from_dict(data)
            elif crdt_type == "revision-log":
                return RevisionLog.from_dict(data)
            elif "vc" in data and "value" in data:
                # Legacy wrapped format -> Register
                return Register(
                    value=data["value"],
                    timestamp=data.get("timestamp", 0),
                    origin=data.get("origin", "unknown"),
                    vc=data["vc"],
                )

        # Raw value -> Register with empty VC (should ideally not happen in new system)
        return Register(value=data, timestamp=0, origin="unknown", vc={})

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
        if key == "swarm_backlog":
            self._discard_managed_batons()

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
        if (
            self.conversation
            and hashlib.sha256(new_key.encode()).hexdigest() != self.topic
        ):
            self.conversation_error = "conversation scope changed; use a separate node database or restart with the original scope"
            self.conversation.disable(self.conversation_error)
            self.conversation = None

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
        if self.conversation_only:
            raise ConversationUnsupported(
                "conversation-only transport requires its dedicated process owner"
            )
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

        # Start MQTT Listener if configured
        if self.mqtt_host:
            self._mqtt_task = asyncio.create_task(self._mqtt_listener())

        # Start BLE Listener if configured
        if self.ble_enabled:
            self._ble_task = asyncio.create_task(self._ble_listener())

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

    async def _mqtt_listener(self):
        """Background task that listens to MQTT broker for macro messages."""
        if not self.mqtt_host:
            return

        while self.running:
            try:
                import aiomqtt

                async with aiomqtt.Client(
                    hostname=self.mqtt_host, port=self.mqtt_port
                ) as client:
                    self._mqtt_client = client
                    topic_sub = f"keystone/{self.topic}/macro"
                    await client.subscribe(topic_sub)
                    print(f"Subscribed to MQTT topic: {topic_sub}")

                    async for message in client.messages:
                        if not self.running:
                            break
                        try:
                            msg_str = message.payload.decode()
                            msg_dict = json.loads(msg_str)
                            # Wrap it to simulate sidecar format for handle_message
                            if "e" in msg_dict and "s" in msg_dict and "p" in msg_dict:
                                # A bit hacky, but derive peer_id from p to conform to handle_message
                                derived_peer_id = hashlib.sha256(
                                    bytes.fromhex(msg_dict["p"])
                                ).hexdigest()[:16]
                                wrapped_msg = {
                                    "type": "message",
                                    "peer_id": derived_peer_id,
                                    "payload": msg_dict,
                                }
                                await self._handle_message(wrapped_msg)
                        except Exception as e:
                            print(f"Error parsing incoming MQTT message: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"MQTT listener error: {e}. Retrying in 5 seconds...")
                self._mqtt_client = None
                await asyncio.sleep(5)

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

        if self._ble_server:
            try:
                await self._ble_server.stop()
            except Exception:
                pass

        # Cancel all background tasks
        tasks_to_cancel = [
            self._snapshot_task,
            self._monitor_task,
            self._idle_task,
            self._gossip_task,
            self._mqtt_task,
            self._ble_task,
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

        # Attach origin info and one ID reused across all transports.
        payload["message_id"] = secrets.token_hex(16)
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
            return await self.broadcast_macro(payload)
        if max_dist < 1.0 and self.peer_distances:
            return await self.broadcast_micro(payload)

        # Default fallback to Hyperswarm (DHT). This reports submission,
        # not acknowledgement by any remote agent.
        await self._send_to_sidecar("broadcast", self._signed_envelope(payload))
        return {"message_id": payload["message_id"], "dht": "submitted"}

    async def broadcast_macro(self, payload: Any):
        """Submit one envelope through MQTT and DHT and report each route."""
        msg = self._signed_envelope(payload)
        result = {
            "message_id": payload["message_id"],
            "mqtt": "disabled",
            "dht": "pending",
        }
        if self.mqtt_host:
            result["mqtt"] = "failed"
            try:
                encoded = json.dumps(msg).encode()
                topic = f"keystone/{self.topic}/macro"
                if self._mqtt_client:
                    await self._mqtt_client.publish(topic, encoded)
                else:
                    import aiomqtt

                    async with aiomqtt.Client(
                        hostname=self.mqtt_host, port=self.mqtt_port
                    ) as client:
                        await client.publish(topic, encoded)
                result["mqtt"] = "submitted"
            except Exception as exc:
                print(f"Warning: MQTT delivery failed: {exc}")

        try:
            await self._send_to_sidecar("broadcast", msg)
            result["dht"] = "submitted"
        except Exception:
            result["dht"] = "failed"
            if result["mqtt"] != "submitted":
                raise
            print("Warning: DHT unavailable; MQTT delivery was attempted")
        return result

    async def _ble_listener(self):
        """Background task that starts a BLE GATT Server via bless and periodically scans via bleak."""
        if not self.ble_enabled:
            return

        try:
            import bleak
            import bless
            from bless import (
                BlessServer,
                BlessGATTCharacteristic,
                GATTCharacteristicProperties,
                GATTAttributePermissions,
            )
        except ImportError:
            print(
                "Error: 'bleak' or 'bless' module not installed for BLE. BLE Listener disabled."
            )
            return

        # Start Bless GATT Server
        server_name = "K-Polyphony"

        def read_request(characteristic):
            # When another node reads this, give them our last state or an empty payload.
            # In a true push system, they write to us, not read from us.
            return b"{}"

        def write_request(characteristic, value):
            try:
                msg_str = value.decode()
                msg_dict = json.loads(msg_str)
                if "e" in msg_dict and "s" in msg_dict and "p" in msg_dict:
                    derived_peer_id = hashlib.sha256(
                        bytes.fromhex(msg_dict["p"])
                    ).hexdigest()[:16]
                    wrapped_msg = {
                        "type": "message",
                        "peer_id": derived_peer_id,
                        "payload": msg_dict,
                    }
                    asyncio.create_task(self._handle_message(wrapped_msg))
            except Exception as e:
                print(f"Error parsing incoming BLE message: {e}")

        try:
            loop = asyncio.get_running_loop()
            self._ble_server = BlessServer(name=server_name, loop=loop)
            self._ble_server.read_request_func = read_request
            self._ble_server.write_request_func = write_request

            await self._ble_server.add_new_service(self._ble_service_uuid)

            char_flags = (
                GATTCharacteristicProperties.read
                | GATTCharacteristicProperties.write
                | GATTCharacteristicProperties.indicate
            )
            permissions = (
                GATTAttributePermissions.readable | GATTAttributePermissions.writeable
            )

            await self._ble_server.add_new_characteristic(
                self._ble_service_uuid,
                self._ble_characteristic_uuid,
                char_flags,
                None,
                permissions,
            )

            await self._ble_server.start()
            print(
                f"BLE GATT Server started. Advertising Service: {self._ble_service_uuid}"
            )
        except Exception as e:
            print(f"Failed to start BLE GATT Server: {e}")
            self._ble_server = None

        # Periodically Scan for other Swarm peers (Bleak Client side of listener)
        while self.running:
            try:
                # We do not connect here; broadcast_micro handles connection and writing.
                # The scan just keeps discovery active.
                def callback(device, advertisement_data):
                    if self._ble_service_uuid.lower() in [
                        str(u).lower() for u in advertisement_data.service_uuids
                    ]:
                        # We found someone in our swarm!
                        pass

                scanner = bleak.BleakScanner(detection_callback=callback)
                await scanner.start()
                await asyncio.sleep(30.0)
                await scanner.stop()
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"BLE scanner error: {e}")
                await asyncio.sleep(30)

    async def broadcast_micro(self, payload: Any):
        """Submit one envelope through discovered BLE peers and DHT."""
        msg = self._signed_envelope(payload)
        result = {
            "message_id": payload["message_id"],
            "ble": "disabled" if not self.ble_enabled else "no_peers",
            "ble_attempted": 0,
            "ble_submitted": 0,
            "dht": "pending",
        }
        if self.ble_enabled:
            try:
                import bleak

                encoded = json.dumps(msg).encode()
                devices = await bleak.BleakScanner.discover(
                    timeout=3.0, return_adv=True
                )
                for device, advertisement in devices.values():
                    advertised_uuids = [
                        str(uuid).lower() for uuid in advertisement.service_uuids
                    ]
                    if self._ble_service_uuid.lower() not in advertised_uuids:
                        continue
                    result["ble_attempted"] += 1
                    try:
                        async with bleak.BleakClient(device) as client:
                            await client.write_gatt_char(
                                self._ble_characteristic_uuid, encoded, response=True
                            )
                        result["ble_submitted"] += 1
                    except Exception as exc:
                        print(
                            f"Warning: BLE delivery to {device.address} failed: {exc}"
                        )
                if result["ble_attempted"]:
                    if result["ble_submitted"] == result["ble_attempted"]:
                        result["ble"] = "submitted"
                    elif result["ble_submitted"]:
                        result["ble"] = "partial"
                    else:
                        result["ble"] = "failed"
            except Exception as exc:
                result["ble"] = "failed"
                print(f"Warning: BLE broadcast failed: {exc}")

        try:
            await self._send_to_sidecar("broadcast", msg)
            result["dht"] = "submitted"
        except Exception:
            result["dht"] = "failed"
            if not result["ble_submitted"]:
                raise
            print("Warning: DHT unavailable; BLE delivery was attempted")
        return result

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
            if k.startswith("chat:"):
                continue  # Legacy unsigned chat is retained locally, never replayed as new history.
            try:
                kv_serialized[k] = v.to_dict()
            except BaseException:
                kv_serialized[k] = str(v)

        payload = {
            "type": "welcome",
            "target": peer_id,  # Only intended peer should process this
            "thoughts": self.thoughts,
            "batons": self.advisory_batons(),
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
            if self.conversation_only:
                await self.require_conversation().announce()
                return
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
            if self.conversation:
                await self.conversation.announce()
            await self.broadcast_network_state()

        elif msg_type == "peer_disconnected":
            pid = msg.get("peer_id")
            if pid in self.peers:
                self.peers.remove(pid)
            if self.conversation_only:
                self.peer_map.pop(pid, None)
                return
            await self.broadcast_network_state()

        elif msg_type == "message":
            # Track activity from any peer message
            if not self.conversation_only:
                self.touch_activity()

            payload = msg.get("payload", {})
            if self.conversation_only and not isinstance(payload, dict):
                return

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

                if self.conversation_only:
                    # Authenticate before dispatch; do not touch legacy clocks,
                    # registry, KV, welcome state or execution callbacks.
                    derived = hashlib.sha256(bytes.fromhex(payload["p"])).hexdigest()[
                        :16
                    ]
                    if (
                        not isinstance(decrypted_payload, dict)
                        or decrypted_payload.get("origin") != derived
                        or not isinstance(decrypted_payload.get("type"), str)
                        or decrypted_payload.get("type")
                        not in {"chat_delta", "chat_hello", "chat_request", "chat_page"}
                    ):
                        return
                    await self._handle_payload(decrypted_payload)
                    peer_id = msg.get("peer_id")
                    if isinstance(peer_id, str):
                        self.peer_map[peer_id] = derived
                    return

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
        if self.conversation_only:
            if (
                isinstance(payload, dict)
                and isinstance(payload.get("type"), str)
                and payload.get("type")
                in {"chat_delta", "chat_hello", "chat_request", "chat_page"}
            ):
                try:
                    await self.require_conversation().handle(payload)
                except ConversationError:
                    pass  # Invalid chat cannot fall through into another protocol.
            return
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

        if isinstance(p_type, str) and p_type.startswith("chat_"):
            if self.conversation:
                try:
                    await self.conversation.handle(payload)
                except ConversationError:
                    # Invalid public chat cannot invoke another protocol or expose its contents in logs.
                    self.log_aggregator.add_telemetry(
                        "invalid_conversation_messages", 1
                    )
            return

        if isinstance(p_type, str) and p_type in {
            "baton_request",
            "baton_claim",
            "baton_release",
            "baton_deny",
        }:
            resource = payload.get("resource")
            if self.is_managed_task_resource(resource):
                self._discard_managed_baton(resource)
                return

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
            if isinstance(key, str) and key.startswith("chat:"):
                return  # New chat never accepts unverified legacy history from peers.
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
                    # For Register, we might want to check causality vs current if types mismatch?
                    # For simplicity, if types mismatch, we overwrite with the incoming one
                    # (assuming the network converges to the new type eventually).
                    self.kv_store[key] = remote_crdt
                    self._save_kv(key, remote_crdt)

            else:
                # Legacy update (Register implicit)
                value = payload.get("value")
                remote_ts = payload.get("timestamp", 0)

                remote_lww = Register(value, remote_ts, origin, remote_vc)

                current = self.kv_store.get(key)
                if current and isinstance(current, Register):
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
                future = self._lock_requests.get(resource)
                if future is not None and not future.done():
                    future.set_result(False)

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

            # All selectors are conjunctive. A targeted command must never
            # be executed by a different node that shares its capabilities.
            is_target = (target is None or target == self.node_id) and all(
                cap in self.capabilities for cap in capabilities
            )

            # If status_filter is provided, we must match it
            if status_filter and getattr(self, "status", "unknown") != status_filter:
                is_target = False

            if is_target and self.on_command_request:
                if self._command_is_duplicate(origin, payload.get("message_id")):
                    return
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

                # Classify batons using metadata from this same welcome first.
                for key, crdt_data in welcome_kv.items():
                    if key.startswith("chat:"):
                        continue
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

                managed_ids = managed_backlog_ids(self.get_kv("swarm_backlog"))
                for resource, owner in welcome_batons.items():
                    if not managed_task_resource(resource, managed_ids):
                        self.batons[resource] = owner
                self._discard_managed_batons()

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
                    "batons": self.advisory_batons(),
                    "kv": {
                        k: v.to_dict() if hasattr(v, "to_dict") else str(v)
                        for k, v in self.kv_store.items()
                        if not k.startswith("chat:")
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
                task = json.loads(item)
                if isinstance(task, dict) and isinstance(task.get("id"), str):
                    tasks.append(task)
            except (json.JSONDecodeError, TypeError):
                continue
        managed_ids = managed_backlog_ids(backlog_raw)

        # Filter: todo AND capabilities match AND dependencies met
        available = []

        # Build a fast lookup for task statuses
        task_statuses = {t.get("id"): t.get("status") for t in tasks if t.get("id")}

        for task in tasks:
            if task.get("status") == "todo" and not managed_task_resource(
                f"task:{task.get('id', '')}", managed_ids
            ):
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
        try:
            success = await self.acquire_baton(f"task:{task_id}")
            self.require_advisory_resource(f"task:{task_id}")
        except ManagedTaskRefusal:
            return None
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

        try:
            self.require_advisory_resource(f"task:{task_id}")
        except ManagedTaskRefusal:
            return None

        picked["status"] = "in_progress"
        picked["owner"] = self.node_id
        await self.update_set("swarm_backlog", json.dumps(picked), urgency="high")

        try:
            self.require_advisory_resource(f"task:{task_id}")
        except ManagedTaskRefusal:
            return None

        return picked

    async def update_kv(self, key: str, value: Any, urgency: str = "low"):
        """Updates a Key-Value pair using Register."""
        if key.startswith("chat:"):
            raise ConversationError(
                "legacy chat keys are read-only; use the conversation interface"
            )
        self._increment_clock()
        timestamp = time.time()

        current = self.kv_store.get(key)

        if current and not isinstance(current, Register):
            # If it was another CRDT type, we are overwriting it with a Register
            pass

        new_register = Register(
            value, timestamp, self.node_id, self.vector_clock.copy()
        )

        # If we had a previous Register, we might want to merge just to be safe (idempotency),
        # but here we are originating a new value, so we just set it.
        # Actually, if we want to preserve history? Register doesn't preserve history, just latest.

        self.kv_store[key] = new_register
        self._save_kv(key, new_register)

        # Broadcast
        await self.broadcast(
            {"type": "kv_update", "key": key, "crdt": new_register.to_dict()},
            urgency=urgency,
        )

    async def update_counter(self, key: str, delta: int = 1, urgency: str = "low"):
        """Updates a PNCounter."""
        if key.startswith("chat:"):
            raise ConversationError(
                "legacy chat keys are read-only; use the conversation interface"
            )
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
        if key.startswith("chat:"):
            raise ConversationError(
                "legacy chat sets are read-only; use the conversation interface"
            )
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

    def require_conversation(self):
        if self.conversation is None:
            raise ConversationUnsupported(self.conversation_error)
        return self.conversation

    def legacy_chat(self, topic, limit=100):
        """Bounded local inspection of retained, unauthenticated ORSet data."""
        from itertools import islice

        try:
            from .conversation import MAX_PAGE_BYTES, limit as page_limit, topic_name
        except ImportError:
            from conversation import MAX_PAGE_BYTES, limit as page_limit, topic_name
        topic_name(topic)
        page_limit(limit)
        current = self.kv_store.get("chat:" + topic)
        if not isinstance(current, ORSet):
            return {"legacy_unverified": True, "messages": [], "truncated": False}
        messages, seen, size, scanned = [], set(), 0, 0
        for raw, tag in islice(current.added, 2048):
            scanned += 1
            if (raw, tag) in current.removed or raw in seen or not isinstance(raw, str):
                continue
            seen.add(raw)
            if len(raw.encode()) > MAX_PAGE_BYTES or len(messages) >= limit:
                break
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                continue
            size += len(raw.encode())
            if size > MAX_PAGE_BYTES:
                break
            messages.append({"legacy_unverified": True, "original": value})
        return {
            "legacy_unverified": True,
            "messages": messages,
            "truncated": scanned < len(current.added) or len(messages) >= limit,
            "pagination_supported": False,
            "note": "Local legacy display only; original authorship is unverified. Full data remains in the node database.",
        }

    def get_all_kv(self) -> Dict[str, Any]:
        """Returns a copy of the KV store with values unwrapped."""
        return {k: v.value() for k, v in self.kv_store.items()}

    async def save_revision(self, key: str, diff: str) -> None:
        """Appends a code or state revision diff to the log and broadcasts it."""
        if key.startswith("chat:"):
            raise ConversationError(
                "legacy chat keys are read-only; use the conversation interface"
            )
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

    def is_managed_task_resource(self, resource):
        if not isinstance(resource, str) or not resource.startswith("task:"):
            return False
        if managed_task_resource(resource):
            return True
        return managed_task_resource(
            resource, managed_backlog_ids(self.get_kv("swarm_backlog"))
        )

    def _discard_managed_baton(self, resource, future=None):
        """Invalidate only ephemeral advisory ownership and its current waiter."""
        self.batons.pop(resource, None)
        if future is None:
            future = self._lock_requests.get(resource)
        if future is not None:
            if not future.done():
                future.set_result(False)
            if self._lock_requests.get(resource) is future:
                del self._lock_requests[resource]

    def _discard_managed_batons(self):
        managed_ids = managed_backlog_ids(self.get_kv("swarm_backlog"))
        for resource in set(self.batons) | set(self._lock_requests):
            if managed_task_resource(resource, managed_ids):
                self._discard_managed_baton(resource)

    def advisory_batons(self):
        """Expose advisory lock state without suggesting managed ownership."""
        self._discard_managed_batons()
        return dict(self.batons)

    def require_advisory_resource(self, resource, *, pending_request=None):
        if self.is_managed_task_resource(resource):
            self._discard_managed_baton(resource, pending_request)
            raise ManagedTaskRefusal(MANAGED_TASK_REFUSAL)

    async def acquire_baton(self, resource: str, timeout: float = 2.0) -> bool:
        """Tries to acquire a lock on a resource."""
        self.require_advisory_resource(resource)
        if resource in self.batons:
            if self.batons[resource] == self.node_id:
                return True
            return False

        future = asyncio.get_running_loop().create_future()
        self._lock_requests[resource] = future

        try:
            await self.broadcast({"type": "baton_request", "resource": resource})
            self.require_advisory_resource(resource, pending_request=future)
            try:
                result = await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                self.require_advisory_resource(resource, pending_request=future)
                if self._lock_requests.get(resource) is not future:
                    return False
                self.batons[resource] = self.node_id
                await self.broadcast({"type": "baton_claim", "resource": resource})
                self.require_advisory_resource(resource, pending_request=future)
                asyncio.create_task(
                    self.log_audit_event("baton_acquired", {"resource": resource})
                )
                return True
            self.require_advisory_resource(resource, pending_request=future)
            if result:
                asyncio.create_task(
                    self.log_audit_event("baton_acquired", {"resource": resource})
                )
            return result
        finally:
            if self._lock_requests.get(resource) is future:
                del self._lock_requests[resource]
            if not future.done():
                future.cancel()

    async def release_baton(self, resource: str):
        self.require_advisory_resource(resource)
        if self.batons.get(resource) == self.node_id:
            del self.batons[resource]
            await self.broadcast({"type": "baton_release", "resource": resource})
            self.require_advisory_resource(resource)
            asyncio.create_task(
                self.log_audit_event("baton_released", {"resource": resource})
            )
            # Trigger Pulse
            if self.on_baton_release:
                # Pass resource and my identity
                await self.on_baton_release(resource, self.node_id)
