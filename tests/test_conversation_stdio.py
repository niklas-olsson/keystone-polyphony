"""Raw adversarial input around the actual lowlevel SDK and bounded adapter."""

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest

from src.liminal_bridge.conversation_stdio import (
    DIAGNOSTIC_BYTES,
    FRAME_BYTES,
    WIRE_BYTES,
    Diagnostics,
    StdioError,
    Wire,
    encoded,
    strict_json,
)

ROOT = Path(__file__).resolve().parents[1]
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "boundary-test", "version": "1"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
SERVER = r"""
import asyncio,json,sys
from pathlib import Path
from mcp.server.lowlevel import Server
from mcp.types import ServerCapabilities,ToolsCapability,Tool,TextContent
from src.liminal_bridge.conversation_stdio import bounded_stdio
async def main():
    server=Server("boundary-test",version="1")
    @server.list_tools()
    async def tools():
        return [Tool(name="test",inputSchema={"type":"object"})]
    @server.call_tool(validate_input=False)
    async def call(name,arguments):
        with open(sys.argv[1],"a") as out: out.write(json.dumps(arguments)+"\n")
        if arguments.get("block"): await asyncio.sleep(30)
        if arguments.get("notify"):
            await server.request_context.session.send_log_message("info","forbidden")
        return [TextContent(type="text",text=arguments.get("text","accepted"))]
    options=server.create_initialization_options()
    options.capabilities=ServerCapabilities(tools=ToolsCapability(listChanged=False))
    async with bounded_stdio() as (read,write,wire):
        run=asyncio.create_task(server.run(read,write,options))
        end=asyncio.create_task(wire.ended.wait())
        try:
            await asyncio.wait([run,end],return_when=asyncio.FIRST_COMPLETED)
        finally:
            run.cancel(); end.cancel()
            await asyncio.gather(run,end,return_exceptions=True)
        return 2 if wire.failure else 0
raise SystemExit(asyncio.run(main()))
"""


class Endpoint:
    def __init__(self, tmp_path):
        self.log = tmp_path / "admitted.jsonl"
        self.process = subprocess.Popen(
            [sys.executable, "-c", SERVER, str(self.log)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.buffer = bytearray()

    def send(self, value):
        self.process.stdin.write(encoded(value) + b"\n")

    def response(self):
        end = time.monotonic() + 3
        while b"\n" not in self.buffer:
            assert time.monotonic() < end, "bounded response did not arrive"
            readable, _, _ = select.select(
                [self.process.stdout], [], [], max(0, end - time.monotonic())
            )
            assert readable, "bounded response did not arrive"
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                return None
            self.buffer.extend(chunk)
        line, _, remainder = self.buffer.partition(b"\n")
        self.buffer = bytearray(remainder)
        return json.loads(line)

    def initialize(self):
        self.send(INITIALIZE)
        assert self.response()["result"]["capabilities"] == {
            "tools": {"listChanged": False}
        }
        self.send(INITIALIZED)

    def calls(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def wait_for_call(self):
        end = time.monotonic() + 3
        while not self.calls():
            assert time.monotonic() < end
            time.sleep(0.01)

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=2)
            self.process.stdout.close()
            self.process.stderr.close()


@pytest.fixture
def endpoint(tmp_path):
    value = Endpoint(tmp_path)
    try:
        yield value
    finally:
        value.close()


def call(number, **arguments):
    return {
        "jsonrpc": "2.0",
        "id": number,
        "method": "tools/call",
        "params": {"name": "test", "arguments": arguments},
    }


def test_real_sdk_serial_control_and_fragmented_utf8(endpoint):
    raw = encoded(INITIALIZE) + b"\n"
    for byte in raw:
        endpoint.process.stdin.write(bytes([byte]))
    assert endpoint.response()["id"] == 0
    endpoint.send(INITIALIZED)
    for identifier in (1, 2):
        endpoint.send(call(identifier, text="å🙂"))
        result = endpoint.response()
        assert result["id"] == identifier
        assert result["result"]["content"][0]["text"] == "å🙂"
    assert len(endpoint.calls()) == 2


def test_second_request_cannot_start_while_owned_handler_is_active(endpoint):
    endpoint.initialize()
    endpoint.send(call(1, block=True))
    endpoint.wait_for_call()
    endpoint.send(call(2, text="must not run"))
    assert endpoint.process.wait(timeout=3) == 2
    assert endpoint.calls() == [{"block": True}]


def test_cancel_is_terminal_even_if_sdk_error_precedes_handler_cleanup(endpoint):
    endpoint.initialize()
    endpoint.send(call(1, block=True))
    endpoint.wait_for_call()
    cancellation = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 1, "reason": "private-reason-canary"},
    }
    endpoint.process.stdin.write(
        encoded(cancellation) + b"\n" + encoded(call(2)) + b"\n"
    )
    assert endpoint.process.wait(timeout=3) == 0
    assert endpoint.calls() == [{"block": True}]
    assert b"private-reason-canary" not in endpoint.process.stderr.read()
    remaining = endpoint.process.stdout.read()
    assert b'"id":2' not in remaining


