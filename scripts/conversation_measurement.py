"""Strict helpers for the opt-in real-loopback measurement, not a packet API."""

import ipaddress
import re
import sqlite3
import struct

MAX_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_PACKETS = 200_000
MAX_RECORD_BYTES = 262_144
MAX_SAFE_INTEGER = (1 << 53) - 1


class MeasurementError(ValueError):
    """Safe aggregate qualification failure with no captured content."""


def require(condition, message):
    if not condition:
        raise MeasurementError(message)


def packet_totals():
    return {
        "packets": 0,
        "captured_original_frame_bytes": 0,
        "ip_total_bytes": 0,
        "udp_length_bytes": 0,
        "udp_payload_bytes": 0,
    }


def capture_statistics(stderr):
    """Keep tcpdump statistics distinct; received-by-filter is not packet count."""
    values = {}
    for label, phrase in (
        ("captured", "packets? captured"),
        ("received_by_filter", "packets? received by filter"),
        ("dropped_by_kernel", "packets? dropped by kernel"),
    ):
        found = re.findall(r"(?m)^([0-9]+) " + phrase + r"\s*$", stderr)
        require(len(found) == 1, "capture has no unique final statistics")
        values[label] = int(found[0])
    require(values["dropped_by_kernel"] == 0, "capture reported packet loss")
    return values


