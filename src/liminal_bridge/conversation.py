"""Signed public discussion, durable local receipts and bounded anti-entropy.

Conversation delivery never invokes execution or confers managed authority.
Local receipt cursors order a node's log, not the participants' causal history.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import sqlite3
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

MAX_CONTENT_BYTES = 8192
MAX_RECORD_BYTES = 16384
MAX_PAGE_BYTES = 24576
MAX_PAGE_RECORDS = 32
MAX_HISTORY_RECORDS = 100
MAX_CURSOR = (1 << 63) - 1
KINDS = {"message", "start", "invite", "join", "decline", "leave", "result"}
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX32 = re.compile(r"[0-9a-f]{32}")
NODE = re.compile(r"[0-9a-f]{16}")
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
INTENT_FIELDS = {
    "topic",
    "session_id",
    "kind",
    "content",
    "reply_to",
    "target",
    "references",
    "request_key",
    "visibility",
    "swarm_id",
}
BODY_FIELDS = INTENT_FIELDS | {"schema_version", "author", "public_key", "timestamp"}
TRANSPORT_FIELDS = {
    "type",
    "origin",
    "timestamp",
    "urgency",
    "sender_pubkey",
    "message_id",
    "vc",
}


class ConversationError(ValueError):
    """Invalid or conflicting public conversation input."""


class ConversationUnsupported(RuntimeError):
    """This node's storage backend has no qualified conversation journal."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def topic_name(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise ConversationError(
            "topic must be nonempty text of at most 256 UTF-8 bytes"
        )
    return value


def session_id(swarm_id, topic):
    return digest(["polyphony-public-conversation-v1", swarm_id, topic_name(topic)])


def cursor(value):
    if type(value) is not int or not 0 <= value <= MAX_CURSOR:
        raise ConversationError("invalid conversation cursor")
    return value


def limit(value, maximum=MAX_HISTORY_RECORDS):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ConversationError("invalid conversation page limit")
    return value


def matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def validate_intent(value, swarm_id):
    if not isinstance(value, dict) or set(value) != INTENT_FIELDS:
        raise ConversationError("unexpected conversation fields")
    if (
        value["swarm_id"] != swarm_id
        or value["visibility"] != "public"
        or value["session_id"] != session_id(swarm_id, value["topic"])
        or not isinstance(value["kind"], str)
        or value["kind"] not in KINDS
        or not matches(KEY, value["request_key"])
        or not isinstance(value["content"], str)
        or len(value["content"].encode()) > MAX_CONTENT_BYTES
        or "\0" in value["content"]
        or (value["reply_to"] is not None and not matches(HEX64, value["reply_to"]))
        or (value["target"] is not None and not matches(NODE, value["target"]))
    ):
        raise ConversationError("invalid public conversation message")
    references = value["references"]
    if not isinstance(references, list) or len(references) > 8:
        raise ConversationError("result references must be bounded identifiers")
    for reference in references:
        if (
            not isinstance(reference, dict)
            or set(reference) != {"kind", "id"}
            or not isinstance(reference["kind"], str)
            or reference["kind"] not in {"task", "attempt", "child", "artifact"}
            or not matches(
                HEX64 if reference["kind"] == "artifact" else KEY, reference["id"]
            )
        ):
            raise ConversationError(
                "result references accept identifiers, not URLs or credentials"
            )
    return value


def verify_record(record, swarm_id):
    try:
        if (
            not isinstance(record, dict)
            or set(record) != {"message_id", "body", "signature"}
            or len(canonical(record).encode()) > MAX_RECORD_BYTES
        ):
            raise ConversationError("invalid signed conversation record")
        body = record["body"]
        if not isinstance(body, dict) or set(body) != BODY_FIELDS:
            raise ConversationError("invalid signed conversation body")
        validate_intent({key: body[key] for key in INTENT_FIELDS}, swarm_id)
        if (
            type(body["schema_version"]) is not int
            or body["schema_version"] != 1
            or not matches(HEX64, body["public_key"])
            or not matches(NODE, body["author"])
            or type(body["timestamp"]) not in {int, float}
            or not math.isfinite(body["timestamp"])
            or not 0 <= body["timestamp"] < 1e15
            or not matches(HEX64, record["message_id"])
            or record["message_id"] != digest(body)
            or body["reply_to"] == record["message_id"]
            or not isinstance(record["signature"], str)
            or not re.fullmatch(r"[0-9a-f]{128}", record["signature"])
        ):
            raise ConversationError(
                "invalid conversation identity or display timestamp"
            )
        public = bytes.fromhex(body["public_key"])
        if hashlib.sha256(public).hexdigest()[:16] != body["author"]:
            raise ConversationError("conversation author does not match its key")
        ed25519.Ed25519PublicKey.from_public_bytes(public).verify(
            bytes.fromhex(record["signature"]), canonical(body).encode()
        )
    except ConversationError:
        raise
    except Exception:
        raise ConversationError("conversation signature or record is invalid") from None
    return record


