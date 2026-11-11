#!/usr/bin/env python3
"""Measure actual steady Hyperswarm traffic and storage on Linux loopback.

Requires actual tcpdump capture; there is no fixture fallback. Use a new private
work directory. Export only report.json, never logs, journals, identities or PCAP.
"""

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.conversation_measurement import (
    MAX_CAPTURE_BYTES,
    MeasurementError,
    endpoints,
    measurement_delta,
    parse_capture,
    require,
    retained_ids,
    storage_snapshot,
)
from src.liminal_bridge.mesh import LiminalMesh
from src.liminal_bridge.storage import SQLiteStorageProvider

TOPIC = "qualified-real-measurement"
GOSSIP_SECONDS = 30
SETTLE_SECONDS = 0.5


class MeasuredMesh(LiminalMesh):
    """Observe only local diagnostics; ordinary real delivery is inherited."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_measurement = {}

    async def _handle_message(self, message):
        if message.get("type") == "local_measurement":
            future = self.pending_measurement.get(message.get("request_id"))
            if future is not None and not future.done():
                if set(message) == {"type", "request_id", "snapshot"}:
                    future.set_result(message["snapshot"])
                else:
                    future.set_exception(
                        ValueError("invalid local diagnostic response")
                    )
            return
        await super()._handle_message(message)

    async def measurement(self, operation):
        require(not self.pending_measurement, "concurrent diagnostic request")
        request = secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        self.pending_measurement[request] = future
        try:
            async with asyncio.timeout(5):
                self.process.stdin.write(
                    (
                        json.dumps(
                            {
                                "type": "local_measurement",
                                "request_id": request,
                                "operation": operation,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await self.process.stdin.drain()
                return await future
        finally:
            self.pending_measurement.pop(request, None)


async def stop_process(process, graceful_signal=signal.SIGTERM):
    if process is None or process.returncode is not None:
        return
    try:
        process.send_signal(graceful_signal)
    except ProcessLookupError:
        return await process.wait()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(process.wait(), 5)
        raise ValueError("measurement subprocess required forced cleanup") from None


async def stop_mesh(mesh):
    process = mesh.process
    try:
        async with asyncio.timeout(5) as deadline:
            await mesh.stop()
        require(not deadline.expired(), "mesh cleanup exceeded deadline")
    finally:
        try:
            await stop_process(process)
        finally:
            mesh.storage.conn.close()


async def until(predicate, check, seconds, description):
    try:
        async with asyncio.timeout(seconds):
            while True:
                check()
                value = predicate()
                if value:
                    return value
                await asyncio.sleep(0.05)
    except TimeoutError:
        raise ValueError(description) from None


class Capture:
    def __init__(self, directory, command, flows):
        self.path = directory / "traffic.pcap"
        self.log_path = directory / "capture.log"
        self.command, self.flows = command, flows
        self.process, self.log = None, None

    def check(self):
        require(
            self.process is not None and self.process.returncode is None,
            "actual capture exited during measurement",
        )
        require(
            not self.path.exists() or self.path.stat().st_size <= MAX_CAPTURE_BYTES,
            "capture size limit exceeded",
        )
        require(
            self.log_path.stat().st_size <= 65536, "capture diagnostic limit exceeded"
        )

    async def start(self):
        self.log = self.log_path.open("xb")
        expressions = [
            f"(src host {a} and src port {p} and dst host {b} and dst port {q})"
            for a, p, b, q in self.flows
        ]
        capture_filter = "ip and udp and (" + " or ".join(expressions) + ")"
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            "-i",
            "lo",
            "-Q",
            "in",
            "-n",
            "-p",
            "-U",
            "-s",
            "0",
            "-B",
            "4096",
            "-Z",
            pwd.getpwuid(os.getuid()).pw_name,
            "-w",
            str(self.path),
            capture_filter,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=self.log,
        )
        await until(
            lambda: "listening on lo," in self.log_path.read_text(errors="replace"),
            self.check,
            10,
            "capture did not become ready",
        )

    async def stop(self, qualify=False):
        try:
            await stop_process(self.process, signal.SIGINT)
            if qualify:
                require(
                    self.process is not None and self.process.returncode == 0,
                    "capture did not exit successfully",
                )
        finally:
            if self.log:
                self.log.close()


def make_peer(directory, name, key, bootstrap):
    peer = MeasuredMesh(
        key,
        SQLiteStorageProvider(str(directory / (name + ".db"))),
        identity_path=str(directory / (name + ".pem")),
        bootstrap=bootstrap,
        snapshot_path=str(directory / (name + ".snapshot.json")),
        mqtt_host=None,
        ble_enabled=False,
    )
    peer._gossip_interval_seconds = GOSSIP_SECONDS
    return peer


async def measure(directory, count, command):
    directory.mkdir(mode=0o700)
    key = secrets.token_hex(32)
    peers, processes, sockets = [], [], []
    bootstrap, capture = None, None
    report = None
    previous_opt_in = os.environ.get("POLYPHONY_LOCAL_MEASUREMENT")
    os.environ["POLYPHONY_LOCAL_MEASUREMENT"] = "1"
    started = time.monotonic()
    with (directory / "bootstrap.log").open("xb") as bootstrap_log:
        try:
            bootstrap = await asyncio.create_subprocess_exec(
                "node",
                str(ROOT / "src/liminal_bridge/sidecar/verify-bootstrap.js"),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=bootstrap_log,
            )
            line = (
                (await asyncio.wait_for(bootstrap.stdout.readline(), 15))
                .decode()
                .strip()
            )
            require(
                line.startswith("127.0.0.1:"), "actual loopback bootstrap did not start"
            )
            for name in ("sender", "receiver"):
                mesh = make_peer(directory, name, key, line)
                peers.append(mesh)
                await asyncio.wait_for(mesh.start(), 15)
                processes.append(mesh.process)
            a, b = peers

            def healthy():
                require(
                    bootstrap.returncode is None
                    and all(process.returncode is None for process in processes),
                    "native transport process exited",
                )
                require(
                    all(
                        peer.process is process and peer._sidecar_restart_count == 0
                        for peer, process in zip(peers, processes)
                    ),
                    "native transport restarted",
                )
                if capture is not None and capture.process is not None:
                    capture.check()

            await until(
                lambda: a.peers
                and b.peers
                and a.node_id in b.peer_map.values()
                and b.node_id in a.peer_map.values(),
                healthy,
                35,
                "real peers did not discover and authenticate",
            )
            require(a.node_id != b.node_id, "independently keyed peers required")
            await asyncio.sleep(SETTLE_SECONDS)
            initial = await asyncio.gather(
                *(peer.measurement("snapshot") for peer in peers)
            )
            left, right = endpoints(*initial)
            peer_flow = (left["host"], left["port"], right["host"], right["port"])
            flows = {
                peer_flow: "sender_to_receiver",
                (
                    peer_flow[2],
                    peer_flow[3],
                    peer_flow[0],
                    peer_flow[1],
                ): "receiver_to_sender",
            }
            for _ in range(2):
                udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sockets.append(udp)
                udp.bind(("127.0.0.1", 0))
                udp.setblocking(False)
            x, y = (udp.getsockname() for udp in sockets)
            calibration = {
                (x[0], x[1], y[0], y[1]): secrets.token_bytes(37),
                (y[0], y[1], x[0], x[1]): secrets.token_bytes(83),
            }
            capture = Capture(directory, command, list(flows) + list(calibration))
            await capture.start()
            loop = asyncio.get_running_loop()
            for sender, receiver in (
                (sockets[0], sockets[1]),
                (sockets[1], sockets[0]),
            ):
                source, destination = sender.getsockname(), receiver.getsockname()
                payload = calibration[(*source, *destination)]
                await loop.sock_sendto(sender, payload, destination)
                received, origin = await asyncio.wait_for(
                    loop.sock_recvfrom(receiver, 128), 2
                )
                require(
                    received == payload and origin == source,
                    "actual UDP calibration did not arrive",
                )
            storage = {
                name: {
                    "baseline": storage_snapshot(
                        directory / (name + ".db"), peer.storage.conn
                    )
                }
                for name, peer in zip(("sender", "receiver"), peers)
            }
            window_start = time.time_ns()
            steady_started = time.monotonic()
            before = await asyncio.gather(
                *(peer.measurement("reset") for peer in peers)
            )
            require(
                endpoints(*before) == (left, right),
                "peer endpoints changed before the corpus",
            )
            expected = set()
            async with asyncio.timeout(120):
                for index in range(count):
                    healthy()
                    posted = await a.conversation.say(
                        TOPIC, "x" * 256, request_key=f"message-{index:04d}"
                    )
                    expected.add(posted["message_id"])
                    if index % 25 == 0:
                        await asyncio.sleep(0)
                await until(
                    lambda: retained_ids(b, TOPIC) == expected,
                    healthy,
                    90,
                    "remote durable message IDs are missing",
                )
            require(
                len(expected) == count and retained_ids(a, TOPIC) == expected,
                "original messages were lost or duplicated",
            )
            await asyncio.sleep(SETTLE_SECONDS)
            healthy()
            after = await asyncio.gather(
                *(peer.measurement("snapshot") for peer in peers)
            )
            window_end = time.time_ns()
            steady_seconds = time.monotonic() - steady_started
            measured = {
                name: measurement_delta(start, end)
                for name, start, end in zip(("sender", "receiver"), before, after)
            }
            require(
                measured["sender"]["application"]["write_submitted_frames"] >= count
                and measured["receiver"]["application"]["receive_bytes"] > 0,
                "actual application stream did not submit and receive the corpus",
            )
            for name, peer in zip(("sender", "receiver"), peers):
                storage[name]["quiescent"] = storage_snapshot(
                    directory / (name + ".db"), peer.storage.conn
                )
            await capture.stop(qualify=True)
            wire = parse_capture(
                capture.path,
                flows,
                calibration,
                (window_start, window_end),
                capture.log_path.read_text(),
            )
            capture.path.unlink()
            # Stop the actual peers before reopening their exact identities/stores.
            for peer in peers:
                await stop_mesh(peer)
            for name in ("sender", "receiver"):
                storage[name]["closed"] = storage_snapshot(directory / (name + ".db"))
                reopened = make_peer(directory, name, key, line)
                try:
                    require(
                        retained_ids(reopened, TOPIC) == expected,
                        "reopened durable store lost expected message IDs",
                    )
                finally:
                    reopened.storage.conn.close()
            report = {
                "messages": count,
                "content_utf8_bytes_per_message": 256,
                "remote_ids_verified": count,
                "reopened_stores_verified": 2,
                "gossip_interval_seconds": GOSSIP_SECONDS,
                "settle_seconds": SETTLE_SECONDS,
                "steady_observed_seconds": round(steady_seconds, 6),
                "application_and_native": measured,
                "captured_packets": wire,
                "storage": storage,
            }
        finally:
            failures = await asyncio.gather(
                *(stop_mesh(peer) for peer in peers), return_exceptions=True
            )
            try:
                if capture is not None:
                    try:
                        await capture.stop()
                    finally:
                        capture.path.unlink(missing_ok=True)
            finally:
                for udp in sockets:
                    udp.close()
                try:
                    await stop_process(bootstrap)
                finally:
                    if previous_opt_in is None:
                        os.environ.pop("POLYPHONY_LOCAL_MEASUREMENT", None)
                    else:
                        os.environ["POLYPHONY_LOCAL_MEASUREMENT"] = previous_opt_in
                    for failure in failures:
                        if isinstance(failure, BaseException):
                            raise failure
    report["scenario_seconds_including_discovery_and_cleanup"] = round(
        time.monotonic() - started, 6
    )
    return report


async def run(directory, command):
    require(sys.platform == "linux", "qualified capture requires Linux loopback")
    require(
        (ROOT / "src/liminal_bridge/sidecar/node_modules/hyperswarm").is_dir(),
        "install locked sidecar dependencies with npm ci",
    )
    require(shutil.which(command[-1]) is not None, "tcpdump executable is unavailable")
    versions = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "node": subprocess.check_output(
            ["node", "--version"], text=True, timeout=5
        ).strip(),
        "tcpdump_libpcap": subprocess.check_output(
            [command[-1], "--version"], text=True, stderr=subprocess.STDOUT, timeout=5
        ).strip(),
        "polyphony_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=5
        ).strip(),
        "polyphony_worktree_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=ROOT,
                text=True,
                timeout=5,
            ).strip()
        ),
        "transport": {
            name: json.loads(
                (
                    ROOT
                    / "src/liminal_bridge/sidecar/node_modules"
                    / name
                    / "package.json"
                ).read_text()
            )["version"]
            for name in ("hyperswarm", "hyperdht", "udx-native")
        },
        "libudx_source": re.search(
            r"github:holepunchto/libudx#([a-f0-9]+)",
            (
                ROOT
                / "src/liminal_bridge/sidecar/node_modules/udx-native/CMakeLists.txt"
            ).read_text(),
        ).group(1),
        "package_lock_sha256": hashlib.sha256(
            (ROOT / "src/liminal_bridge/sidecar/package-lock.json").read_bytes()
        ).hexdigest(),
    }
    async with asyncio.timeout(600):
        reports = [
            await measure(directory / str(count), count, command)
            for count in (10, 100, 1000)
        ]
    return {
        "schema_version": 1,
        "passed": True,
        "qualification": "actual Hyperswarm and captured loopback IPv4 UDP with reopened SQLite stores",
        "model_calls": 0,
        "versions": versions,
        "reports": reports,
        "metric_boundaries": [
            "application submissions and observed bytes are not durable acknowledgements",
            "native UDX counters include control/retransmissions and some failed sends; scopes overlap",
            "one virtual-interface capture records actual packets; no physical-link or WAN cost inference",
            "wire window encloses the two local counter snapshots; snapshots are not globally atomic",
            "storage includes retained node/protocol state, not only message content",
        ],
        "not_measured": [
            "cold discovery/catch-up traffic",
            "model latency",
            "managed child sessions",
            "WAN or physical Ethernet cost",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir",
        required=True,
        type=Path,
        help="new private directory; export only report.json",
    )
    parser.add_argument(
        "--sudo-capture",
        action="store_true",
        help="use noninteractive sudo only for tcpdump on an authorized CI/host",
    )
    args = parser.parse_args()
    os.umask(0o077)
    directory = args.work_dir.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    command = (["sudo", "-n"] if args.sudo_capture else []) + ["tcpdump"]
    with (directory / "measurement.log").open("x") as log:
        try:
            with redirect_stdout(log), redirect_stderr(log):
                report = asyncio.run(run(directory, command))
        except Exception as exc:
            traceback.print_exception(exc, file=log)
            failure = {
                "passed": False,
                "error_type": type(exc).__name__,
                "error": (
                    str(exc)
                    if isinstance(exc, MeasurementError)
                    else "actual qualification failed; inspect private local diagnostics"
                ),
            }
            (directory / "diagnostics.json").write_text(
                json.dumps(failure, indent=2) + "\n"
            )
            print(json.dumps(failure), file=sys.stderr)
            return 1
    (directory / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
