"""Measurement rejection/oracle tests. PCAP and native counters are fixtures."""

from copy import deepcopy
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess

import pytest

from scripts.conversation_measurement import (
    MeasurementError,
    capture_statistics,
    endpoints,
    measurement_delta,
    parse_capture,
    retained_ids,
    storage_snapshot,
)
from src.liminal_bridge.mesh import LiminalMesh
from src.liminal_bridge.storage import SQLiteStorageProvider

ROOT = Path(__file__).resolve().parents[1]
FORWARD = ("127.0.0.1", 1201, "127.0.0.1", 1202)
REVERSE = ("127.0.0.1", 1202, "127.0.0.1", 1201)
CALIBRATION = {
    ("127.0.0.1", 1301, "127.0.0.1", 1302): b"calibrate-a",
    ("127.0.0.1", 1302, "127.0.0.1", 1301): b"calibrate-b",
}
FLOWS = {FORWARD: "forward", REVERSE: "reverse"}


def packet(flow, content):
    source, src_port, destination, dst_port = flow
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        28 + len(content),
        0,
        0,
        64,
        17,
        0,
        bytes(map(int, source.split("."))),
        bytes(map(int, destination.split("."))),
    )
    return (
        bytes(12)
        + b"\x08\x00"
        + ip
        + struct.pack("!HHHH", src_port, dst_port, 8 + len(content), 0)
        + content
    )


def pcap(records, order="<"):
    magic = b"\xd4\xc3\xb2\xa1" if order == "<" else b"\xa1\xb2\xc3\xd4"
    result = magic + struct.pack(order + "HHIIII", 2, 4, 0, 0, 262144, 1)
    for frame, seconds in records:
        result += (
            struct.pack(order + "IIII", seconds, 123, len(frame), len(frame)) + frame
        )
    return result


def records():
    # An identical second data packet represents a real retransmission. It must
    # remain in the totals, unlike duplicate observation at two capture points.
    return [(packet(flow, value), 9) for flow, value in CALIBRATION.items()] + [
        (packet(FORWARD, b"x" * 100), 10),
        (packet(FORWARD, b"x" * 100), 10),
        (packet(REVERSE, b"ack"), 10),
    ]


def stats(count=5, dropped=0):
    return f"{count} packets captured\n99 packets received by filter\n{dropped} packets dropped by kernel\n"


def parse(tmp_path, data=None, stderr=None):
    path = tmp_path / "traffic.pcap"
    path.write_bytes(data if data is not None else pcap(records()))
    return parse_capture(
        path,
        FLOWS,
        CALIBRATION,
        (10_000_000_000, 11_000_000_000),
        stderr if stderr is not None else stats(),
    )


@pytest.mark.parametrize("order", ["<", ">"])
def test_packet_accounting_keeps_real_retransmissions_and_layers_separate(
    tmp_path, order
):
    result = parse(tmp_path, pcap(records(), order))
    assert result["directions"]["forward"] == {
        "packets": 2,
        "captured_original_frame_bytes": 284,
        "ip_total_bytes": 256,
        "udp_length_bytes": 216,
        "udp_payload_bytes": 200,
    }
    assert result["directions"]["reverse"]["udp_payload_bytes"] == 3
    assert result["capture_statistics"]["received_by_filter"] == 99
    assert result["calibration_datagrams"] == 2
    assert all(
        row["expected_payload_bytes"] == row["observed_payload_bytes"] == 11
        and row["ip_total_bytes"] == row["udp_length_bytes"] + 20
        for row in result["calibration_lengths"]
    )
    assert "calibrate" not in json.dumps(result)


@pytest.mark.parametrize(
    "offset,format,value",
    [
        (20, "<I", 113),  # unsupported cooked-link format
        (24 + 12, "<I", 999),  # original packet was truncated
        (24 + 16 + 12, "!H", 0x86DD),  # IPv6 is outside this qualified parser
        (24 + 16 + 14 + 2, "!H", 65535),  # impossible IP total length
        (24 + 16 + 14 + 6, "!H", 0x2000),  # fragment needs reassembly
        (24 + 16 + 14 + 20 + 4, "!H", 65535),  # impossible UDP length
    ],
)
def test_corrupt_truncated_or_unsupported_capture_cannot_qualify(
    tmp_path, offset, format, value
):
    data = bytearray(pcap(records()))
    struct.pack_into(format, data, offset, value)
    with pytest.raises(MeasurementError):
        parse(tmp_path, data)


