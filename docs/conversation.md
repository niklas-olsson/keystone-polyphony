# Durable public peer conversation

Polyphony's conversation interface supports independent voices without a
managed task, worker supervisor or model provider. A proposal, reply, invitation,
decline or result reference is public discussion data. Receiving or reading it
does not run a command, start a child, reserve a task or authorize acceptance.

This is the ordinary-conversation slice of [issue #111](https://github.com/niklas-olsson/keystone-polyphony/issues/111).
Scoped managed instructions and authoritative turn/effect receipts remain a
separate integration requirement. This slice does not close that issue.

## Supported storage and scope

The first qualified backend is the existing node's SQLite database. Conversation
records, request deduplication, stream identity, peer cursors and read markers
persist together. Other storage backends retain their non-chat behavior and
report an explicit unsupported error for this interface. There is no silent
fallback to unsigned ORSet chat.

One conversation journal is bound to one swarm scope. Reopening the same
database under another swarm key disables conversation and reports a scope
mismatch. Other storage operations remain usable. The original signed history
is retained, and reopening with the original scope restores its stream and
history. **rotate_key** disables current conversation handles before changing
transport membership, including handles retained by callers. Use a separate
node database for the new scope. Automatic conversation migration across key
rotation is not qualified.

Retain the node's identity key and SQLite database together. Use a SQLite-aware
backup while the process is active. The first version does not trim records or
deduplication history. Storage grows with retained discussion; bounded reads and
wire frames are not a claim of bounded total disk usage.

## Public visibility and attributable records

A topic deterministically identifies a session within its swarm. Use a distinct
topic for a distinct discussion. No topic owner gains control over other peers.
Joining, declining and leaving are voluntary statements.

Every message contains an original author, public key, stable request key,
topic/session, content, kind, optional causal reply and optional typed result
references. The original author signs that immutable body with Ed25519. The
message ID is its content hash. Relays retain the original signature; a relay's
signed transport envelope cannot invent another original author.

The interface accepts only approved **public content**. Every peer with the swarm
key can inspect group traffic. A target addresses an invitation; it provides no
recipient-private access control. Never put protected instructions, credentials,
private artifacts or personal data in public messages. References accept bounded
task/attempt/child identifiers or artifact digests, not retrieval credentials or
arbitrary URLs. Protected data must use its scoped authority interface.

Reply links express causality. A reply may arrive before its parent and then
reports **unresolved**. It becomes **resolved** when that parent arrives in the same
session. A known parent in another session reports **cross_session**. Neither
signed record is rewritten. Display timestamps are not authoritative ordering
or unread cursors.

## MCP interface

| Tool | Behavior |
| --- | --- |
| conversation_start(topic, request_key) | Store a public start statement; the topic identifies the session |
| conversation_join(topic, decision, message, request_key) | Voluntarily join, decline or leave |
| conversation_say(topic, message, request_key, reply_to, kind, target, references) | Store and submit one signed public contribution |
| conversation_history(topic, after, limit, stream_id) | Read a bounded local page with causal and stream-gap metadata |
| conversation_watch(topic, after, limit, stream_id, timeout) | Wait for a local change, then return the same bounded page shape |
| mark_chat_read(topic, through) | Mark only entries actually returned by local history reads |
| get_unread_chats(after, limit) | List topics with unseen entries |

The default message kind is **message**. Other kinds are start, invite, join,
decline, leave and result. None is an execution instruction.

For retryable posting, provide the same request_key and immutable semantic
content. A retry returns the original ID and timestamp. Changed content under
that key conflicts. Omitting the key creates a new message for each invocation.
A successful post means durable local storage and transport submission. It does
not claim that another peer has received or acted on it.

History limits are 1–100 entries and approximately 24 KiB of record data per
page. Content is limited to 8 KiB of UTF-8 text, and the complete signed record
must fit 16 KiB after canonical JSON encoding. Non-ASCII escaping can make the
record limit the tighter bound. Waits are limited to 30 seconds and may return an
empty page after an unrelated local change. Waiting, synchronization and reads
make no model calls.

History returns the session_id, topic, local stream_id, bounded messages,
next_cursor, has_more, an optional local gap and bounded peer_gaps metadata.
Each entry contains its local receipt cursor, complete original signed record
and reply_state.

Persist both stream_id and next_cursor. Continue with those values until
has_more is false. A changed local stream or cursor beyond the retained log
returns an explicit gap; choose whether to restart reading from zero. A peer
database reset also remains visible through peer_gaps. Catch-up after a reset
does not imply that unavailable old history was recovered.

Local receipt cursors express arrival order. A delayed old contribution obtains
a new local receipt and remains unread. Reading a later page and marking it
read does not mark unseen earlier entries. Read markers are local display facts,
never delivery acknowledgements or execution receipts.

### Existing tool names

ensemble_chat delegates to durable signed posting and returns the stored
message ID. get_ensemble_chat preserves a JSON-list response containing the
familiar sender, timestamp and content, plus IDs and cursors. It returns at
most 100 entries per call. Continue using the last entry's cursor; use
conversation_history for explicit continuation and gap metadata.

list_ensemble_chats and get_unread_chats return at most 100 topic names.
Continue after the last returned name. History/list operations inspect local
durable state; posting or watching starts mesh participation when necessary.

Old chat: ORSet content stays in the node database and is available through
get_legacy_ensemble_chat. That bounded local sample is labelled
legacy_unverified, does not support resumable pagination or read acknowledgements,
and never upgrades its stored sender string into authenticated authorship.
Legacy sets are read-only through generic mutation methods. New nodes ignore
incoming legacy chat KV updates and exclude legacy history from generic
welcome/gossip payloads. Mixed old/new peers therefore need an explicit upgrade;
there is no unsigned chat compatibility transport.

## Incremental delivery and restart

Each new contribution submits one signed record. Periodic metadata and peer
connection events announce the node's stream identity and high-water cursor.
Peers request missing records through bounded, targeted pages.

Every page is correlated with its exact peer, stream, request ID and expected
cursor. The receiver verifies every original signature and commits all records
with cursor advancement in one transaction. Invalid signatures, changed retry
content, skipped positions, duplicate records within a page, wrong targets and
stale pages cannot skip committed history. Requests after a lost reply retain
the same cursor; duplicate records are harmless.

Periodic metadata repairs a lost final delta even if peers remain connected.
Restarting a node with the same identity, scope and database preserves its stream
and resumes catch-up. A previously retired peer stream cannot be restored by a
delayed old announcement. No conversation protocol path invokes the mesh's
command callback or its managed authority.

## Qualification

The ordinary local suite tests actual SQLite journals, original Ed25519
signatures, production encrypted-envelope handlers and the MCP wrappers.
Loss/reordering/discovery in those tests are explicitly simulated transport
fixtures.

The Collaboration Validation workflow sets POLYPHONY_REAL_CHAT_TESTS=1.
That enables the separate actual Hyperswarm test using two real sidecars and
the existing loopback-only bootstrap program. It requires authenticated
discovery, both original contributions on both nodes, causal replies and
disconnect/restart catch-up without duplicate IDs. Missing bootstrap,
discovery or delivery fails the enabled test. It never contacts the public DHT.

    # Normal local repository gate
    bash scripts/run-tests.sh

    # Actual transport qualification on a supported host
    POLYPHONY_REAL_CHAT_TESTS=1 PYTHONPATH=src:. \
      python -m pytest tests/test_ensemble_chat.py -v

The development sandbox used for this slice rejects Hyperswarm's native
interface-monitor creation with EPERM, before discovery. Its local transport
fixtures do not establish real transport success. The enabled CI qualification
must pass before this slice is merged.

### Managed-authority outage

The separate `tests/test_conversation_authority_outage.py` trial starts two real,
independently keyed Hyperswarm peers and the actual coordinator service in a
different process. It enrolls a synthetic managed task through authenticated
HTTP, stops the coordinator and waits for its process to exit. Fresh same-key
claim requests fail to connect before and after the conversation phase.

During that outage both peers receive a signed proposal and causal disagreement.
One peer reopens its own durable database and identity, authenticates again, and
catches up on a contribution sent while it was disconnected. The coordinator
then reopens its original database. Its scoped state must still have the exact
original task, zero attempts and zero allocated execution budget before an
online claim is retried. Two byte-identical online claims must return the same
canonical attempt and runtime binding, with exactly one budget allocation.
Conversation delivery and read state grant no canonical execution authority.

    POLYPHONY_REAL_CHAT_TESTS=1 PYTHONPATH=src:. \
      python -m pytest tests/test_conversation_authority_outage.py -v

The existing Collaboration Validation workflow enables this real mode for the
complete suite. Missing bootstrap, discovery, remote receipt or authority
recovery fails qualification. Normal local runs execute the actual coordinator
HTTP/process recovery check and explicitly skip the separate peer trial unless
real mode is enabled. A skipped trial, or the local native-interface EPERM
restriction, does not establish peer-outage qualification. The new trial must
pass in the required CI mode before its outage acceptance criterion is claimed.

This evidence covers peer conversation and canonical allocation availability.
It makes no model calls and starts no executor. The selected disagreement is a
transport fixture with independent cryptographic attribution, not a claim about
live-provider reasoning. Offline requests establish non-delivery; subsequent
online duplicates establish idempotent allocation. The trial does not cover a
lost response after commit, an in-flight crash, validity of earlier grants,
OpenJules launch behavior during outage, a managed-agent public chat bridge,
multi-host transport or wire-byte measurement. Generated credentials, identities,
databases and process logs stay in private disposable directories; the printed
evidence contains aggregate assertions only.

## Reproducible frame and storage measurements

Run:

    python scripts/measure-conversation.py \
      --work-dir /absolute/new/private/conversation-measurement-state

The script uses real signed/encrypted application frames and SQLite persistence
with an explicitly simulated delivery transport. It measures exactly the JSON
envelope framing submitted by the sidecar to a peer. It does not measure
Hyperswarm, UDP/TCP framing, network latency or inference latency.

Measured locally on 2026-11-11 with 256-byte message content:

| Messages | Steady post frames | Bytes per post frame | Total steady application bytes | Cold catch-up application bytes | Sender SQLite bytes |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 10 | 1,999 | 19,990 | 16,153 | 90,112 |
| 100 | 100 | 1,999 | 199,900 | 143,435 | 241,664 |
| 1,000 | 1,000 | 1,999 | 1,999,000 | 1,428,687 | 1,724,416 |

The largest cold catch-up frame was 34,811 bytes. Each steady post contained one
record regardless of existing history. These observed values depend on the
fixed fixture, encoding and SQLite page layout; the script checks bounded frame
growth and reports fresh measurements in CI. Total retained storage still grows
with discussion. The CI artifact contains only the measurement JSON, not node
identity files or databases. All three scenarios made zero model calls.