def parse_capture(path, flows, calibration, window, stderr):
    """Read one classic-PCAP Ethernet/IPv4 UDP capture, with no payload output.

    flows maps exact (source host/port, destination host/port) tuples to labels.
    calibration maps separate flow tuples to one expected synthetic datagram.
    The capture is already filtered to those endpoints; no hash deduplication
    removes retransmissions. Only the recorded steady wall-clock interval counts.
    """
    require(path.stat().st_size <= MAX_CAPTURE_BYTES, "capture file is oversized")
    require(set(flows).isdisjoint(calibration), "calibration overlaps peer traffic")
    require(
        len(set(flows.values())) == len(flows) == 2, "two directed peer flows required"
    )
    require(window[0] < window[1], "invalid capture window")
    stats = capture_statistics(stderr)
    totals = {label: packet_totals() for label in flows.values()}
    calibrations = {flow: 0 for flow in calibration}
    calibrated_lengths = []
    records, outside = 0, 0
    with path.open("rb") as source:
        header = source.read(24)
        require(len(header) == 24, "truncated PCAP header")
        formats = {
            b"\xd4\xc3\xb2\xa1": ("<", 1000),
            b"\xa1\xb2\xc3\xd4": (">", 1000),
            b"\x4d\x3c\xb2\xa1": ("<", 1),
            b"\xa1\xb2\x3c\x4d": (">", 1),
        }
        require(header[:4] in formats, "unsupported capture format")
        order, unit = formats[header[:4]]
        major, minor, zone, accuracy, snaplen, linktype = struct.unpack(
            order + "HHIIII", header[4:]
        )
        require(
            (major, minor, zone, accuracy) == (2, 4, 0, 0), "unsupported PCAP header"
        )
        require(
            65535 <= snaplen <= MAX_RECORD_BYTES, "unsupported capture snapshot length"
        )
        require(
            linktype == 1, "unsupported capture link type; Ethernet loopback required"
        )
        while True:
            record = source.read(16)
            if not record:
                break
            require(len(record) == 16, "truncated packet record")
            seconds, fraction, included, original = struct.unpack(
                order + "IIII", record
            )
            require(fraction < 1_000_000_000 // unit, "invalid packet timestamp")
            require(
                42 <= included == original <= snaplen,
                "truncated or oversized captured packet",
            )
            data = source.read(included)
            require(len(data) == included, "truncated packet bytes")
            records += 1
            require(records <= MAX_PACKETS, "capture packet limit exceeded")
            require(data[12:14] == b"\x08\x00", "unsupported packet link protocol")
            ip = data[14:]
            header_bytes = (ip[0] & 15) * 4
            require(
                ip[0] >> 4 == 4 and 20 <= header_bytes <= len(ip), "invalid IPv4 header"
            )
            total = int.from_bytes(ip[2:4], "big")
            require(header_bytes + 8 <= total <= len(ip), "invalid IPv4 total length")
            require(
                int.from_bytes(ip[6:8], "big") & 0x3FFF == 0,
                "fragmented capture is unsupported",
            )
            require(ip[9] == 17, "non-UDP packet in capture")
            udp = ip[header_bytes:total]
            src_port, dst_port, udp_length, _ = struct.unpack("!HHHH", udp[:8])
            require(8 <= udp_length == len(udp), "invalid UDP length")
            flow = (
                str(ipaddress.IPv4Address(ip[12:16])),
                src_port,
                str(ipaddress.IPv4Address(ip[16:20])),
                dst_port,
            )
            if flow in calibration:
                require(udp[8:] == calibration[flow], "calibration payload changed")
                calibrations[flow] += 1
                calibrated_lengths.append(
                    {
                        "expected_payload_bytes": len(calibration[flow]),
                        "observed_payload_bytes": udp_length - 8,
                        "udp_length_bytes": udp_length,
                        "ip_total_bytes": total,
                        "captured_original_frame_bytes": original,
                    }
                )
                continue
            require(flow in flows, "captured packet has no attributed test endpoint")
            stamp = seconds * 1_000_000_000 + fraction * unit
            if not window[0] <= stamp <= window[1]:
                outside += 1
                continue
            result = totals[flows[flow]]
            result["packets"] += 1
            result["captured_original_frame_bytes"] += original
            result["ip_total_bytes"] += total
            result["udp_length_bytes"] += udp_length
            result["udp_payload_bytes"] += udp_length - 8
    require(
        records == stats["captured"],
        "capture record count differs from final statistics",
    )
    require(
        len(calibrations) == 2 and set(calibrations.values()) == {1},
        "calibration must capture one datagram in each direction",
    )
    require(
        all(
            item["packets"] > 0 and item["udp_payload_bytes"] > 0
            for item in totals.values()
        ),
        "established peer traffic was not captured",
    )
    return {
        "interface": "lo",
        "format": "classic-PCAP",
        "linktype": linktype,
        "directions": totals,
        "calibration_datagrams": 2,
        "calibration_lengths": calibrated_lengths,
        "outside_steady_window_packets": outside,
        "capture_statistics": stats,
        "accounting": "one loopback capture; original virtual-frame, IP, UDP and UDP-payload lengths; retransmissions retained",
    }


def connection(snapshot):
    require(
        type(snapshot.get("schema_version")) is int
        and snapshot["schema_version"] == 1
        and snapshot.get("overflow") is False,
        "invalid measurement snapshot",
    )
    rows = snapshot.get("connections")
    require(
        isinstance(rows, list) and len(rows) == 1 and rows[0].get("connected") is True,
        "one stable connected peer is required",
    )
    return rows[0]


def endpoints(left, right):
    """Resolve a wildcard bind only from the other authenticated connection."""
    a, b = (connection(snapshot)["endpoints"] for snapshot in (left, right))
    for item in (a, b):
        require(
            item["local"] and item["remote"],
            "transport endpoint metadata is unavailable",
        )
        require(
            ipaddress.ip_address(item["remote"]["host"]).is_loopback,
            "peer did not use the qualified loopback path",
        )
        require(
            ipaddress.ip_address(item["remote"]["host"]).version == 4,
            "IPv4 peer endpoints required",
        )
    require(
        a["local"]["port"] == b["remote"]["port"]
        and b["local"]["port"] == a["remote"]["port"],
        "peer endpoints do not describe one direct connection",
    )
    left_endpoint, right_endpoint = b["remote"], a["remote"]
    for bind, observed in ((a["local"], left_endpoint), (b["local"], right_endpoint)):
        require(
            bind["host"] in ("0.0.0.0", observed["host"]),
            "local endpoint changed or is not attributed",
        )
    require(left_endpoint != right_endpoint, "peer endpoints are not distinct")
    return left_endpoint, right_endpoint


def measurement_delta(before, after):
    a, b = connection(before), connection(after)
    require(
        before["window"] == after["window"], "measurement window changed unexpectedly"
    )
    require(
        a["connection_id"] == b["connection_id"] and a["endpoints"] == b["endpoints"],
        "transport connection or endpoints changed",
    )
    application = b["application"]
    for key, value in application.items():
        if (
            key.startswith(("min_", "max_"))
            and not application["write_submitted_frames"]
        ):
            require(value is None, "empty application window has frame extrema")
            continue
        require(
            type(value) is int and 0 <= value <= MAX_SAFE_INTEGER,
            "invalid application counter",
        )
        if not key.startswith(("min_", "max_")):
            require(a["application"][key] == 0, "application baseline was not reset")
    require(
        application["write_threw_frames"] == application["stream_error_events"] == 0,
        "application stream reported a write or asynchronous error",
    )
    if application["write_submitted_frames"]:
        require(
            0
            < application["min_submitted_frame_bytes"]
            <= application["max_submitted_frame_bytes"]
            < 65536,
            "observed real frame exceeded the bounded protocol envelope",
        )
    result = {"application": application}
    for scope in ("native_stream", "native_instance"):
        delta, unavailable = {}, {}
        for key, value in b[scope]["values"].items():
            old = a[scope]["values"].get(key)
            if old is None or value is None:
                delta[key] = None
                unavailable[key] = (
                    "counter unavailable from the pinned native transport"
                )
            else:
                require(
                    type(old) is int
                    and type(value) is int
                    and 0 <= old <= value <= MAX_SAFE_INTEGER,
                    "native counter changed generation, overflowed or is invalid",
                )
                delta[key] = value - old
        if scope == "native_stream" and delta.get("retransmits") is not None:
            require(
                a[scope]["values"]["retransmits"] <= 65535
                and b[scope]["values"]["retransmits"] <= 65535,
                "native retransmit counter exceeded its 16-bit representation",
            )
            packets = delta.get("packetsTransmitted")
            # libudx increments stream packets_tx beside retransmit_count. A
            # known delta below 2**16 rules out an otherwise invisible full wrap.
            if packets is None or packets >= 65536:
                delta["retransmits"] = None
                unavailable["retransmits"] = (
                    "16-bit wrap ambiguity: transmitted packet delta is unavailable or at least 65536"
                )
            else:
                require(
                    delta["retransmits"] <= packets,
                    "native retransmits exceed transmitted packets",
                )
        result[scope] = {
            "delta": delta,
            "unavailable": list(unavailable),
            "unavailable_reasons": unavailable,
        }
    return result


def storage_snapshot(path, db=None):
    own = db is None
    if own:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        metrics = {
            key: db.execute("PRAGMA " + key).fetchone()[0]
            for key in ("journal_mode", "page_size", "page_count", "freelist_count")
        }
    finally:
        if own:
            db.close()
    # A closed-store sample must not stat side files while its own information
    # connection is still open. Active samples retain the caller's connection.
    metrics["files"] = {}
    for name, suffix in (
        ("main", ""),
        ("journal", "-journal"),
        ("wal", "-wal"),
        ("shm", "-shm"),
    ):
        candidate = path.with_name(path.name + suffix)
        stat = candidate.stat() if candidate.exists() else None
        metrics["files"][name] = {
            "present": stat is not None,
            "bytes": stat.st_size if stat else 0,
            "allocated_bytes": (
                stat.st_blocks * 512 if stat and hasattr(stat, "st_blocks") else None
            ),
        }
    return metrics


def retained_ids(mesh, topic):
    after, ids, stream = 0, set(), mesh.conversation.store.stream_id
    for _ in range(100):
        page = mesh.conversation.store.history(
            topic, after=after, page_limit=100, stream_id=stream
        )
        require(
            page["gap"] is None and page["peer_gaps"] == {"count": 0, "items": []},
            "conversation recovery reported a gap",
        )
        for item in page["messages"]:
            identity = item["record"]["message_id"]
            require(identity not in ids, "history repeated a message ID")
            ids.add(identity)
        if not page["has_more"]:
            return ids
        require(page["next_cursor"] > after, "history pagination made no progress")
        after = page["next_cursor"]
    raise ValueError("history exceeded bounded measurement pages")