@pytest.mark.parametrize(
    "data",
    [b"", b"pcapng is not supported", pcap(records())[:-1], pcap(records()) + b"x"],
)
def test_partial_capture_cannot_qualify(tmp_path, data):
    with pytest.raises(MeasurementError):
        parse(tmp_path, data)


@pytest.mark.parametrize(
    "stderr", [stats(dropped=1), stats(count=4), "", stats() + stats()]
)
def test_loss_unknown_health_and_final_count_mismatch_fail(tmp_path, stderr):
    with pytest.raises(MeasurementError):
        parse(tmp_path, stderr=stderr)


def test_calibration_and_attribution_are_required(tmp_path):
    altered = records()
    altered.append(altered[0])
    with pytest.raises(MeasurementError, match="one datagram"):
        parse(tmp_path, pcap(altered), stats(6))
    altered = records()
    altered[-1] = (packet(("127.0.0.1", 9999, "127.0.0.1", 9998), b"unknown"), 10)
    with pytest.raises(MeasurementError, match="attributed"):
        parse(tmp_path, pcap(altered))
    with pytest.raises(MeasurementError, match="not captured"):
        parse(tmp_path, pcap([(frame, 9) for frame, _ in records()]))


def snapshot(port=1201, remote_port=1202):
    application = {
        key: 0
        for key in (
            "write_attempt_frames",
            "write_attempt_bytes",
            "write_submitted_frames",
            "write_submitted_bytes",
            "write_threw_frames",
            "write_threw_bytes",
            "write_backpressure_frames",
            "stream_error_events",
            "receive_bytes",
        )
    } | {"min_submitted_frame_bytes": None, "max_submitted_frame_bytes": None}
    counters = {
        key: 10
        for key in (
            "bytesTransmitted",
            "bytesReceived",
            "packetsTransmitted",
            "packetsReceived",
        )
    }
    return {
        "schema_version": 1,
        "window": 1,
        "overflow": False,
        "connections": [
            {
                "connection_id": 1,
                "connected": True,
                "application": application,
                "endpoints": {
                    "local": {"host": "0.0.0.0", "port": port},
                    "remote": {"host": "127.0.0.1", "port": remote_port},
                },
                "native_stream": {
                    "values": counters | {"retransmits": 0},
                    "unavailable": [],
                },
                "native_instance": {
                    "values": counters | {"packetsDroppedByKernel": 0},
                    "unavailable": [],
                },
            }
        ],
    }


def test_native_unknown_remains_unknown_and_scopes_are_not_added():
    before = snapshot()
    after = deepcopy(before)
    after["connections"][0]["application"]["receive_bytes"] = 512
    after["connections"][0]["native_stream"]["values"]["bytesReceived"] = 110
    after["connections"][0]["native_instance"]["values"]["bytesReceived"] = 210
    after["connections"][0]["native_stream"]["values"]["retransmits"] = None
    result = measurement_delta(before, after)
    assert result["native_stream"]["delta"]["bytesReceived"] == 100
    assert result["native_instance"]["delta"]["bytesReceived"] == 200
    assert result["native_stream"]["delta"]["retransmits"] is None
    assert result["native_stream"]["unavailable"] == ["retransmits"]
    assert result["application"]["max_submitted_frame_bytes"] is None


@pytest.mark.parametrize(
    "packet_delta,retransmits", [(65536, 0), (65537, 1), (70000, 4), (None, 0)]
)
def test_full_native_retransmit_wrap_cannot_appear_as_exact_zero_or_small_delta(
    packet_delta, retransmits
):
    before, after = snapshot(), snapshot()
    after["connections"][0]["native_stream"]["values"].update(
        packetsTransmitted=10 + packet_delta if packet_delta is not None else None,
        retransmits=retransmits,
    )
    result = measurement_delta(before, after)["native_stream"]
    assert result["delta"]["retransmits"] is None
    assert result["delta"]["packetsTransmitted"] == packet_delta
    assert "16-bit wrap ambiguity" in result["unavailable_reasons"]["retransmits"]


