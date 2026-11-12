"""Fixed-topic, public-only MCP participant with a retained local identity.

No ordinary MCP server, execution callback, model client or authority is built.
The supported host owns this private directory and its installed dependencies.
"""

from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from .conversation import (
    ConversationError,
    MAX_CONTENT_BYTES,
    MAX_CURSOR,
    canonical,
    session_id,
    topic_name,
)
from .mesh import LiminalMesh
from .storage import SQLiteStorageProvider
from .conversation_stdio import (
    DIAGNOSTIC_BYTES,
    Diagnostics,
    StdioError,
    bounded_stdio,
    strict_json,
)

PROFILE = "polyphony-public-conversation"
SIDECAR_PROFILE = "polyphony-conversation-only/v1"
MAX_BYTES = 65536
SCOPE_BYTES = 4096
START_SECONDS = 5
TERM_SECONDS = 1.5
KILL_SECONDS = 1
FIXED_PATH = "/usr/local/bin:/usr/bin:/bin"
TOOLS = (
    "conversation_status",
    "conversation_start",
    "conversation_join",
    "conversation_say",
    "conversation_history",
    "conversation_watch",
)
SIDECAR = Path(__file__).with_name("sidecar")
CHAT_TYPES = {"chat_delta", "chat_hello", "chat_request", "chat_page"}
STATE_TABLES = {
    "conversation_binding",
    "conversation_stream",
    "conversation_records",
    "conversation_peers",
    "conversation_retired_streams",
}


class ProfileError(RuntimeError):
    """A bounded diagnostic that never includes caller text or credentials."""


def require(predicate, reason):
    if not predicate:
        raise ProfileError(reason)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_field")
        result[key] = value
    return result