@pytest.mark.parametrize(
    "raw",
    [
        b'{"jsonrpc":"2.0","jsonrpc":"2.0","id":1,"method":"initialize"}\n',
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"value":1e999}}\n',
        b'{"value":NaN}\n',
        b'{"value":Infinity}\n',
        b"\xff\n",
        b"[]\n",
        b"\n",
        b"x" * FRAME_BYTES,
        b'{"jsonrpc":"2.0","id":true,"method":"initialize"}\n',
        b'{"jsonrpc":"2.0","id":-1,"method":"initialize"}\n',
        b'{"jsonrpc":"2.0","id":1,"result":{}}\n',
        b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1}}\n',
    ],
)
def test_raw_refusal_occurs_before_any_sdk_handler(tmp_path, raw):
    log = tmp_path / "admitted.jsonl"
    result = subprocess.run(
        [sys.executable, "-c", SERVER, str(log)],
        cwd=ROOT,
        input=raw,
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == 2
    assert not result.stdout
    assert not log.exists()


def test_unterminated_frame_fails_at_eof(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", SERVER, str(tmp_path / "calls")],
        cwd=ROOT,
        input=encoded(INITIALIZE),
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == 2 and not result.stdout


def test_unsolicited_sdk_output_never_reaches_stdout(endpoint):
    endpoint.initialize()
    endpoint.send(call(1, notify=True))
    assert endpoint.process.wait(timeout=3) == 2
    assert endpoint.process.stdout.read() == b""
    assert len(endpoint.calls()) == 1


def test_cumulative_real_wire_limit_stops_repeated_valid_requests(endpoint):
    endpoint.initialize()
    text = "x" * 200000
    for identifier in (1, 2):
        endpoint.send(call(identifier, text=text))
        assert endpoint.response()["result"]["content"][0]["text"] == text
    endpoint.send(call(3, text=text))
    assert endpoint.response() is None
    assert endpoint.process.wait(timeout=3) == 2
    assert 2 <= len(endpoint.calls()) <= 3


def test_outer_json_escaping_counts_toward_exact_frame_and_wire_limits():
    wire = Wire()
    wire.active = 1
    value = {"jsonrpc": "2.0", "id": 1, "result": ""}
    overhead = len(encoded(value)) + 1
    value["result"] = "x" * (FRAME_BYTES - overhead)
    assert len(wire.response(value)) == FRAME_BYTES
    value["result"] += "x"
    with pytest.raises(StdioError):
        wire.response(value)
    wire.total = WIRE_BYTES
    with pytest.raises(StdioError):
        wire.response({"jsonrpc": "2.0", "id": 1, "result": {}})


def test_nested_nonfinite_and_shared_diagnostics_are_bounded():
    for raw in (b'{"nested":[1e999]}', b'{"nested":[-1e999]}'):
        with pytest.raises(StdioError):
            strict_json(raw)
    sink = Diagnostics()
    sink.write("private-canary")
    assert sink.__dict__ == {"count": 14, "exceeded": sink.exceeded}
    sink.write("x" * DIAGNOSTIC_BYTES)
    assert sink.exceeded.is_set()
