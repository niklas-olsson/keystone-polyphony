"""Bounded raw stdio around the real MCP SDK, with one owned request."""

from contextlib import asynccontextmanager
import asyncio
import json
import math
import os

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

FRAME_BYTES = 262144
WIRE_BYTES = 1048576
DIAGNOSTIC_BYTES = 65536


class StdioError(RuntimeError):
    """A fixed diagnostic; raw protocol data is never included."""


def require(value, reason="conversation_protocol_refused"):
    if not value:
        raise StdioError(reason)


def object_pairs(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value)
        value[key] = item
    return value


def strict_json(raw):
    def nonfinite(_):
        raise StdioError("conversation_protocol_refused")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=nonfinite,
        )
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, float):
                require(math.isfinite(item))
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise StdioError("conversation_protocol_refused") from None


def encoded(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StdioError("conversation_protocol_refused") from None


def fields(value, required, optional=()):
    require(
        isinstance(value, dict)
        and required <= value.keys()
        and value.keys() <= required | set(optional)
    )


class Diagnostics:
    """Discard shared-library text, retaining only a cumulative byte count."""

    def __init__(self):
        self.count = 0
        self.exceeded = asyncio.Event()

    def write(self, value):
        if isinstance(value, str):
            self.count += len(value.encode("utf-8", errors="replace"))
            if self.count > DIAGNOSTIC_BYTES:
                self.exceeded.set()
            return len(value)
        self.exceeded.set()
        return 0

    def flush(self):
        pass

    def isatty(self):
        return False


class Wire:
    def __init__(self):
        self.total = 0
        self.active = None
        self.initialize_seen = False
        self.initialize_returned = False
        self.initialized = False
        self.cancelled = False
        self.ended = asyncio.Event()
        self.failure = None

    def fail(self, reason):
        if self.failure is None:
            self.failure = reason
        self.ended.set()

    def charge(self, count):
        self.total += count
        require(self.total <= WIRE_BYTES, "conversation_wire_limit")

    def incoming(self, value):
        fields(value, {"jsonrpc", "method"}, {"id", "params"})
        require(value["jsonrpc"] == "2.0" and not self.cancelled)
        method, params = value["method"], value.get("params")
        require(isinstance(method, str))
        if method == "notifications/initialized":
            require(
                "id" not in value
                and self.initialize_returned
                and not self.initialized
                and self.active is None
                and params in (None, {})
            )
            self.initialized = True
        elif method == "notifications/cancelled":
            require("id" not in value and self.active is not None)
            fields(params, {"requestId"}, {"reason"})
            require(type(params["requestId"]) is int)
            require(params["requestId"] == self.active)
            reason = params.get("reason")
            require(reason is None or isinstance(reason, str))
            # A cancellation is terminal. The SDK can send its error response
            # before the old handler finishes unwinding; that response must not
            # reopen admission for a second handler.
            self.cancelled = True
            value = dict(value, params={"requestId": self.active})
        else:
            require(type(value.get("id")) is int and value["id"] >= 0)
            require(self.active is None)
            if method == "initialize":
                require(not self.initialize_seen)
                fields(params, {"protocolVersion", "capabilities", "clientInfo"})
                require(isinstance(params["protocolVersion"], str))
                require(params["capabilities"] == {})
                fields(params["clientInfo"], {"name", "version"}, {"title"})
                require(
                    all(isinstance(item, str) for item in params["clientInfo"].values())
                )
                self.initialize_seen = True
            elif method == "tools/list":
                require(self.initialized and params in (None, {}))
            elif method == "tools/call":
                require(self.initialized)
                fields(params, {"name", "arguments"})
                require(isinstance(params["name"], str))
                require(isinstance(params["arguments"], dict))
            else:
                require(False)
            self.active = value["id"]
        try:
            return SessionMessage(JSONRPCMessage.model_validate(value))
        except (ValueError, TypeError):
            raise StdioError("conversation_protocol_refused") from None

    def response(self, value):
        fields(value, {"jsonrpc", "id"}, {"result", "error"})
        require(
            value["jsonrpc"] == "2.0"
            and type(value["id"]) is int
            and self.active is not None
            and value["id"] == self.active
            and (("result" in value) != ("error" in value))
        )
        raw = encoded(value) + b"\n"
        require(len(raw) <= FRAME_BYTES, "conversation_frame_limit")
        self.charge(len(raw))
        return raw

    def completed(self):
        if not self.initialize_returned:
            self.initialize_returned = True
        if not self.cancelled:
            self.active = None


async def read_fd(fd, size):
    while True:
        try:
            return os.read(fd, size)
        except BlockingIOError:
            await anyio.wait_readable(fd)


async def write_fd(fd, raw):
    view = memoryview(raw)
    while view:
        try:
            count = os.write(fd, view)
            require(count > 0, "conversation_output_unavailable")
            view = view[count:]
        except BlockingIOError:
            await anyio.wait_writable(fd)


@asynccontextmanager
async def bounded_stdio(stdin_fd=0, stdout_fd=1):
    """Yield SDK streams and termination state without task-per-frame growth."""
    wire = Wire()
    incoming, read = anyio.create_memory_object_stream(1)
    write, outgoing = anyio.create_memory_object_stream(1)
    for fd in (stdin_fd, stdout_fd):
        os.set_blocking(fd, False)

    async def reader():
        buffer = bytearray()
        try:
            async with incoming:
                while True:
                    chunk = await read_fd(
                        stdin_fd, min(4096, FRAME_BYTES - len(buffer))
                    )
                    if not chunk:
                        require(not buffer, "conversation_truncated_frame")
                        wire.ended.set()
                        return
                    wire.charge(len(chunk))
                    buffer.extend(chunk)
                    while b"\n" in buffer:
                        line, _, remainder = buffer.partition(b"\n")
                        buffer = bytearray(remainder)
                        require(line and len(line) + 1 <= FRAME_BYTES)
                        await incoming.send(wire.incoming(strict_json(line)))
                        if wire.cancelled:
                            wire.ended.set()
                            return
                    require(len(buffer) < FRAME_BYTES, "conversation_frame_limit")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            wire.fail(
                str(error)
                if isinstance(error, StdioError)
                else "conversation_input_unavailable"
            )

    async def writer():
        try:
            async with outgoing:
                async for message in outgoing:
                    value = message.message.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    )
                    raw = wire.response(value)
                    # There is one response writer. A stalled host cannot keep
                    # a profile alive indefinitely or accumulate producer tasks.
                    with anyio.fail_after(2):
                        await write_fd(stdout_fd, raw)
                    wire.completed()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            wire.fail(
                str(error)
                if isinstance(error, StdioError)
                else "conversation_output_unavailable"
            )

    async with read, write:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(reader)
            tasks.start_soon(writer)
            try:
                yield read, write, wire
            finally:
                tasks.cancel_scope.cancel()
