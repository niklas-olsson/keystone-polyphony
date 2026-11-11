#!/usr/bin/env python3
"""Measure real signed protocol frames and SQLite growth with a labelled transport fixture.

These are application-frame byte counts, not UDP/TCP/Hyperswarm overhead or
model latency. The new private work directory retains synthetic node identities;
only the printed JSON report is suitable for a public CI artifact.
"""

import argparse
import asyncio
from collections import deque
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.liminal_bridge.mesh import LiminalMesh
from src.liminal_bridge.storage import SQLiteStorageProvider


class FrameFixture:
    def __init__(self, a, b):
        self.queue, self.frames = deque(), []
        for sender, receiver in ((a, b), (b, a)):

            async def send(operation, envelope, sender=sender, receiver=receiver):
                if operation != "broadcast":
                    raise AssertionError("unexpected transport operation")
                # Exact JSON envelope framing written by bridge.js to each peer.
                self.frames.append(
                    len(json.dumps(envelope, separators=(",", ":")).encode()) + 1
                )
                self.queue.append(
                    (
                        receiver,
                        {
                            "type": "message",
                            "peer_id": sender.node_id,
                            "payload": envelope,
                        },
                    )
                )

            sender._send_to_sidecar = send

    async def flush(self):
        while self.queue:
            receiver, message = self.queue.popleft()
            await receiver._handle_message(message)


def node(root, name):
    return LiminalMesh(
        "synthetic-public-conversation-measurement",
        SQLiteStorageProvider(str(root / (name + ".db"))),
        identity_path=str(root / (name + ".pem")),
        snapshot_path=str(root / (name + ".snapshot.json")),
    )


async def measure(root, count):
    root.mkdir(mode=0o700)
    a, b, cold = (node(root, name) for name in ("sender", "receiver", "cold"))
    steady = FrameFixture(a, b)
    for number in range(count):
        await a.conversation.say(
            "measurement", "x" * 256, request_key=f"message-{number:04}"
        )
        await steady.flush()
    assert (
        a.conversation.store.high_water() == b.conversation.store.high_water() == count
    )
    assert len(steady.frames) == count
    catch_up = FrameFixture(a, cold)
    await a.conversation.announce()
    await catch_up.flush()
    assert cold.conversation.store.high_water() == count
    report = {
        "messages": count,
        "content_utf8_bytes_per_message": 256,
        "steady_application_frames": len(steady.frames),
        "steady_application_bytes": sum(steady.frames),
        "steady_min_frame_bytes": min(steady.frames),
        "steady_max_frame_bytes": max(steady.frames),
        "catch_up_application_frames": len(catch_up.frames),
        "catch_up_application_bytes": sum(catch_up.frames),
        "catch_up_max_frame_bytes": max(catch_up.frames),
        "sender_sqlite_file_bytes": (root / "sender.db").stat().st_size,
        "receiver_sqlite_file_bytes": (root / "receiver.db").stat().st_size,
        "cold_receiver_sqlite_file_bytes": (root / "cold.db").stat().st_size,
    }
    assert report["catch_up_max_frame_bytes"] < 65536
    for mesh in (a, b, cold):
        mesh.storage.conn.close()
    return report


async def run(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    reports = [
        await measure(directory / str(count), count) for count in (10, 100, 1000)
    ]
    assert max(row["steady_max_frame_bytes"] for row in reports) <= (
        min(row["steady_min_frame_bytes"] for row in reports) + 256
    ), "steady post frames grew with stored history"
    return {
        "schema_version": 1,
        "passed": True,
        "transport": "explicit in-process delivery fixture; production encrypted/signed handlers",
        "measured": "JSON application frames submitted to sidecar and actual SQLite file sizes",
        "not_measured": [
            "Hyperswarm/UDP/TCP framing",
            "network latency",
            "model latency",
        ],
        "model_calls": 0,
        "reports": reports,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir",
        required=True,
        type=Path,
        help="new private directory for synthetic identities and durable journals",
    )
    args = parser.parse_args()
    print(
        json.dumps(asyncio.run(run(args.work_dir.resolve())), indent=2, sort_keys=True)
    )


if __name__ == "__main__":
    main()