def test_bounded_native_retransmit_delta_requires_valid_raw_range():
    before, after = snapshot(), snapshot()
    after["connections"][0]["native_stream"]["values"].update(
        packetsTransmitted=13, retransmits=2
    )
    assert (
        measurement_delta(before, after)["native_stream"]["delta"]["retransmits"] == 2
    )
    after["connections"][0]["native_stream"]["values"]["retransmits"] = 65536
    with pytest.raises(MeasurementError, match="16-bit"):
        measurement_delta(before, after)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(window=2),
        lambda value: value["connections"][0].update(connection_id=2),
        lambda value: value["connections"][0].update(connected=False),
        lambda value: value["connections"][0]["endpoints"]["remote"].update(port=5555),
        lambda value: value["connections"][0]["native_stream"]["values"].update(
            retransmits=-1
        ),
        lambda value: value["connections"][0]["native_stream"]["values"].update(
            bytesReceived=9
        ),
        lambda value: value["connections"][0]["native_stream"]["values"].update(
            bytesReceived=True
        ),
        lambda value: value["connections"][0]["application"].update(
            stream_error_events=1
        ),
    ],
)
def test_reset_or_replaced_connections_cannot_produce_valid_deltas(mutate):
    before, after = snapshot(), snapshot()
    mutate(after)
    with pytest.raises(MeasurementError):
        measurement_delta(before, after)


def test_endpoint_inventory_requires_direct_loopback_pair():
    a, b = snapshot(), snapshot(1202, 1201)
    assert endpoints(a, b) == (
        {"host": "127.0.0.1", "port": 1201},
        {"host": "127.0.0.1", "port": 1202},
    )
    for changed in (
        {"host": "192.0.2.1", "port": 1201},
        {"host": "127.0.0.1", "port": 8888},
        None,
    ):
        other = deepcopy(b)
        other["connections"][0]["endpoints"]["remote"] = changed
        with pytest.raises(MeasurementError):
            endpoints(a, other)


def test_real_sqlite_pages_and_reopen_prove_ids_beyond_the_first_page(tmp_path):
    path = tmp_path / "node.db"

    def node():
        return LiminalMesh(
            "measurement-test-scope",
            SQLiteStorageProvider(str(path)),
            identity_path=str(tmp_path / "node.pem"),
            snapshot_path=str(tmp_path / "snapshot.json"),
        )

    mesh = node()
    try:
        before = storage_snapshot(path, mesh.storage.conn)
        ids = {
            mesh.conversation.store.post(
                "measurement", "x" * 256, request_key=f"message-{index:04d}"
            )["message_id"]
            for index in range(250)
        }
        assert mesh.conversation.store.history("measurement")["has_more"]
        assert retained_ids(mesh, "measurement") == ids
        after = storage_snapshot(path, mesh.storage.conn)
        assert before["journal_mode"] == after["journal_mode"]
        assert after["files"]["main"]["bytes"] > before["files"]["main"]["bytes"]
        assert set(after["files"]) == {"main", "journal", "wal", "shm"}
    finally:
        mesh.storage.conn.close()
    reopened = node()
    try:
        assert retained_ids(reopened, "measurement") == ids
    finally:
        reopened.storage.conn.close()