def private_file(path, *, maximum=None, readonly=False):
    """Check the opened inode before reading; a FIFO cannot block this open."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        require(
            stat.S_ISREG(info.st_mode)
            and stat.S_IMODE(info.st_mode) in ({0o400, 0o600} if readonly else {0o600})
            and info.st_uid == os.getuid()
            and info.st_nlink == 1,
            "state_file_must_be_private_owned_regular",
        )
        if maximum is None:
            return None
        require(info.st_size <= maximum, "state_file_exceeds_bound")
        with os.fdopen(os.dup(fd), "rb") as stream:
            value = stream.read(maximum + 1)
        require(len(value) <= maximum, "state_file_exceeds_bound")
        after = os.fstat(fd)
        require(
            (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            and len(value) == info.st_size,
            "private_file_changed_during_read",
        )
        return value
    finally:
        os.close(fd)


def create_private(path, content):
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | os.O_CLOEXEC,
        0o600,
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def absolute_path(value):
    require(isinstance(value, str) and bool(value), "canonical_absolute_path_required")
    path = Path(value)
    require(
        path.is_absolute()
        and str(path) == value
        and ".." not in path.parts
        and path.resolve() == path,
        "canonical_absolute_path_required",
    )
    return path


def load_config(path):
    path = absolute_path(path)
    value = strict_json(private_file(path, maximum=MAX_BYTES, readonly=True))
    required = {"schema_version", "topic", "scope_key_file", "state_directory"}
    require(
        isinstance(value, dict)
        and required <= value.keys()
        and value.keys() <= required | {"bootstrap"},
        "invalid_conversation_configuration",
    )
    require(
        type(value["schema_version"]) is int and value["schema_version"] == 1,
        "invalid_conversation_configuration",
    )
    topic_name(value["topic"])
    absolute_path(value["scope_key_file"])
    absolute_path(value["state_directory"])
    if "bootstrap" in value:
        endpoint = value["bootstrap"]
        require(
            isinstance(endpoint, dict)
            and set(endpoint) == {"host", "port"}
            and endpoint["host"] == "127.0.0.1"
            and type(endpoint["port"]) is int
            and 1 <= endpoint["port"] <= 65535,
            "invalid_loopback_bootstrap",
        )
    return value


class ProfileState:
    """Exclusive key/scope/topic/store binding; incomplete state is never reset."""

    def __init__(self, state_dir, scope_file, topic):
        self.directory = absolute_path(state_dir)
        scope_file = absolute_path(scope_file)
        self.topic = topic_name(topic)
        raw_scope = private_file(scope_file, maximum=SCOPE_BYTES, readonly=True)
        require(raw_scope and b"\0" not in raw_scope, "scope_must_be_nonempty_utf8")
        try:
            self.secret = raw_scope.decode("utf-8")
        except UnicodeError:
            raise ProfileError("scope_must_be_nonempty_utf8") from None
        self.scope_id = hashlib.sha256(raw_scope).hexdigest()
        self.lock_fd, self.dir_fd, self.mesh = None, None, None
        self.created_lock = False
        try:
            self.directory.mkdir(mode=0o700, exist_ok=True)
            self.dir_fd = os.open(
                self.directory,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            info = os.fstat(self.dir_fd)
            require(
                stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.getuid(),
                "state_directory_must_be_private_and_owned",
            )
            lock = self.directory / "owner.lock"
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            try:
                self.lock_fd = os.open(lock, flags | os.O_CREAT | os.O_EXCL, 0o600)
                self.created_lock = True
            except FileExistsError:
                self.lock_fd = os.open(lock, flags)
            info = os.fstat(self.lock_fd)
            require(
                stat.S_ISREG(info.st_mode)
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_uid == os.getuid()
                and info.st_nlink == 1,
                "state_lock_must_be_private_owned_regular",
            )
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProfileError("conversation_state_already_owned") from None
            self._open()
        except BaseException:
            self.close()
            raise

    def _open(self):
        key_path = self.directory / "identity.pem"
        database = self.directory / "conversation.sqlite3"
        binding_path = self.directory / "binding.json"
        names = {path.name for path in self.directory.iterdir()}
        require(
            names
            <= {
                "owner.lock",
                "identity.pem",
                "conversation.sqlite3",
                "binding.json",
                "conversation.sqlite3-journal",
                "conversation.sqlite3-wal",
                "conversation.sqlite3-shm",
            },
            "unexpected_or_incomplete_state_files",
        )
        for name in names - {"owner.lock"}:
            private_file(self.directory / name)
        retained = "binding.json" in names
        if retained:
            require(
                {"identity.pem", "conversation.sqlite3"} <= names,
                "retained_identity_or_store_missing",
            )
            key = serialization.load_pem_private_key(
                private_file(key_path, maximum=4096), password=None
            )
            require(
                isinstance(key, ed25519.Ed25519PrivateKey), "retained_identity_invalid"
            )
            binding = strict_json(private_file(binding_path, maximum=4096))
        else:
            require(
                self.created_lock and names == {"owner.lock"},
                "incomplete_state_requires_explicit_recovery",
            )
            key = ed25519.Ed25519PrivateKey.generate()
            create_private(
                key_path,
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )
            create_private(database, b"")
            binding = None
        public = (
            key.public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            .hex()
        )
        expected = {
            "schema_version": 1,
            "profile": PROFILE,
            "topic": self.topic,
            "swarm_id": self.scope_id,
            "public_key": public,
            "author": hashlib.sha256(bytes.fromhex(public)).hexdigest()[:16],
            "session_id": session_id(self.scope_id, self.topic),
            "visibility": "public",
            "execution_authorized": False,
        }
        if retained:
            require(
                isinstance(binding, dict)
                and set(binding) == set(expected) | {"stream_id"}
                and all(
                    type(binding[name]) is type(value) and binding[name] == value
                    for name, value in expected.items()
                )
                and isinstance(binding["stream_id"], str)
                and re.fullmatch(r"[0-9a-f]{32}", binding["stream_id"]),
                "retained_identity_scope_or_topic_mismatch",
            )
            # SQLite constructors create absent tables/metadata. Refuse damaged
            # retained bindings before any mutating constructor can heal them.
            db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                require(STATE_TABLES <= tables, "retained_conversation_tables_missing")
                require(
                    db.execute(
                        "SELECT singleton,scope_id FROM conversation_binding LIMIT 2"
                    ).fetchall()
                    == [(1, self.scope_id)],
                    "retained_scope_binding_mismatch",
                )
                require(
                    db.execute(
                        "SELECT singleton,stream_id FROM conversation_stream LIMIT 2"
                    ).fetchall()
                    == [(1, binding["stream_id"])],
                    "retained_stream_binding_mismatch",
                )
            finally:
                db.close()
        storage = SQLiteStorageProvider(str(database))
        try:
            self.mesh = LiminalMesh(
                self.secret,
                storage,
                identity_path=str(key_path),
                conversation_only=True,
                conversation_identity=key,
            )
        except BaseException:
            if storage.conn is not None:
                storage.conn.close()
            raise
        store = self.mesh.require_conversation().store
        self.binding = expected | {"stream_id": store.stream_id}
        require(
            store.author == expected["author"] and store.public_key == public,
            "active_signing_identity_mismatch",
        )
        if retained:
            require(self.binding == binding, "retained_binding_changed")
        else:
            create_private(binding_path, canonical(self.binding).encode())
            os.fsync(self.dir_fd)

    def close(self):
        try:
            if self.mesh is not None and self.mesh.storage.conn is not None:
                try:
                    self.mesh.storage.conn.close()
                finally:
                    self.mesh.storage.conn = None
        finally:
            for name in ("lock_fd", "dir_fd"):
                fd = getattr(self, name, None)
                if fd is not None:
                    try:
                        os.close(fd)
                    finally:
                        setattr(self, name, None)


def tool_schemas():
    string = {"type": "string"}
    integer = {"type": "integer"}

    def nullable(value):
        return {"anyOf": [value, {"type": "null"}]}

    key = nullable(string)
    history = {"topic": string, "after": integer, "limit": integer, "stream_id": key}
    shapes = {
        "conversation_status": ({}, []),
        "conversation_start": ({"topic": string, "request_key": key}, ["topic"]),
        "conversation_join": (
            {
                "topic": string,
                "decision": string,
                "message": string,
                "request_key": key,
            },
            ["topic"],
        ),
        "conversation_say": (
            {
                "topic": string,
                "message": string,
                "request_key": key,
                "reply_to": key,
                "kind": string,
                "target": key,
                "references": nullable(
                    {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": string,
                        },
                    }
                ),
            },
            ["topic", "message"],
        ),
        "conversation_history": (history, ["topic"]),
        "conversation_watch": (history | {"timeout": {"type": "number"}}, ["topic"]),
    }
    return {
        name: {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        for name, (properties, required) in shapes.items()
    }


def valid_shape(value, schema):
    if "anyOf" in schema:
        return any(valid_shape(value, option) for option in schema["anyOf"])
    kind = schema["type"]
    if kind == "null":
        return value is None
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return type(value) is int
    if kind == "number":
        return type(value) in (int, float) and math.isfinite(value)
    if kind == "array":
        return isinstance(value, list) and all(
            valid_shape(item, schema["items"]) for item in value
        )
    if kind == "object":
        return isinstance(value, dict) and all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        )
    return False


class ConversationProfile:
    """One retained identity, one owned child and fixed public-only operations."""

    def __init__(self, config):
        self.state = ProfileState(
            config["state_directory"], config["scope_key_file"], config["topic"]
        )
        self.mesh = self.state.mesh
        self.bootstrap = config.get("bootstrap")
        self.schemas = tool_schemas()
        self.process, self.ready = None, None
        self.tasks = []
        self.failed = asyncio.Event()
        self.failure = None
        self.closing = False
        self.application = asyncio.Queue(maxsize=4)
        self.outbound = asyncio.Queue(maxsize=2)
        self.outbound_bytes = 0
        self.stderr_bytes = 0
        self.mesh._send_to_sidecar = self._send

    def live(self):
        require(
            self.process is not None
            and self.process.returncode is None
            and self.ready is not None
            and self.ready.done()
            and not self.ready.cancelled()
            and self.ready.exception() is None
            and self.failure is None
            and not self.closing,
            "conversation_transport_unavailable",
        )

    @staticmethod
    def child_environment():
        return {
            "PATH": FIXED_PATH,
            "LANG": "C.UTF-8",
            "POLYPHONY_CONVERSATION_ONLY": "1",
        }

    async def start(self):
        require(self.process is None, "conversation_transport_already_started")
        node = shutil.which("node", path=FIXED_PATH)
        require(node is not None, "fixed_path_node_missing")
        for name in ("hyperswarm", "hyperdht", "hypercore-crypto", "b4a"):
            require(
                (SIDECAR / "node_modules" / name / "package.json").is_file(),
                "sidecar_dependencies_missing_run_npm_ci",
            )
        args = [node, str(SIDECAR / "bridge.js"), self.mesh.topic]
        if self.bootstrap is not None:
            args += ["--conversation-bootstrap-port", str(self.bootstrap["port"])]
        self.ready = asyncio.get_running_loop().create_future()
        try:
            async with asyncio.timeout(START_SECONDS):
                self.process = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=MAX_BYTES,
                    env=self.child_environment(),
                )
                self.tasks = [
                    asyncio.create_task(self._stdout()),
                    asyncio.create_task(self._stderr()),
                    asyncio.create_task(self._process_events()),
                    asyncio.create_task(self._writer()),
                ]
                await asyncio.shield(self.ready)
                self.live()
            self.mesh.running = True
            self.tasks.append(asyncio.create_task(self._announce()))
        except BaseException:
            await self.stop()
            raise

    def _fail(self, reason):
        if self.failure is None:
            self.failure = reason
        self.failed.set()
        if self.ready is not None and not self.ready.done():
            self.ready.set_exception(ProfileError(self.failure))

    def _event(self, value):
        require(isinstance(value, dict), "invalid_sidecar_frame")
        kind = value.get("type")
        require(isinstance(kind, str), "invalid_sidecar_frame")
        if kind == "conversation_ready":
            require(
                value
                == {"type": kind, "topic": self.mesh.topic, "profile": SIDECAR_PROFILE}
                and not self.ready.done(),
                "invalid_sidecar_readiness",
            )
            self.ready.set_result(True)
            return None
        require(
            kind in {"peer_connected", "peer_disconnected", "message"},
            "invalid_sidecar_event",
        )
        expected = (
            {"type", "peer_id", "payload"} if kind == "message" else {"type", "peer_id"}
        )
        require(
            set(value) == expected
            and isinstance(value["peer_id"], str)
            and re.fullmatch(r"[0-9a-f]{64}", value["peer_id"]),
            "invalid_sidecar_peer",
        )
        if kind == "message":
            envelope = value["payload"]
            require(
                isinstance(envelope, dict)
                and set(envelope) == {"e", "p", "s"}
                and all(isinstance(item, str) for item in envelope.values())
                and re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", envelope["e"])
                and re.fullmatch(r"[0-9a-f]{64}", envelope["p"])
                and re.fullmatch(r"[0-9a-f]{128}", envelope["s"]),
                "invalid_sidecar_envelope",
            )
        if not self.ready.done():
            # Connections can precede flushed readiness. No early envelope is
            # acknowledged; initial anti-entropy repairs discarded early chat.
            if kind == "peer_connected":
                require(
                    len(self.mesh.peers) < 64 or value["peer_id"] in self.mesh.peers,
                    "sidecar_peer_limit",
                )
                self.mesh.peers.add(value["peer_id"])
            elif kind == "peer_disconnected":
                self.mesh.peers.discard(value["peer_id"])
            return None
        return value

    async def _stdout(self):
        try:
            while line := await self.process.stdout.readline():
                require(
                    line.endswith(b"\n") and len(line) <= MAX_BYTES,
                    "sidecar_frame_exceeds_bound",
                )
                value = self._event(strict_json(line))
                if value is not None:
                    # Awaiting this finite queue stops the one reader; it does
                    # not create a task or a blocked producer per peer frame.
                    await self.application.put(value)
            if not self.closing:
                self._fail("conversation_sidecar_exited")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail("conversation_sidecar_protocol_failed")

    async def _stderr(self):
        try:
            while chunk := await self.process.stderr.read(4096):
                self.stderr_bytes += len(chunk)
                require(
                    self.stderr_bytes <= DIAGNOSTIC_BYTES, "sidecar_diagnostic_limit"
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail("conversation_sidecar_diagnostics_failed")

    async def _process_events(self):
        try:
            while True:
                value = await self.application.get()
                self.live()
                if value["type"] == "peer_connected":
                    require(
                        len(self.mesh.peers) < 64
                        or value["peer_id"] in self.mesh.peers,
                        "sidecar_peer_limit",
                    )
                async with asyncio.timeout(2):
                    await self.mesh._handle_message(value)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail("conversation_sidecar_processing_failed")

    async def _writer(self):
        active = None
        try:
            while True:
                raw, active = await self.outbound.get()
                self.outbound_bytes -= len(raw)
                self.process.stdin.write(raw)
                await asyncio.wait_for(self.process.stdin.drain(), 2)
                if not active.done():
                    active.set_result(None)
                active = None
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail("conversation_transport_submission_failed")
        finally:
            pending = [active] if active is not None else []
            while not self.outbound.empty():
                raw, future = self.outbound.get_nowait()
                self.outbound_bytes -= len(raw)
                pending.append(future)
            for future in pending:
                if not future.done():
                    if self.closing:
                        future.cancel()
                    else:
                        future.set_exception(
                            ProfileError("conversation_transport_submission_failed")
                        )

    async def _announce(self):
        try:
            while True:
                await self.mesh.require_conversation().announce()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail("conversation_transport_submission_failed")

    async def _send(self, operation, envelope):
        self.live()
        require(operation == "broadcast", "conversation_transport_operation_refused")
        raw = (canonical({"type": operation, "payload": envelope}) + "\n").encode()
        require(len(raw) <= MAX_BYTES, "sidecar_frame_exceeds_bound")
        if self.outbound.full() or self.outbound_bytes + len(raw) > 2 * MAX_BYTES:
            self._fail("conversation_transport_queue_limit")
            raise ProfileError("conversation_transport_queue_limit")
        future = asyncio.get_running_loop().create_future()
        self.outbound_bytes += len(raw)
        self.outbound.put_nowait((raw, future))
        try:
            await asyncio.wait_for(asyncio.shield(future), 2)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail("conversation_transport_submission_failed")
            raise ProfileError("conversation_transport_submission_failed") from None
        finally:
            if not future.done():
                future.cancel()

    async def call(self, name, arguments):
        require(
            isinstance(name, str)
            and name in self.schemas
            and isinstance(arguments, dict),
            "unknown_tool_or_arguments",
        )
        schema = self.schemas[name]
        require(
            set(schema["required"]) <= arguments.keys()
            and arguments.keys() <= schema["properties"].keys()
            and all(
                valid_shape(value, schema["properties"][key])
                for key, value in arguments.items()
            ),
            "invalid_tool_arguments",
        )
        require(
            len(canonical(arguments).encode()) <= MAX_BYTES,
            "tool_arguments_exceed_bound",
        )
        require(
            name == "conversation_status" or arguments["topic"] == self.state.topic,
            "conversation_topic_mismatch",
        )
        after, limit = arguments.get("after", 0), arguments.get("limit", 100)
        timeout = arguments.get("timeout", 30)
        require(0 <= after <= MAX_CURSOR and 1 <= limit <= 100, "invalid_history_range")
        require(0 <= timeout <= 30, "invalid_watch_timeout")
        stream = arguments.get("stream_id")
        require(
            (after == 0 or stream is not None)
            and (stream is None or stream == self.state.binding["stream_id"]),
            "conversation_stream_mismatch",
        )
        self.live()
        conversation = self.mesh.require_conversation()
        if name == "conversation_status":
            value = dict(self.state.binding)
        elif name in {"conversation_history", "conversation_watch"}:
            args = (self.state.topic, after, limit, stream)
            value = (
                conversation.store.history(*args)
                if name == "conversation_history"
                else await conversation.watch(*args, timeout=timeout)
            )
        else:
            options = {
                key: arguments[key]
                for key in ("request_key", "reply_to", "target", "references", "kind")
                if key in arguments
            }
            if name == "conversation_start":
                options["kind"] = "start"
            elif name == "conversation_join":
                decision = arguments.get("decision", "join")
                require(
                    decision in {"join", "decline", "leave"}, "invalid_join_decision"
                )
                options["kind"] = decision
            message = arguments.get("message", "")
            require(
                len(message.encode()) <= MAX_CONTENT_BYTES, "message_exceeds_utf8_bound"
            )
            value = await conversation.say(
                self.state.topic, message, include_record=True, **options
            )
        self.live()
        require(
            len(canonical(value).encode()) <= MAX_BYTES, "tool_result_exceeds_bound"
        )
        return value

    async def stop(self):
        if self.closing:
            return
        self.closing = True
        self.mesh.running = False
        failure = None
        drainers = []

        async def drain(reader):
            while await reader.read(4096):
                pass

        try:
            # Replace application readers with bounded-lifetime drainers before
            # waiting on the child. A full pipe cannot hold up observed reaping.
            for task in self.tasks:
                task.cancel()
            if self.tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*self.tasks, return_exceptions=True), 0.25
                    )
                except BaseException:
                    failure = ProfileError("conversation_cleanup_unconfirmed")
            if self.ready is not None and not self.ready.done():
                self.ready.cancel()
            if self.process is not None:
                drainers = [
                    asyncio.create_task(drain(reader))
                    for reader in (self.process.stdout, self.process.stderr)
                ]
                try:
                    self.process.stdin.close()
                except Exception:
                    failure = ProfileError("conversation_cleanup_unconfirmed")
                if self.process.returncode is None:
                    try:
                        self.process.terminate()
                    except ProcessLookupError:
                        pass
                    except Exception:
                        failure = ProfileError("conversation_cleanup_unconfirmed")
                    try:
                        await asyncio.wait_for(self.process.wait(), TERM_SECONDS)
                    except BaseException:
                        try:
                            self.process.kill()
                        except ProcessLookupError:
                            pass
                        await asyncio.wait_for(self.process.wait(), KILL_SECONDS)
        except BaseException:
            failure = ProfileError("conversation_cleanup_unconfirmed")
        finally:
            for task in self.tasks + drainers:
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self.tasks, *drainers, return_exceptions=True), 0.25
                )
            except BaseException:
                failure = ProfileError("conversation_cleanup_unconfirmed")
            self.state.close()
        if failure is not None:
            raise failure


def mcp_server(profile):
    from mcp.server import Server
    from mcp.types import CallToolResult, TextContent, Tool

    server = Server("Polyphony public conversation", version="1")

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name=name,
                description="Fixed-topic public conversation; no execution authority.",
                inputSchema=profile.schemas[name],
            )
            for name in TOOLS
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            result = await profile.call(name, arguments)
            return [TextContent(type="text", text=canonical(result))]
        except (ProfileError, ConversationError, StdioError):
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text="conversation_request_refused")],
            )
        except Exception:
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text="conversation_unavailable")],
            )

    return server


async def serve(config, diagnostics):
    from mcp.types import ServerCapabilities, ToolsCapability

    profile, tasks = None, []
    loop = asyncio.get_running_loop()
    owner = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, owner.cancel)
    try:
        profile = ConversationProfile(config)
        require(not diagnostics.exceeded.is_set(), "conversation_diagnostic_limit")
        async with bounded_stdio() as (read, write, wire):
            startup = asyncio.create_task(profile.start())
            end = asyncio.create_task(wire.ended.wait())
            diagnostic_limit = asyncio.create_task(diagnostics.exceeded.wait())
            tasks = [startup, end, diagnostic_limit]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if startup not in done:
                startup.cancel()
                outcome = (await asyncio.gather(startup, return_exceptions=True))[0]
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome, asyncio.CancelledError
                ):
                    raise outcome
                require(
                    not diagnostics.exceeded.is_set(), "conversation_diagnostic_limit"
                )
                require(wire.failure is None, "conversation_protocol_refused")
                return
            await startup
            require(not diagnostics.exceeded.is_set(), "conversation_diagnostic_limit")
            require(wire.failure is None, "conversation_protocol_refused")
            if wire.ended.is_set():
                return
            server = mcp_server(profile)
            options = server.create_initialization_options()
            options.capabilities = ServerCapabilities(
                tools=ToolsCapability(listChanged=False)
            )
            running = asyncio.create_task(server.run(read, write, options))
            failed = asyncio.create_task(profile.failed.wait())
            tasks.extend([running, failed])
            done, _ = await asyncio.wait(
                [running, failed, end, diagnostic_limit],
                return_when=asyncio.FIRST_COMPLETED,
            )
            require(
                failed not in done and diagnostic_limit not in done,
                "conversation_transport_unavailable",
            )
            require(wire.failure is None, "conversation_protocol_refused")
            if running in done:
                await running
    finally:
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), 0.5
                )
        finally:
            if profile is not None:
                await profile.stop()
            loop.remove_signal_handler(signal.SIGTERM)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    os.umask(0o077)
    diagnostic = sys.stderr
    try:
        require(len(argv) == 2 and argv[0] == "--config", "config_argument_required")
        config = load_config(argv[1])
        sink = Diagnostics()
        with redirect_stdout(sink), redirect_stderr(sink):
            asyncio.run(serve(config, sink))
    except (Exception, KeyboardInterrupt, asyncio.CancelledError):
        print(
            "Public conversation unavailable; check private retained state, installed dependencies and transport.",
            file=diagnostic,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
