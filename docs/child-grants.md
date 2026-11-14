# Bounded delegated grants

The canonical managed authority can delegate one bounded level of authority and
runtime allowance under an existing root attempt. A delegated grant is neither
a separate task, an OS process relationship nor another ZPM worker.
Independently invited peers do not become subordinate grants and are not
affected by a root's cancellation.

The maintained implementation is
[`delegated_grants.py`](../src/core/collaboration/delegated_grants.py), with
`DelegatedGrantAuthority`, `DelegatedGrantNotAllocated`,
`delegated_grant_policy` and `DELEGATED_GRANT_POLICY_FIELDS`. The private HTTP
routes are installed by `install_delegated_grant_routes` in
[`delegated_grant_service.py`](../src/liminal_bridge/delegated_grant_service.py).
The old `child_grants.py` and `child_service.py` modules remain import-compatible
aliases to the same objects, including the exception type and `CAPTURES`.

The version-1 contract still uses `child` in method names, routes, fields,
identifiers, launch keys and journal records. The operational names below remain
exact; changing source names does not change profile or grant digests, persisted
records, budget accounting or replay. "Child process" describes execution
managed by OpenJules, while "delegated grant" describes Polyphony's authority.

This implements the authority portion of
[#112](https://github.com/niklas-olsson/keystone-polyphony/issues/112). Actual
child processes, broker identity, filesystem confinement and process termination
belong to OpenJules. The existing capability response retains `children: false`.
`child_grants: true` means only that this canonical API and an explicit policy
permit grants. It is not proof that a connected runtime can execute them.

## Approved policy and shared allowances

An existing root profile can explicitly add these fields:

```json
{
  "base": "0123456789012345678901234567890123456789",
  "attempt_seconds": 120,
  "capabilities": ["python", "tests"],
  "max_children": 2,
  "max_child_seconds": 30,
  "max_child_turns": 8,
  "child_capabilities": ["python"]
}
```

The authority does not insert child defaults into profiles that omit these
fields. Their approved representations and digests remain unchanged. Explicit
`max_children: 0` disables children. Enabling children requires all four fields.

- `max_children` is a **total lifetime grant limit per root**, from 0 to 64.
  Finished, cancelled and unknown children continue counting. There is no slot
  recycling in this version.
- `max_child_seconds` is positive, finite and no greater than the approved root
  duration. The actual child deadline is also capped by the original root
  deadline, including time spent waiting for a grant or launch response.
- `max_child_turns` is 1–1000. It bounds canonical turn reservations, not an
  independent model account.
- `child_capabilities` is a distinct subset of parent capabilities. The runtime
  and broker must enforce what these capabilities permit. A capability name
  never grants an owner/reviewer role or access to authority credentials.

Child seconds are additional to the root reservation in the **same task's**
`allocated_seconds`. For example, a 120-second root and two 30-second children
consume 180 seconds of the task allowance. The authority rejects a grant that
would exceed the allowance. It never refunds a finished or uncertain execution.
If the root has only ten seconds remaining when a 30-second child allocation is
approved, the child receives at most those ten seconds of runtime; the full
requested allocation remains charged. The caller can request a smaller slice.

Model calls and tokens remain within the single root allowance enforced by the
OpenJules broker. Reserving a child turn does not create another model budget.

## Host identity is required

Only the trusted runtime receives the authority credential. Guest root and child
processes must have separately authenticated broker contexts. A suitable Linux
implementation uses distinct runtime-assigned UIDs, separate Unix sockets and
verified `SO_PEERCRED`, with socket and mount permissions preventing a child from
using its parent's or sibling's connection.

The broker derives the root/child identifiers from that context. It must never
accept a guest-supplied `child_id`, role label or prompt as proof of identity.
Root contexts may request grants and manage their descendants. Child contexts
cannot self-grant, reparent, extend their limits, request parent controls or read
sibling material. All canonical operations also verify the configured runtime,
repository and root/child lineage.

The authority API is a trusted supervisor API, not a guest-facing API. Its runtime
credential intentionally permits that supervisor to recover its bound roots.
This code does not itself authenticate a process running inside Docker or prove
that socket isolation is configured correctly.

## HTTP contract

Mutations use the existing strict version-1 envelope:
`schema_version`, `deployment_id`, canonical `repository`, and `attempt_id` of
the managed root. Unsupported fields are rejected. The runtime identity is
derived from bearer authentication.

| Endpoint | Additional fields | Authorized caller |
| --- | --- | --- |
| `POST /managed/children/grant` | `request_key`, `instructions`, `seconds`, `max_turns`, `capabilities` | Bound runtime |
| `POST /managed/children/permission` | `child_id` | Bound runtime |
| `POST /managed/children/turn` | `child_id`, `request_key`, `request_digest` | Bound runtime |
| `POST /managed/children/capture` | `child_id`, `kind`, `content` | Bound runtime |
| `POST /managed/children/observe` | `child_id`, `state`, `termination_confirmed`, `launch_fenced`, `artifacts` | Bound runtime |
| `POST /managed/children/close` | `reason` | Bound runtime or authorized owner |
| `POST /managed/children/revoke` | `child_id`, `reason` | Bound runtime or authorized owner |
| `POST /managed/cancel` | `reason` | Bound runtime or authorized owner |

Recovery reads use query fields `deployment_id`, `repository`, and `attempt_id`:

- `GET /managed/children` returns this root's grants and current permission.
- `GET /managed/children/{child_id}` returns one child in that lineage.
- `GET /managed/children/{child_id}/artifacts/{hash}` returns that child's captured
  UTF-8 content after checking ownership and its hash.

These reads require the bound runtime or an authorized owner. Child instructions
and capture records are excluded from broad repository snapshots. Ordinary
ingress and reviewer credentials cannot use the private child endpoints. A hash
is an identifier, not a read capability. The parent broker must expose only the
read appropriate to its authenticated guest context.

Grant and permission responses contain:

```text
schema_version, deployment_id, repository, root_attempt_id,
child, binding, instructions, launch_allowed, remaining_seconds, reason
```

The immutable binding includes the child ID, parent/root attempt IDs, task ID,
scope, runtime principal, worker incarnation, approved profile and digest, base,
requirements revision, launch key, allocated seconds, original deadline,
capabilities, maximum turns, instructions digest and grant digest. The parent
and root IDs are identical in this one-level version and are derived by the
service. A child ID cannot be submitted as a parent attempt.

## Idempotency and revocation

The runtime persists a grant intent before calling the authority. The same root,
runtime and request key returns the original child, including after expiry,
cancellation, requirements revision, profile removal or restart. Changed input
conflicts. Only a `409` response with `code: child_not_allocated` proves that a
new request did not allocate a child. A timeout cannot establish that fact.

The returned `launch_allowed` is current permission for the original child. It
does not authorize another execution. The runtime journals one stable launch
identity and reconciles unknown outcomes rather than spawning replacements.
The original instructions remain recoverable when permission is revoked.

Turn requests include a SHA-256 `request_digest` of the runtime's canonical model
request. Identical retries return the original `turn` object plus **current**
permission. Changed requests under one key conflict. The runtime must still use
its durable model-request journal so a lost provider reply cannot result in a
duplicate model call.

Closing child admission is monotonic and prevents later grants. Already committed
grants retain their original permission, subject to OpenJules' own lifecycle
admission check before launching. During drain, the runtime closes local admission
and notifies `/managed/children/close`; it must not issue further grants. A grant
committed before close is retained for execution or safe cancellation/capture.

Root cancellation closes new admission and revokes descendants. Child revocation
affects only the named child under that root. Neither action confirms process
death or refunds time. Existing captures remain recoverable. Requirements/profile
changes, deadlines and host reboot also invalidate applicable permission.

## Captures and the root release barrier

Each child requires an immutable patch, log and JSON-object manifest, each at most
4 MB. The trusted runtime uploads these after preserving the work. The authority
stores their SHA-256 identities and verifies the actual bytes before accepting a
terminal receipt with confirmed termination and `launch_fenced: true`.

Every admitted child must reach that state before the root can confirm termination.
Unknown children, missing captures or corrupted artifacts hold the root's task
reservation. Stopping a whole container can establish descendant death, but the
runtime still must preserve and submit each child's captures. It may use truthful
empty captures for an execution it has conclusively fenced before launch.

The final root record and candidate bind the completed child capture lineage.
This is provenance, not a claim that every child patch was applied successfully.
The runtime must compose and test the actual root work product, and independent
review remains mandatory. No child receipt grants repository merge authority.

## Initial child semantic outcome

The capability response advertises `child_initial_outcome: 1`. This means the
authority validates an optional initial-instruction report in the existing final
child manifest. It does not enable a child runtime or change the approved profile
representation/digest. An OpenJules runner that explicitly enables this protocol
must check the capability before claiming readiness. Existing command profiles,
grants and manifests remain valid without it.

The original grant is the durable instruction intent. A logical work turn is
distinct from `/managed/children/turn`, which still reserves an inference request.
This first outcome is only for the original instruction and has ordinal one:

```text
work_turn_id = digest(["polyphony-child-work-v1", child_id, grant_digest, 1])
```

`digest` is SHA-256 of UTF-8 JSON with sorted keys, separators `,` and `:`, default
ASCII escaping, and no non-finite numbers. The
[interoperability vector](../tests/fixtures/child-initial-outcome-v1.json) includes
a Unicode summary and frozen digest values. It is a deterministic protocol
fixture, not a runtime receipt.

The final JSON child manifest can contain a `work_outcome` object with exactly
these fields:

```text
schema_version: 1
work_turn_id: derived SHA-256
ordinal: 1
child_id, root_attempt_id, parent_attempt_id, task_id
requirements_revision, grant_digest, instructions_digest
source: "child_report_via_bound_runtime" | "unreported"
report: null | {
  request_key, payload_digest,
  outcome: "completed" | "declined" | "needs_clarification" | "failed",
  summary
}
```

Version, ordinal and requirements revision are actual JSON integers; booleans and
floating-point aliases fail. All binding fields must equal the **original child
grant**, including its instruction digest and original requirements revision.
The report key matches `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. The summary is UTF-8,
at most 8,192 bytes, without NUL; empty text is permitted. The payload digest
covers exactly `{request_key, outcome, summary}`. Unknown fields and ambiguous
duplicate JSON fields in a manifest containing this extension fail.

An absent extension means the legacy outcome protocol is unsupported; it is not
upgraded or projected into a new receipt. An enabled runtime with no retained
callback supplies `report: null` and `source: "unreported"`. A non-null report
requires `source: "child_report_via_bound_runtime"`. A present null or malformed
`work_outcome` is invalid, not a request to fall back to legacy behavior.

The child chooses its semantic outcome. `completed` is a child self-report,
separate from test results, composition, termination, independent review, task
acceptance and publication. A decline or clarification does not discard partial
work. An exit code, stdout message or empty patch cannot manufacture a report.

### Commitment and recovery

The runtime authenticates the callback through the child's bound UID/socket,
derives its grant fields and retains one immutable report before acknowledging
it locally. It must serialize that retention against final manifest freezing:
after freeze, only an exact already-retained retry can succeed. Listener closure
alone does not fence an in-flight callback. This runtime behavior belongs to
OpenJules and needs its own qualification.

The existing `/managed/children/capture` route validates a present extension
**before** it can occupy the immutable manifest slot. Bad new input can therefore
be corrected without poisoning that slot. Once committed, changed bytes conflict,
including a changed report under a new request key. The service revalidates the
stored extension and capture hashes at terminal observation and when the root
consumes the child lineage. Corrupt or invalid historical evidence fails closed
and remains retained; the service does not rewrite it.

Only after verified terminal capture does the child record acquire
`initial_outcome`. It contains the validated extension plus:

```text
artifacts: {patch: final_hash, log: final_hash, manifest: final_hash}
canonical_committed: true
release_authorized: false
```

The projection and terminal child update commit in the same transaction.
Identical upload/observation retries recover the same bytes and record, without
allocating execution, reserving inference or refunding time. A runtime may expose
a locally retained report earlier only as provisional and not canonically
committed. The authority does not treat that early view as a terminal receipt.

Historical evidence delivery and authorized reads remain possible after expiry,
revocation, requirements revision or profile removal. They validate the original
binding and cannot restore permission for new effects. The existing bounded
single-child and protected artifact reads recover the outcome; a hash alone is
not access. Broad snapshots and public conversation omit the protected summary.
The root/candidate lineage retains only its existing final manifest reference.

This final-only outcome does not provide a second live instruction, parent agent
process replay, a general inbox, or an automatic public chat bridge. Two-round
managed conversation requires a separately approved persistent session and
intermediate receipt contract under the same grant, deadlines and shared limits.

## Verification boundary

The child suite covers independent SQLite connections racing against count and
time limits, changed/replayed requests after revocation, monotonic expiry, original
deadlines, narrowed capabilities, turn exhaustion, HTTP role/lineage checks,
corrupted captures, root termination barriers and candidate provenance. Existing
managed root behavior and old profile digests remain covered.

These are authority and HTTP tests. Actual guest UID/socket isolation, subprocess
supervision, shared model allowances and end-to-end child execution require the
OpenJules integration gate before child runtime readiness can be advertised.

`tests/test_child_outcomes.py` additionally covers the schema and shared digest
vector, pre-insertion rejection, competing capture writers, restart/retry,
historical delivery, durable corruption, root/candidate barriers and actual HTTP
read scope. It does not establish a real child callback, Docker confinement,
live model behavior, installed three-service interoperability or forge approval.