@pytest.mark.asyncio
async def test_harness_routes_only_top_level_diagnostics_locally(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "real_measurement", ROOT / "scripts/measure-conversation-real.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    peer = module.make_peer(tmp_path, "a", "scope", "127.0.0.1:1")
    observed = []

    async def handle(self, message):
        observed.append(message)

    monkeypatch.setattr(LiminalMesh, "_handle_message", handle)
    try:
        message = {
            "type": "message",
            "payload": {"type": "local_measurement", "request_id": "peer-chosen"},
        }
        await peer._handle_message(message)
        assert observed == [message]
        await peer._handle_message(
            {"type": "local_measurement", "request_id": "unknown", "snapshot": {}}
        )
        assert observed == [message]
    finally:
        peer.storage.conn.close()


def test_sidecar_disabled_mode_framing_and_local_only_diagnostics():
    result = subprocess.run(
        ["node", "--test", str(ROOT / "tests/test_sidecar_measurement.js")],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def load_real_measurement():
    spec = importlib.util.spec_from_file_location(
        "real_measurement", ROOT / "scripts/measure-conversation-real.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ControlledCaptureProcess:
    """Capture process lifecycle fixture; no packets or native capture."""

    def __init__(self, returncode=None, stopped_returncode=0):
        self.returncode = returncode
        self.stopped_returncode = stopped_returncode
        self.signals = []

    def send_signal(self, value):
        self.signals.append(value)

    async def wait(self):
        self.returncode = self.stopped_returncode
        return self.returncode


@pytest.mark.asyncio
async def test_actual_capture_requests_immediate_input_waits_for_readiness_and_stops(
    tmp_path, monkeypatch
):
    module = load_real_measurement()
    process = ControlledCaptureProcess()
    calls, waits = [], []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    async def become_ready(seconds):
        waits.append(seconds)
        capture.log.write(b"listening on lo, link-type EN10MB\n")
        capture.log.flush()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module.asyncio, "sleep", become_ready)
    capture = module.Capture(tmp_path, ["sudo", "-n", "tcpdump"], [FORWARD, REVERSE])
    await capture.start()
    assert waits == [0.05]
    assert len(calls) == 1
    args, options = calls[0]
    assert args == (
        "sudo",
        "-n",
        "tcpdump",
        "-i",
        "lo",
        "-Q",
        "in",
        "-n",
        "-p",
        "--immediate-mode",
        "-U",
        "-s",
        "0",
        "-B",
        "4096",
        "-Z",
        module.pwd.getpwuid(os.getuid()).pw_name,
        "-w",
        str(capture.path),
        "ip and udp and ((src host 127.0.0.1 and src port 1201 and dst host 127.0.0.1 and dst port 1202) or "
        "(src host 127.0.0.1 and src port 1202 and dst host 127.0.0.1 and dst port 1201))",
    )
    assert options == {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.DEVNULL,
        "stderr": capture.log,
    }
    await capture.stop(qualify=True)
    assert process.signals == [module.signal.SIGINT]
    assert process.returncode == 0 and capture.log.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["spawn", "exit"])
async def test_actual_capture_startup_failure_has_no_fallback_or_false_readiness(
    tmp_path, monkeypatch, failure
):
    module = load_real_measurement()
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        if failure == "spawn":
            raise OSError("controlled startup failure")
        # A readiness-looking line cannot override the process exit status.
        kwargs["stderr"].write(b"listening on lo, link-type EN10MB\n")
        kwargs["stderr"].flush()
        return ControlledCaptureProcess(returncode=1)

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    capture = module.Capture(tmp_path, ["tcpdump"], [FORWARD, REVERSE])
    try:
        with pytest.raises(OSError if failure == "spawn" else MeasurementError):
            await capture.start()
    finally:
        await capture.stop()
    assert len(calls) == 1 and "--immediate-mode" in calls[0]
    assert capture.log.closed


@pytest.mark.asyncio
async def test_actual_capture_nonzero_shutdown_cannot_qualify(tmp_path):
    module = load_real_measurement()
    capture = module.Capture(tmp_path, ["tcpdump"], [FORWARD, REVERSE])
    capture.process = ControlledCaptureProcess(stopped_returncode=1)
    capture.log = capture.log_path.open("xb")
    with pytest.raises(MeasurementError, match="capture did not exit successfully"):
        await capture.stop(qualify=True)
    assert capture.process.signals == [module.signal.SIGINT]
    assert capture.log.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [(None, None), ("original diagnostics", "0")])