class ConversationStore:
    """SQLite capability within the existing node database.

    All methods run on the owning mesh event loop. The peer log cursor and its
    validated records commit in the same SQLite transaction.
    """

    def __init__(self, connection, private_key, swarm_id):
        if not isinstance(connection, sqlite3.Connection) or not matches(
            HEX64, swarm_id
        ):
            raise ConversationError("an initialized SQLite node database is required")
        self.db, self.private_key, self.swarm_id = connection, private_key, swarm_id
        public = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.public_key = public.hex()
        self.author = hashlib.sha256(public).hexdigest()[:16]
        with self.db:
            self.db.executescript(
                "CREATE TABLE IF NOT EXISTS conversation_stream("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1),stream_id TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS conversation_binding("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1),scope_id TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS conversation_records("
                "cursor INTEGER PRIMARY KEY AUTOINCREMENT,message_id TEXT UNIQUE NOT NULL,"
                "session_id TEXT NOT NULL,topic TEXT NOT NULL,author TEXT NOT NULL,"
                "request_key TEXT NOT NULL,record TEXT NOT NULL,offered INTEGER NOT NULL DEFAULT 0,"
                "seen INTEGER NOT NULL DEFAULT 0,UNIQUE(author,session_id,request_key));"
                "CREATE INDEX IF NOT EXISTS conversation_session_cursor "
                "ON conversation_records(session_id,cursor);"
                "CREATE TABLE IF NOT EXISTS conversation_peers("
                "peer TEXT PRIMARY KEY,stream_id TEXT NOT NULL,cursor INTEGER NOT NULL,"
                "previous_stream TEXT,gap_reason TEXT,request_id TEXT,requested_at REAL);"
                "CREATE TABLE IF NOT EXISTS conversation_retired_streams("
                "peer TEXT NOT NULL,stream_id TEXT NOT NULL,PRIMARY KEY(peer,stream_id));"
            )
            binding = self.db.execute(
                "SELECT scope_id FROM conversation_binding WHERE singleton=1"
            ).fetchone()
            if binding is None:
                if self.db.execute(
                    "SELECT 1 FROM conversation_records LIMIT 1"
                ).fetchone():
                    raise ConversationUnsupported(
                        "retained conversation scope is unbound; explicit migration is required"
                    )
                self.db.execute(
                    "INSERT INTO conversation_binding VALUES(1,?)", (swarm_id,)
                )
            elif binding[0] != swarm_id:
                raise ConversationUnsupported(
                    "conversation scope changed; use a separate node database or reopen the original scope"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO conversation_stream VALUES(1,?)",
                (secrets.token_hex(16),),
            )
        self.stream_id = self.db.execute(
            "SELECT stream_id FROM conversation_stream WHERE singleton=1"
        ).fetchone()[0]

    def high_water(self):
        return self.db.execute(
            "SELECT COALESCE(MAX(cursor),0) FROM conversation_records"
        ).fetchone()[0]

    def _insert(self, record):
        body, encoded = record["body"], canonical(record)
        rows = self.db.execute(
            "SELECT record FROM conversation_records WHERE message_id=? "
            "OR (author=? AND session_id=? AND request_key=?)",
            (
                record["message_id"],
                body["author"],
                body["session_id"],
                body["request_key"],
            ),
        ).fetchall()
        if rows:
            if any(row[0] != encoded for row in rows):
                raise ConversationError(
                    "conversation identity or request key changed its content"
                )
            return False
        self.db.execute(
            "INSERT INTO conversation_records(message_id,session_id,topic,author,request_key,record)"
            "VALUES(?,?,?,?,?,?)",
            (
                record["message_id"],
                body["session_id"],
                body["topic"],
                body["author"],
                body["request_key"],
                encoded,
            ),
        )
        return True

    def receive(self, record):
        verify_record(record, self.swarm_id)
        with self.db:
            return self._insert(record)

    def post(
        self,
        topic,
        content,
        *,
        request_key=None,
        kind="message",
        reply_to=None,
        target=None,
        references=None,
    ):
        intent = validate_intent(
            {
                "topic": topic,
                "session_id": session_id(self.swarm_id, topic),
                "kind": kind,
                "content": content,
                "reply_to": reply_to,
                "target": target,
                "references": [] if references is None else references,
                "request_key": (
                    secrets.token_hex(16) if request_key is None else request_key
                ),
                "visibility": "public",
                "swarm_id": self.swarm_id,
            },
            self.swarm_id,
        )
        with self.db:
            existing = self.db.execute(
                "SELECT record FROM conversation_records WHERE author=? AND session_id=? AND request_key=?",
                (self.author, intent["session_id"], intent["request_key"]),
            ).fetchone()
            if existing:
                record = json.loads(existing[0])
                if {key: record["body"][key] for key in INTENT_FIELDS} != intent:
                    raise ConversationError(
                        "conversation request key changed its content"
                    )
                return record
            body = intent | {
                "schema_version": 1,
                "author": self.author,
                "public_key": self.public_key,
                "timestamp": time.time(),
            }
            record = {
                "body": body,
                "message_id": digest(body),
                "signature": self.private_key.sign(canonical(body).encode()).hex(),
            }
            verify_record(record, self.swarm_id)
            self._insert(record)
        return record

    def gaps(self):
        total = self.db.execute(
            "SELECT COUNT(*) FROM conversation_peers WHERE gap_reason IS NOT NULL"
        ).fetchone()[0]
        rows = self.db.execute(
            "SELECT peer,previous_stream,stream_id,gap_reason FROM conversation_peers "
            "WHERE gap_reason IS NOT NULL ORDER BY peer LIMIT 100"
        ).fetchall()
        return {
            "count": total,
            "items": [
                dict(zip(("peer", "previous_stream", "stream_id", "reason"), row))
                for row in rows
            ],
        }

    def history(self, topic, after=0, page_limit=100, stream_id=None):
        session = session_id(self.swarm_id, topic)
        cursor(after), limit(page_limit)
        high = self.high_water()
        result = {
            "schema_version": 1,
            "session_id": session,
            "topic": topic,
            "stream_id": self.stream_id,
            "messages": [],
            "next_cursor": after,
            "has_more": False,
            "gap": None,
            "peer_gaps": self.gaps(),
        }
        if (stream_id is not None and stream_id != self.stream_id) or after > high:
            return result | {
                "next_cursor": 0,
                "gap": "local history stream changed or cursor is ahead",
            }
        rows = self.db.execute(
            "SELECT cursor,record FROM conversation_records WHERE session_id=? AND cursor>? "
            "ORDER BY cursor LIMIT ?",
            (session, after, page_limit + 1),
        ).fetchall()
        used = 0
        delivered = []
        for position, encoded in rows[:page_limit]:
            record = json.loads(encoded)
            parent = record["body"]["reply_to"]
            reply_state = "none"
            if parent:
                found = self.db.execute(
                    "SELECT session_id FROM conversation_records WHERE message_id=?",
                    (parent,),
                ).fetchone()
                reply_state = (
                    "unresolved"
                    if found is None
                    else ("resolved" if found[0] == session else "cross_session")
                )
            item = {"cursor": position, "record": record, "reply_state": reply_state}
            size = len(canonical(item).encode())
            if used + size > MAX_PAGE_BYTES:
                break
            result["messages"].append(item)
            result["next_cursor"] = position
            delivered.append(record["message_id"])
            used += size
        result["has_more"] = len(rows) > len(delivered)
        with self.db:
            self.db.executemany(
                "UPDATE conversation_records SET offered=1 WHERE message_id=?",
                ((identifier,) for identifier in delivered),
            )
        return result

    def mark_read(self, topic, through=None):
        session = session_id(self.swarm_id, topic)
        if through is None:
            through = self.high_water()
        cursor(through)
        with self.db:
            changed = self.db.execute(
                "UPDATE conversation_records SET seen=1 WHERE session_id=? AND offered=1 "
                "AND seen=0 AND cursor<=?",
                (session, through),
            ).rowcount
        unread = self.db.execute(
            "SELECT COUNT(*) FROM conversation_records WHERE session_id=? AND seen=0",
            (session,),
        ).fetchone()[0]
        return {"session_id": session, "marked": changed, "unread": unread}

    def topics(self, after="", page_limit=100, unread_only=False):
        if not isinstance(after, str) or len(after.encode()) > 256:
            raise ConversationError("invalid topic cursor")
        limit(page_limit)
        rows = self.db.execute(
            "SELECT topic,session_id,COUNT(*),SUM(1-seen) FROM conversation_records "
            "WHERE topic>? GROUP BY topic,session_id "
            + ("HAVING SUM(1-seen)>0 " if unread_only else "")
            + "ORDER BY topic LIMIT ?",
            (after, page_limit + 1),
        ).fetchall()
        return {
            "topics": [
                dict(zip(("topic", "session_id", "count", "unread"), row))
                for row in rows[:page_limit]
            ],
            "next_topic": (
                rows[page_limit - 1][0]
                if len(rows) >= page_limit
                else (rows[-1][0] if rows else after)
            ),
            "has_more": len(rows) > page_limit,
        }

    def request_page(self, peer, remote_stream, high_water, *, force=False):
        if not matches(NODE, peer) or not matches(HEX32, remote_stream):
            raise ConversationError("invalid conversation peer stream")
        cursor(high_water)
        now = time.monotonic()
        with self.db:
            row = self.db.execute(
                "SELECT stream_id,cursor,request_id,requested_at FROM conversation_peers WHERE peer=?",
                (peer,),
            ).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO conversation_peers(peer,stream_id,cursor) VALUES(?,?,0)",
                    (peer, remote_stream),
                )
                row = (remote_stream, 0, None, None)
            elif row[0] != remote_stream:
                if self.db.execute(
                    "SELECT 1 FROM conversation_retired_streams WHERE peer=? AND stream_id=?",
                    (peer, remote_stream),
                ).fetchone():
                    return None  # A delayed old hello cannot restore a superseded cursor space.
                self.db.execute(
                    "INSERT OR IGNORE INTO conversation_retired_streams VALUES(?,?)",
                    (peer, row[0]),
                )
                self.db.execute(
                    "UPDATE conversation_peers SET previous_stream=stream_id,stream_id=?,cursor=0,"
                    "gap_reason='remote_stream_reset',request_id=NULL,requested_at=NULL WHERE peer=?",
                    (remote_stream, peer),
                )
                row = (remote_stream, 0, None, None)
            if high_water < row[1]:
                self.db.execute(
                    "UPDATE conversation_peers SET gap_reason='remote_log_regressed' WHERE peer=?",
                    (peer,),
                )
                return None
            if high_water == row[1] or (
                not force and row[2] and row[3] is not None and 0 <= now - row[3] < 5
            ):
                return None
            request = secrets.token_hex(16)
            self.db.execute(
                "UPDATE conversation_peers SET request_id=?,requested_at=? WHERE peer=?",
                (request, now, peer),
            )
        return {
            "type": "chat_request",
            "target": peer,
            "request_id": request,
            "stream_id": remote_stream,
            "after": row[1],
        }

    def page(self, after):
        cursor(after)
        high = self.high_water()
        if after > high:
            raise ConversationError("remote requested a cursor beyond the retained log")
        rows = self.db.execute(
            "SELECT cursor,record FROM conversation_records WHERE cursor>? ORDER BY cursor LIMIT ?",
            (after, MAX_PAGE_RECORDS),
        ).fetchall()
        records, used, end = [], 0, after
        for position, encoded in rows:
            item = {"cursor": position, "record": json.loads(encoded)}
            size = len(canonical(item).encode())
            if used + size > MAX_PAGE_BYTES:
                break
            records.append(item)
            used, end = used + size, position
        return {
            "stream_id": self.stream_id,
            "from_cursor": after,
            "to_cursor": end,
            "high_water": high,
            "has_more": end < high,
            "records": records,
        }

    def accept_page(self, peer, payload):
        if not matches(NODE, peer) or not matches(HEX32, payload.get("stream_id")):
            raise ConversationError("invalid synchronization identity")
        if not matches(HEX32, payload.get("request_id")):
            raise ConversationError("invalid synchronization correlation")
        start, end, high = (
            cursor(payload.get(key))
            for key in ("from_cursor", "to_cursor", "high_water")
        )
        records = payload.get("records")
        if (
            end < start
            or end > high
            or type(payload.get("has_more")) is not bool
            or payload["has_more"] != (end < high)
            or not isinstance(records, list)
            or len(records) > MAX_PAGE_RECORDS
            or end - start != len(records)
            or (not records and end < high)
            or len(canonical(records).encode())
            > MAX_PAGE_BYTES + MAX_PAGE_RECORDS * 2 + 2
        ):
            raise ConversationError("incomplete or oversized synchronization page")
        identifiers = set()
        for expected, item in enumerate(records, start + 1):
            if (
                not isinstance(item, dict)
                or set(item) != {"cursor", "record"}
                or type(item["cursor"]) is not int
                or item["cursor"] != expected
            ):
                raise ConversationError("synchronization page skips a receipt cursor")
            record = verify_record(item["record"], self.swarm_id)
            if record["message_id"] in identifiers:
                raise ConversationError("synchronization page repeats a log record")
            identifiers.add(record["message_id"])
        changed = False
        with self.db:
            row = self.db.execute(
                "SELECT stream_id,cursor,request_id FROM conversation_peers WHERE peer=?",
                (peer,),
            ).fetchone()
            if row != (payload["stream_id"], start, payload["request_id"]):
                raise ConversationError("stale or unsolicited synchronization page")
            for item in records:
                changed = self._insert(item["record"]) or changed
            self.db.execute(
                "UPDATE conversation_peers SET cursor=?,request_id=NULL,requested_at=NULL WHERE peer=?",
                (end, peer),
            )
        return changed


