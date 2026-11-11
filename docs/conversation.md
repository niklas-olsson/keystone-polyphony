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

### Actual loopback traffic and reopened storage

The separate real measurement command uses two independently keyed peers, the
actual Hyperswarm sidecars and explicitly loopback-bound DHT sockets, including
the test bootstrap. A loopback bootstrap address alone does not constrain a
wildcard-bound peer's interface advertisement. The pinned HyperDHT instead
advertises only loopback when its sockets are bound to `127.0.0.1`.
This command requires a working Linux `tcpdump` capture and the pinned sidecar
dependencies:

```sh
npm ci --prefix src/liminal_bridge/sidecar
python scripts/measure-conversation-real.py \
  --work-dir /absolute/new/private/conversation-real-measurement
```

On an authorized host where only the capture executable needs elevated access,
add `--sudo-capture`. The GitHub-hosted CI step explicitly installs tcpdump and
uses this option. It does not elevate the Python harness, Node peers or local
development sandbox. Capture is restricted to the loopback interface and the
inventoried test UDP endpoints, including a separate calibration pair.

The command temporarily enables both `POLYPHONY_LOCAL_MEASUREMENT=1` and
`POLYPHONY_MEASUREMENT_LOOPBACK=1` for each scenario, restoring their exact prior
values even when directory, log or bootstrap setup fails. Diagnostics alone
retain ordinary networking. The separate loopback flag requires diagnostics
and exactly one explicit `127.0.0.1:<port>` bootstrap, with a decimal port from
1 through 65535; malformed opt-ins fail before transport construction.
The opt-in injects the pinned HyperDHT with `host: '127.0.0.1'` and `nodes: []`
into Hyperswarm. Empty known nodes also exclude ambient Pear configuration.
Swarm identity and destruction ownership remain unchanged, as do ordinary
sidecar networking, authentication, endpoint checks, gossip and time limits.
No additional namespace privilege or general bind-address API is introduced.

Each fresh scenario posts 10, 100 or 1,000 messages with fixed 256-byte content
and equal-width request keys. Discovery and initial synchronization precede the
steady window. The harness requires exact remote message IDs through bounded
history pages and reopens both identities/databases to verify the retained IDs.
There is no delivery fixture, provider configuration or inference. This command
does not measure cold discovery, catch-up, managed child sessions or model
latency. Its timing includes observation, SQLite work and configured settling;
it is not a throughput or latency benchmark.

The report keeps three accounting layers separate:

| Layer | Values | Interpretation |
| --- | --- | --- |
| Actual application stream | Exact UTF-8 JSON-plus-newline bytes submitted to each `conn.write`, frame counts and interval min/max; received buffer bytes; backpressure and errors | Submissions and observations, not remote durable acknowledgements. Aggregate frames may include gossip and synchronization. |
| Native transport | Separate per-stream and per-instance byte/packet deltas, retransmit and kernel-drop counters where available | Pinned library accounting. The scopes overlap. Unsupported counters remain null with a reason. |
| Captured loopback packets | Packet count, original virtual frame length, IP total length, UDP length and UDP payload length for each direction | Actual capture from one virtual interface. Retransmissions remain counted. These are not physical Ethernet or WAN byte costs. |

The installed `udx-native` CMake file records the native source revision. At the
current pin, [`libudx` accounting](https://github.com/holepunchto/libudx/blob/25b063b/src/udx.c)
uses UDP payload lengths and can advance transmit counters after non-EAGAIN
send errors. Native transmit counts therefore cannot substitute for captured
packets or successful delivery. Instance counts also include transport/control
traffic outside the application stream. Do not add stream and instance totals.
The 16-bit stream retransmit delta is reported only when the known stream
transmitted-packet delta is below 65,536, which rules out an invisible full wrap.
Otherwise that metric is null with an explicit wrap-ambiguity reason.

Bridge diagnostics require the explicit `POLYPHONY_LOCAL_MEASUREMENT=1` process
option and a fixed local stdin request. They are disabled in ordinary operation,
never sent to peers and expose only bounded counters and endpoint metadata.
They contain no messages, keys or credentials. A local window reset makes the
frame extrema refer to this corpus; the native counters remain cumulative and
are differenced separately. The packet window encloses both local diagnostic
snapshots, which are not a globally atomic observation.

Two known-size UDP datagrams calibrate one captured record in each direction.
The parser reads one bounded classic-PCAP Ethernet/IPv4 UDP capture and rejects
unknown formats, invalid lengths, fragmentation, truncation, un-attributed
endpoints, missing final capture statistics or reported drops. It counts packet
records rather than tcpdump's platform-dependent received-by-filter statistic.
It never removes identical packets to deduplicate presumed loopback copies.
Missing native transport, capture permission or attributable peer packets fails
qualification; there is no successful fixture fallback.

Storage measurements include baseline, quiescent and closed main databases,
journal/WAL/SHM presence and file sizes, allocated file blocks when available,
and SQLite journal mode, page size/count and freelist. The harness preserves
the configured journal mode. These files include retained node/protocol state,
not solely message text, and they continue growing with retained history.

The command bounds each corpus and all cleanup, limits raw capture to 64 MiB
and 200,000 packets, and keeps the overall existing 30-minute CI ceiling. The
real command has a ten-minute overall budget. Version information, observed
durations, gossip/settling settings and zero model calls are included in the
aggregate report. No inferred idle-traffic subtraction changes the totals.

The private work directory contains generated identities, databases and logs.
Only `report.json` and a content-free failure `diagnostics.json` are retained as
CI artifacts. Raw capture is removed after parsing or failure; do not upload the
private directory. Local parser/counter fixtures are labelled unit evidence.

Failure diagnostics preserve the harness's fixed capture-readiness,
authenticated-peer-discovery and durable-message-delivery timeout messages.
Malformed local diagnostic responses and forced subprocess cleanup also have
fixed messages. Unrecognized descriptions and arbitrary exception text remain
excluded from the exported artifact. These messages identify failed checks;
they do not establish a transport root cause or qualify numerical measurements.
Actual measurements and the corresponding issue #111 criterion require the
real CI command to pass on the reviewed revision. A new command alone is not
measured wire evidence or independent PR approval.