@pytest.mark.parametrize("failure", [None, ValueError, asyncio.CancelledError])
async def test_measurement_opt_ins_restore_exact_values_on_success_failure_and_cancel(
    tmp_path, monkeypatch, previous, failure
):
    module = load_real_measurement()
    names = ("POLYPHONY_LOCAL_MEASUREMENT", "POLYPHONY_MEASUREMENT_LOOPBACK")
    for name, value in zip(names, previous):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    async def controlled(*args):
        assert [os.environ.get(name) for name in names] == ["1", "1"]
        if failure:
            raise failure("controlled scenario failure")
        return {"controlled": "complete"}

    monkeypatch.setattr(module, "measure_with_environment", controlled)
    if failure:
        with pytest.raises(failure):
            await module.measure(tmp_path / "scenario", 10, ["tcpdump"])
    else:
        assert await module.measure(tmp_path / "scenario", 10, ["tcpdump"]) == {
            "controlled": "complete"
        }
    assert tuple(os.environ.get(name) for name in names) == previous


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["directory", "log", "bootstrap"])
async def test_actual_scenario_setup_failures_restore_both_opt_ins(
    tmp_path, monkeypatch, stage
):
    module = load_real_measurement()
    directory = tmp_path / "scenario"
    monkeypatch.setenv("POLYPHONY_LOCAL_MEASUREMENT", "previous diagnostics")
    monkeypatch.delenv("POLYPHONY_MEASUREMENT_LOOPBACK", raising=False)
    if stage == "directory":
        directory.mkdir()
    elif stage == "log":
        original = Path.open

        def fail_log(path, *args, **kwargs):
            if path.name == "bootstrap.log":
                raise OSError("controlled log setup failure")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_log)
    else:

        async def fail_bootstrap(*args, **kwargs):
            assert os.environ["POLYPHONY_LOCAL_MEASUREMENT"] == "1"
            assert os.environ["POLYPHONY_MEASUREMENT_LOOPBACK"] == "1"
            raise OSError("controlled bootstrap setup failure")

        monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fail_bootstrap)
    with pytest.raises(OSError):
        await module.measure(directory, 10, ["tcpdump"])
    assert os.environ["POLYPHONY_LOCAL_MEASUREMENT"] == "previous diagnostics"
    assert "POLYPHONY_MEASUREMENT_LOOPBACK" not in os.environ


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "description, expected",
    [
        ("capture did not become ready", "capture did not become ready"),
        (
            "real peers did not discover and authenticate",
            "real peers did not discover and authenticate",
        ),
        (
            "remote durable message IDs are missing",
            "remote durable message IDs are missing",
        ),
        (
            "private-provider-token=must-never-be-exported",
            "measurement stage deadline exceeded",
        ),
    ],
)
async def test_harness_timeout_retains_only_its_allowlisted_stage(
    description, expected
):
    module = load_real_measurement()
    with pytest.raises(MeasurementError) as error:
        await module.until(lambda: False, lambda: None, 0, description)
    assert str(error.value) == expected


@pytest.mark.asyncio
async def test_invalid_local_diagnostic_keeps_untrusted_fields_private(tmp_path):
    module = load_real_measurement()
    peer = module.make_peer(tmp_path, "a", "scope", "127.0.0.1:1")
    future = asyncio.get_running_loop().create_future()
    peer.pending_measurement["request"] = future
    try:
        await peer._handle_message(
            {
                "type": "local_measurement",
                "request_id": "request",
                "snapshot": {},
                "private-provider-token": "must-never-be-exported",
            }
        )
        with pytest.raises(
            MeasurementError, match="^invalid local diagnostic response$"
        ):
            future.result()
    finally:
        peer.storage.conn.close()


@pytest.mark.parametrize(
    "failure, exported",
    [
        (
            MeasurementError("real peers did not discover and authenticate"),
            "real peers did not discover and authenticate",
        ),
        (
            ValueError("private-provider-token=must-never-be-exported"),
            "actual qualification failed; inspect private local diagnostics",
        ),
    ],
)
def test_harness_exports_safe_stage_without_arbitrary_exception_text(
    tmp_path, monkeypatch, capsys, failure, exported
):
    module = load_real_measurement()
    directory = tmp_path / "private-measurement"

    async def failed_run(*args):
        raise failure

    monkeypatch.setattr(module, "run", failed_run)
    monkeypatch.setattr(
        module.sys, "argv", ["measurement", "--work-dir", str(directory)]
    )
    monkeypatch.setattr(module.os, "umask", lambda mode: None)
    assert module.main() == 1
    captured = capsys.readouterr()
    artifact = (directory / "diagnostics.json").read_text()
    expected = {
        "passed": False,
        "error_type": type(failure).__name__,
        "error": exported,
    }
    assert json.loads(captured.err) == json.loads(artifact) == expected
    assert captured.out == ""
    assert not (directory / "report.json").exists()
    assert "must-never-be-exported" not in artifact + captured.err + captured.out
    assert str(directory) not in artifact + captured.err + captured.out