class Conversation:
    """Small public protocol over the existing authenticated mesh transport."""

    def __init__(self, store, send):
        self.store, self.send = store, send
        self.changed = asyncio.Event()
        self.disabled_reason = None

    def disable(self, reason):
        self.disabled_reason = reason
        self.changed.set()

    def _available(self):
        if self.disabled_reason is not None:
            raise ConversationUnsupported(self.disabled_reason)

    async def announce(self, target=None):
        self._available()
        await self.send(
            {
                "type": "chat_hello",
                "target": target,
                "stream_id": self.store.stream_id,
                "high_water": self.store.high_water(),
            },
            urgency="high",
        )

    async def say(self, topic, content, **options):
        self._available()
        record = self.store.post(topic, content, **options)
        self.changed.set()
        await self.send({"type": "chat_delta", "record": record}, urgency="high")
        return {
            "message_id": record["message_id"],
            "session_id": record["body"]["session_id"],
            "author": self.store.author,
            "stored": True,
            "visibility": "public",
            "delivery": "submitted",
            "execution_authorized": False,
        }

    async def watch(self, topic, after=0, page_limit=100, stream_id=None, timeout=0):
        self._available()
        if (
            type(timeout) not in {int, float}
            or not math.isfinite(timeout)
            or not 0 <= timeout <= 30
        ):
            raise ConversationError("watch timeout must be between zero and 30 seconds")
        self.changed.clear()
        result = self.store.history(topic, after, page_limit, stream_id)
        if timeout and not result["messages"] and result["gap"] is None:
            try:
                await asyncio.wait_for(self.changed.wait(), timeout)
            except asyncio.TimeoutError:
                pass
            self._available()
            result = self.store.history(topic, after, page_limit, stream_id)
        return result

    async def handle(self, payload):
        self._available()
        origin, kind = payload.get("origin"), payload.get("type")
        if not matches(NODE, origin) or origin == self.store.author:
            raise ConversationError("invalid conversation transport author")
        fields = {
            "chat_delta": {"record"},
            "chat_hello": {"target", "stream_id", "high_water"},
            "chat_request": {"target", "stream_id", "request_id", "after"},
            "chat_page": {
                "target",
                "stream_id",
                "request_id",
                "from_cursor",
                "to_cursor",
                "high_water",
                "has_more",
                "records",
            },
        }
        if (
            not isinstance(kind, str)
            or kind not in fields
            or set(payload) - TRANSPORT_FIELDS != fields[kind]
        ):
            raise ConversationError("unexpected conversation transport fields")
        if kind != "chat_delta" and payload["target"] != self.store.author:
            if kind != "chat_hello" or payload["target"] is not None:
                return
        if kind == "chat_delta":
            if self.store.receive(payload["record"]):
                self.changed.set()
        elif kind == "chat_hello":
            request = self.store.request_page(
                origin, payload["stream_id"], payload["high_water"]
            )
            if request:
                await self.send(request, urgency="high")
        elif kind == "chat_request":
            if (
                not matches(HEX32, payload["request_id"])
                or payload["stream_id"] != self.store.stream_id
            ):
                raise ConversationError("synchronization request changed its stream")
            page = self.store.page(payload["after"])
            await self.send(
                {
                    "type": "chat_page",
                    "target": origin,
                    "request_id": payload["request_id"],
                    **page,
                },
                urgency="high",
            )
        else:
            if self.store.accept_page(origin, payload):
                self.changed.set()
            if payload["has_more"]:
                request = self.store.request_page(
                    origin, payload["stream_id"], payload["high_water"], force=True
                )
                if request:
                    await self.send(request, urgency="high")
