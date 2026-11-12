# Durable collaboration prototype

Issue [#95](https://github.com/niklas-olsson/keystone-polyphony/issues/95) adds an
opt-in single-host service alongside the existing mesh. Workers choose tasks;
the conductor advises; the coordinator checks protected operations. Existing
exploratory tasks keep their advisory mesh workflow.

The implementation uses Python's SQLite driver and existing aiohttp dependency.
Run all SQLite clients on one host, on a local filesystem. Remote workers use
the authenticated HTTP interface through an SSH tunnel. Never share the WAL
files over a network filesystem. The default listener is loopback only.

## Authority and experimental modules

The durable collaboration protocol is organized into:

- **Core Protocol & Authority (`src/core/collaboration/`)**:
  - `authority.py`: Single-host durable SQLite WAL authority (`Coordinator`), expected-fence CAS, attempt reservations, and candidate admission.
  - `models.py`: Formal typed schemas (`TaskObjective`, `AssignmentClaim`, `ReviewReceipt`, `ReviewVerdict`, etc.).
  - `ledger.py`: Experimental `FencedLedger` implementing its own dual-budget and fence tracking. It is not the canonical managed authority.
  - `admission.py`: `ExperimentalReceiptExporter` inspects local FencedLedger acceptance without producing GitHub review evidence or managed admission.
  - `conductor.py`: `EnsembleConductor` providing non-blocking advisory nudges and worker pivot guidance.
  - `adapter.py` & `git_worktree_adapter.py`: Portable execution contracts, worktree lifecycle, diff extraction, and command execution.
- **Service & Transport Layer (`src/liminal_bridge/`)**:
  - `coordinator_service.py`: Authenticated HTTP daemon exposing the authority to remote nodes over SSH tunnels.
  - `server.py`: FastMCP server exposing ordinary peer collaboration and experimental FencedLedger tools (`create_enrolled_task`, `claim_enrolled_task`, etc.). Those experimental tools do not use the authenticated canonical service.
  - `coordinator.py`: Backward-compatible re-export module routing callers to `src.core.collaboration.authority`.

### Experimental inspection and compatibility

`scripts/task.py` and its `./polyphony task` wrapper operate on the
experimental FencedLedger. Their caller-supplied actor and reviewer labels do
not prove authenticated identity. A local candidate identifier is not a Git
commit SHA. Use the [authenticated managed runtime interface](managed-runtime.md)
for supported managed enrollment and execution. The managed CLI implements
enrollment only; unsupported legacy operations refuse managed tasks instead of
silently translating caller labels into service credentials.

New experimental CLI tasks default to `experimental_...` IDs. Explicit existing
nonreserved IDs still work. The experimental ledger, its coordinator/conductor,
and the four enrolled-task MCP mutators reject every `task_v2_` ID before task,
assignment, budget, adapter, review or host-signal effects. A historical row with
that prefix remains inspection-only even if it has an accepted candidate or a
retained execution handle. No rows are migrated, renamed or deleted. The
experimental ledger's own `schema_version: 2` remains its existing local format;
it does not turn an ordinary experimental task into a canonical task.

Advisory mesh task claims and completion, including raw `task:ID` batons, use the
same refusal across MCP, dashboard, baton CLI, peer packets and autonomous task
selection. In `swarm_backlog`, any matching copy with a managed marker or invalid
explicit schema marker blocks the ID. Only an absent marker or exact integer
`1` permits the legacy advisory path. This test considers all duplicate records
and still applies when the authority is offline. New managed metadata discards
only the ephemeral advisory lock and its pending claim; backlog and conversation
history remain. Ordinary file locks and unmarked local tasks retain their
advisory behavior. These checks prevent misleading parallel claims; they do not
claim that a mesh baton ever bypassed the authenticated canonical service.

The execution-host MCP outbox excludes historical signals whose stored targets
contain reserved IDs or malformed target data. Such signals cannot be
acknowledged or marked fulfilled. Direct ledger polling and
`scripts/conductor.py poll-signals` remain historical inspection, with no
execution authority. Ordinary experimental host signals keep their existing
poll/acknowledgement flow. This compatibility change adds no service, provider
call, credential, authority ledger or managed HTTP schema. Integrated acceptance
and exact tested component revisions remain in
[#109](https://github.com/niklas-olsson/keystone-polyphony/issues/109) and
[ZPM #3](https://github.com/niklas-olsson/zero-point-modulator/issues/3).

Inspect an existing locally accepted candidate with:

```sh
./polyphony task --db /path/to/existing-ledger.db local-receipt --task-id TASK
```

The receipt has `receipt_type: polyphony-experimental-local`,
`managed_authority: false`, `release_authorized: false`, and
`identity_source: caller-supplied`. It preserves the local task, candidate,
baseline reference, revision and all still-valid local review records, including
dissent. Local acceptance follows the experimental ledger's policy; this export
does not establish independent review or add an acceptance policy. A held task
cannot export a prior accepted pointer as current local acceptance.

No historical ledger records are migrated or deleted. Read-only ledger methods
remain available to inspect held or unaccepted work. `ExperimentalReceiptExporter`
provides `emit_local_receipt(task_id)` and `write_local_receipt(task_id)`; the
default filename is `experimental-receipt.json`. The old `AdmissionHandoffEmitter`
class name remains an alias. Its `emit_review_evidence` and `write_evidence_file`
methods are deprecated aliases with the same local-only output. Passing
`pr_number`, `workflow_path`, `run_id` or `run_attempt` now fails explicitly.
Writing to the reserved `review-evidence.json` filename also fails without
overwriting the file. This filename check prevents an accidental mix-up; it is
not an access-control boundary.

The CLI's old `evidence` action is a deprecated alias that prints a notice to
stderr and the local receipt to stdout. Its old `--pr` option fails instead of
claiming PR provenance. Consumers of the former forge-shaped output must migrate
to local receipt fields. Previously exported files are historical experimental
data and cannot establish forge approval.

The authentic `scripts/write-review-evidence.py` workflow emitter and the
[decision admission gate](decision-merge.md) are unchanged. Genuine PR evidence
still requires the actual repository, head/base revisions, workflow run, review
and CI provenance. The gate rejects an experimental local receipt even when the
other PR readiness conditions pass. No previous successful bypass is claimed.

## Decisions and boundaries

Upgrading the CRDT batons cannot make destination writes atomic. A distributed
workflow platform would add deployment machinery beyond this prototype. The
selected design keeps a separate SQLite WAL authority and Git worktree adapter.
The [issue discussion](https://github.com/niklas-olsson/keystone-polyphony/issues/95#issuecomment-6728727526)
records alternatives and pass limits posted before implementation.

| Concern | Prototype rule |
| --- | --- |
| Worker choice | `select_task` executes in the worker. Capability, dependency, task-wide budgets, active executions and busy resources filter eligible work. |
| Fairness | Order by creation time with at most 120 seconds of priority advantage. New high-priority tasks cannot indefinitely displace older eligible work. |
| Intent | Durable attempt records announce intent; they do not authorize shared writes. Mesh `swarm_backlog`, `chat:*`, and thoughts remain advisory. |
| Protected destination | `write_resource` writes the coordinator's SQLite destination record. Lease ownership, deadline, fence, effect and retry receipt commit in one transaction. |
| External effects | Arbitrary merge, deployment, filesystem replacement, and provider requests are outside this destination contract. Add destination-side atomic fencing, idempotency and unknown-outcome reconciliation before enrolling such effects. |
| Exclusivity | Acquisition uses expected-fence CAS. Every new lease increments the fence. The acquisition CAS tuple also identifies retries and returns the original lease, even after expiry. A different duration with that tuple is rejected. |
| Retry identity | Attempt start keys and resource write keys are bound to authenticated worker plus payload. Lost responses return the committed result; changing the payload is rejected. |
| Permissions | Owner enrolls task requirements, checks, dependencies, reviewers, capabilities, and resource. Workers cannot change them. Only owner-authorized revisions change acceptance criteria. |
| Reviewer independence | Owner provisions credentials for distinct real principals. The HTTP service injects authenticated identity; two tokens for the same principal cannot review that principal's work. Owner must not register one person as two principals. |
| Cost | Unknown. Wall-time allocations and attempt counts are bounded; this is not a monetary billing limit. |

`Coordinator` is a trusted in-process authority API. Its methods must never be
exposed directly to untrusted callers. `coordinator_service` restricts operations
by role and binds identity before calling it. All workers execute as trusted
processes on the host: a worktree separates working files, **not host access**.
Do not treat bearer credentials or this adapter as a sandbox against malicious
processes that can read the owner's database, credentials, or other worktrees.

## Authority and execution boundaries

| Module | Owns | Failure boundary |
| --- | --- | --- |
| Coordinator | Requirements, task-wide allocations, dependency receipts, artifact bytes, fenced destination effects and admission | SQLite transactions decide authority; elapsed time never proves process death. |
| Service | Authenticated principals, one launch claim and the capture barrier | Polling/cancellation cannot publish a completed reservation past failed or unfinished capture. |
| Worktree adapter | Git preparation, live child/group ownership and patch extraction | A process exit is a local observation; it cannot release authority before its bytes are saved. |
| Worker | Capability-based task choice and bounded retries | It cannot supply host commands, revise requirements, or review its own candidate. |

The service phases below use existing attempt records; they introduce no new
persisted state machine or destructive database migration.

```mermaid
sequenceDiagram
    participant W as Worker
    participant S as Service
    participant A as Authority
    participant G as Git adapter
    W->>A: Start attempt; reserve task budget
    W->>S: Execute attempt
    S->>A: Persist one launch claim (unknown)
    S->>G: Prepare; recheck permission; spawn
    G-->>S: Confirm process-group termination
    S->>A: Commit patch bytes
    alt Artifact committed
        S->>A: Candidate; publish terminal observation
    else Capture or launch outcome failed
        S->>A: Record problem; keep execution reserved
        Note over W,A: Owner must salvage work and verify termination before recovery
    end
```

A failure while extracting or saving a patch retains the execution reservation,
even if the adapter knows the process has exited. Snapshot polling, repeated
cancellation and service restart cannot turn that observation into a fresh task
allocation. The worktree stays available for deliberate salvage. The owner must
save a durable artifact receipt and independently verify termination before using
`observe` to release the reservation. If a launch fails after spawning a child,
its exit alone cannot bypass this barrier either. Explicit `not_started` proof
allows release when no worker ever ran. There is no automatic capture retry or
new inference on this recovery path. Candidate creation may fail after bytes
are committed (for example, requirements were revoked); those bytes remain
recoverable and a verified terminal observation is then safe.

## Running the service and workers

Install `requirements.txt` and Git. The hardened worktree adapter requires
Linux procfs visibility and POSIX `waitid(WNOWAIT)` process-group ownership.
From the repository root, generate local credentials and an approved execution
template. A launch-enabled service validates these OS interfaces at startup;
an unsupported host fails before serving workers or allocating their budgets.
Run the service without `--execution-config` to inspect/review/recover durable
data without a local execution adapter.

```bash
mkdir -p .liminal
python3 - <<'PY'
import json, os, secrets, shutil
from pathlib import Path
root = Path('.liminal').resolve()
credentials = {}
for identity, role in [('owner', 'owner'), ('alice', 'worker'), ('bob', 'worker'),
                       ('reviewer', 'reviewer'), ('conductor', 'conductor')]:
    token = secrets.token_hex(32)
    credentials[token] = {'identity': identity, 'role': role}
    path = root / f'{identity}.token'
    path.write_text(token)
    path.chmod(0o600)
path = root / 'credentials.json'
path.write_text(json.dumps(credentials))
path.chmod(0o600)
config = {
    'repository': str(Path('.').resolve()),
    'root': str(root / 'worktrees'),
    'base': 'HEAD',  # owner-approved baseline, resolved to a commit at launch
    'command': [shutil.which('opencode'), 'run', '--pure', '-m',
                'openrouter/nvidia/nemotron-3-ultra-550b-a55b:free'],
    'inherit_env': ['PATH', 'OPENROUTER_API_KEY'],
    'isolated_home': True,
}
(root / 'execution.json').write_text(json.dumps(config))
PY
```

Use an immutable commit for `base` when the baseline must stay fixed across
attempts. Set the API key in the service environment; it is not a database
record. Credentials, logs, execution configuration and worktrees live under the
ignored `.liminal/` directory. The service command is owner-selected argv, with
no shell expansion and no worker-selected executable or environment. By default
only PATH is inherited; explicitly approve additional environment entries.

```bash
python3 -m src.liminal_bridge.coordinator_service \
  --credentials .liminal/credentials.json --execution-config .liminal/execution.json
```

From another terminal, enroll a task through the owner's credential. The return
value has the new `task_v2_...` ID:

```bash
python3 - <<'PY'
import asyncio
from pathlib import Path
from src.liminal_bridge.coordinator_service import CoordinatorClient
async def main():
    client = CoordinatorClient('http://127.0.0.1:8766', Path('.liminal/owner.token').read_text())
    task = await client.call('enroll', title='Example',
        requirements='Implement the agreed example in the worktree. Do not commit.',
        checks=['tests', 'independent-review'], reviewers=['reviewer'])
    print(task['id'])
asyncio.run(main())
PY
POLYPHONY_WORKER_TOKEN="$(cat .liminal/alice.token)" \
  python3 -m src.liminal_bridge.worker --identity alice
```

With `isolated_home: true`, each attempt receives private HOME and XDG state
directories, and a shared `OPENCODE_DB` override is removed. This avoids OpenCode
first-run migrations competing over a shared session DB.
Use explicit approved configuration if the CLI needs additional settings.

Run a second worker using `bob.token` and `--identity bob`. Identity must match
the owner-provisioned principal. Tasks have three attempts and 900 seconds of
allocated command time by default. The worker default allocates 60 seconds per
attempt. Replacement workers consume the same task budget. Allocated time is
not refunded, including failed launches; increase budgets only through a future
explicit owner policy, never by silently restarting a task.

## Service interface

All routes require `Authorization: Bearer <token>`. Requests and records contain
approved public task data. Keep raw private material out of titles, requirements,
problem causes, work products, evidence and advice.

| Endpoint | Access and behavior |
| --- | --- |
| `GET /snapshot` | Authenticated readers; tasks, attempts, resources, candidates, work receipts, observations, advice, responses and durable event cursor. |
| `GET /events?after=N&timeout=30` | Wait for a committed sequence change, with at most 30 seconds before a recovery check. Service checks local storage every 100 ms; waiting makes no model calls. |
| `GET /artifacts/{sha256}` | Retrieve committed approved work, even after worker removal. |
| `POST /call/enroll`, `/revise` | Owner; fix or explicitly revise requirements and checks. Revision changes invalidate old admission. |
| `POST /call/start_attempt` | Worker supplies selected `task_id`, capabilities, seconds, retry key. Service validates limits; it never chooses a task. |
| `POST /execute` | Worker supplies its `attempt_id`. Service launches the owner-approved template once. Repeated calls observe the original launch. Unknown outcomes require reconciliation. |
| `POST /cancel` | Worker requests its own attempt stop. Reserved attempts are atomically confirmed unlaunched; dispatched executions retain reservations until adapter confirmation. Repeated cancellation never signals a reaped PID. |
| `POST /call/acquire`, `/write_resource` | Worker; require live attempt permission and task resource authorization. |
| `POST /call/save_work`, `/candidate`, `/problem` | Worker; salvage and record progress. An expired attempt can still save work. It cannot write a protected destination. |
| `POST /call/evidence` | Reviewer; candidate ID, check, boolean result, exact input revision, and `request_key` for review changes. Keys bind the complete payload and replay the original receipt. Source identity/time are service-supplied. Historical observations remain durable; the latest distinct observation per reviewer/check applies. |
| `POST /call/accept`, `/admission` | Owner; acceptance requires all configured checks, authorized independent review, committed artifact, and current requirements revision. Returns a durable admission packet. |
| `POST /call/advise` | Conductor; fixed 60-second minimum interval per task, 300-second expiry. No enrollment, requirements edits, execution, or acceptance rights. |
| `POST /call/respond` | Worker records follow/decline/defer plus cause against unexpired, current advice. |
| `POST /call/observe` | Trusted owner recovery only; confirm a terminated execution after independently verifying it. Never infer death from elapsed time or a cancellation request. |

Call payloads correspond to method arguments in `coordinator.py`, excluding
`worker`, `reviewer` and `source`, which the service injects. A 409 is an unmet
precondition. Observe new state, select other work or wait; do not repeatedly
call a model on a busy resource. `Worker.tick()` makes one selection and
`Worker.run()` waits on events or a bounded outage recovery delay.
`advise_from_problems(client)` is the optional non-coding conductor entry point.

## Saved work, completion and repository admission

The store commits UTF-8 patch bytes (including Git binary patch text) in SQLite
before acknowledging their SHA-256 receipt. The 4 MB prototype limit is checked
before saving. Candidate identity hashes canonical JSON with explicit field
boundaries: task ID, base revision, patch digest, requirements text and revision.
The base must be a full lowercase 40- or 64-character Git object ID; symbolic
refs such as `HEAD` cannot define a reviewed candidate. An identical candidate
cannot be relabeled with another author. Different base, patch, requirements or
prerequisite admissions require new applicable evidence.

Attempts capture the exact accepted prerequisite receipt fingerprints before
work begins. Candidate identity includes those fingerprints. Acceptance and
admission export recheck the complete prerequisite chain in one transaction.
Revoking a check or revising a prerequisite withdraws dependent admissions,
including transitive dependents, while preserving their artifacts. Re-accepting
a prerequisite with changed evidence does not make old dependent reviews valid.
Repeated identical review submissions reuse their receipt without revoking an
admission. A changed review result requires an explicit retry key; replaying an
older key returns its historical receipt and cannot override a newer result.

The trusted supervisor preserves patch bytes and candidate records before
publishing execution completion to task selectors. Polling or cancellation does
not release the reservation while this capture is still active.

The trusted supervisor extracts a patch with `git add --all` and
`git diff --cached --binary --full-index <base>` after confirmed process exit.
This includes useful untracked work and work from timed-out attempts. Candidates
are preserved even if the command failed. A submitted candidate enters `awaiting_review`,
which prevents workers from spending more model calls on it while checks are
pending. Failed review makes the task eligible for another bounded attempt.
Only the current candidate can complete the task; earlier candidates and patches
remain available as salvage. Snapshots report remaining attempts/time and an
unresolved cause when a task budget is exhausted. Neither an exit code nor a worker's
completion claim proves acceptance. Review must check the proposed patch against
the stated baseline and requirements. Required check names can only be set by
the task owner; the owner must configure meaningful independent checks.

A new review observation withdraws an existing admission pending re-evaluation.
An owner revision withdraws admission too. Acceptance retries reuse the packet
until observations change. Accepted records remain durable, even if a later
revision supersedes them. The legacy MCP and dashboard completion methods
reject schema-v2 tasks and the reserved namespace before releasing batons or
changing completion state. The old autonomous mesh picker skips these tasks.
Raw CRDT messages cannot establish coordinator acceptance.

An admission packet contains exact candidate identity, author and attempt,
requirements revision, applicable reviewer observations, acceptance time and
`release_authorized: false`. Retrieve it with `/call/admission` and its patch
with `/artifacts/{patch}`. These are inputs for repository admission; they do
**not** authorize Git operations. For the existing
[decision-merge gate](decision-merge.md), create a PR from the reviewed patch and
run the existing SHA-bound workflow reviews and CI against its final head/base.
This prototype does not automatically import a packet as GitHub review evidence
or bypass that gate. The PR's resulting Git commit is a different identity from
the pre-PR patch candidate and must satisfy the gate's own checks.

## Failure, retention and private inputs

A coordinator/network outage holds protected writes and new acceptance. Work
already launched continues within its approved execution limits. Other local
independent actions do not require conductor permission. An unavailable service
cannot grant fresh shared permission. During graceful shutdown the service stops
serving requests and lets existing bounded adapter actions finish.

After supervisor loss, processes and cost are **unknown**. The adapter never
signals a remembered PID after restart (PID reuse can target unrelated work).
Deadline expiry revokes write permission, but does not prove process exit or
release task execution reservations. Inspect the host and use owner `observe`
only after verifying termination. Independently ready tasks remain selectable.
A live supervisor keeps the main child's PID allocated with `waitid(WNOWAIT)`
until it has signalled the process group and checked procfs for remaining live
members. Only then does it reap the main child. If group termination cannot be
confirmed, the state stays unknown and its reservation remains held. Cancellation
retries return the terminal result and never signal that old PID again.

Execution, lease and advice limits use monotonic deadlines bound to the host's
boot identity. Wall-clock timestamps remain available for observations but a
clock rollback cannot revive expired permission. A boot change revokes old
permission without deleting work or guessing termination. Workers can select
independent work while an older attempt has unknown termination. Expired or
revoked reservations that never crossed the launch transaction can be cancelled
safely before a fresh attempt consumes the remaining task budget. This
cooperative adapter does not contain malicious processes that escape their group; use a sandbox/cgroup adapter before
claiming that stronger termination/isolation guarantee.

All task, attempt, work, review, admission and event records are retained
indefinitely in this prototype. No automatic garbage collection deletes useful
work. Worker worktrees and private adapter logs also remain until an owner
removes them; only committed SQLite artifacts are the recovery authority.
Worker deletion and service restart preserve these artifacts. Host-loss recovery
requires an owner-maintained SQLite online backup on storage surviving the host.
The prototype provides no automatic failover or backup schedule. Do not copy
only the main database while WAL transactions are active; use SQLite's backup
API or stop the service and preserve the complete database safely.

`ApprovedInputProvider.worker_input(task_id, public_requirements)` is an optional
trusted provider boundary. Its output goes directly to worker stdin, without
being stored as a public record. The default returns public requirements only.
A custom provider must return only owner-approved inputs. Raw private guidance,
directory structures and memories stay outside public task records. Logs are
local private files (0600), never automatically exported as artifacts. Review
work products before deliberately submitting them to this public collaboration
space. Worktrees are not a mechanism for hiding host files from a worker.

## Upgrading prototype records

No SQL table migration deletes or rewrites work. Old active attempts without
boot-bound monotonic permission fail closed. A reserved attempt can be cancelled
as unlaunched; dispatched/unknown attempts need verified owner recovery. Old
dependent candidates without prerequisite provenance, or candidates with symbolic
base refs, cannot be accepted/exported. Their bytes and historical records stay
available. Snapshots mark unusable old admissions blocked so they cannot attract
repeated worker attempts or satisfy downstream readiness. An owner must approve
fresh attempts/candidates/reviews for that work, with the original task budget
retained. Existing dependency-free admissions with immutable bases remain usable.

### Upgrade compatibility and recovery

| Existing record/client | Supported behavior after upgrade | Required action |
| --- | --- | --- |
| Independent accepted candidate with a full Git ID | Its original admission/evidence packet is exportable unchanged. | None; do not rewrite its identity. |
| Accepted dependent candidate without receipt bindings | Artifact stays readable; acceptance/export is blocked. | Fix prerequisites first, owner revises task, then fresh attempt/candidate/review. |
| Candidate with a symbolic base | Artifact stays readable; the original baseline cannot be guessed safely. | Owner establishes the exact immutable base; fresh candidate and review. |
| Old reserved attempt without monotonic metadata | New execution/write permission is denied. | Worker cancels as unlaunched, then spends only the remaining task budget. |
| Old dispatched/unknown attempt | Reservation and saved work survive. | Independently verify death and preserve work before owner recovery. |
| Old lease and completed effect receipt | Old lease cannot grant a new write; an identical historical retry returns its original receipt without repeating the effect. | Acquire the next fence from a new permitted attempt. |
| Existing reviewer resends the same result | The original receipt and admission remain unchanged. | None. Changed results require a distinct `request_key`; a 409 explains this. |

Before replacing a running service, make a SQLite online backup to a new owner-
selected file, including committed WAL work, and stop the old service. Reopen
with the new service and inspect `/snapshot` and `/artifacts/{id}` before any
recovery operation. No upgrade step resets `attempts`, `allocated_seconds`,
artifact bytes or historical review/effect receipts.

For a snapshot task marked `admission_invalid`, the owner explicitly creates a
new requirements revision, even when retaining the same requirements text:

```python
# owner is a CoordinatorClient authenticated with the owner credential.
# task is the inspected task from await owner.snapshot().
revised = await owner.call(
    "revise", task_id=task["id"], expected_revision=task["revision"],
    requirements=task["requirements"], checks=task["checks"],
    reviewers=task["reviewers"],
)
```

Then a capable worker starts a new attempt within the retained budget. Saved
patch bytes can be deliberately salvaged, but must be checked against the
owner-established full Git base before creating a new candidate. Independent
review must use `revised["revision"]`; old evidence cannot accept the new
candidate. Do not automatically resolve an old symbolic `HEAD` against today's
repository. If attempts/time are exhausted, keep the work for owner action;
there is no automatic budget reset.

For dispatched/unknown or failed-capture recovery, first inspect the host and
save the useful patch through the original worker principal's `save_work` call.
Retrieve that receipt from the reopened store to verify durability. **Only after
independently verifying termination**, the owner can call:

```python
await owner.call("observe", attempt_id=attempt_id,
                 state="exited", termination_confirmed=True)
```

If termination cannot be verified, leave the attempt held and select independent
work. The service never signals a remembered PID after restart.

`tests/fixtures/coordinator-pr96.json` contains records generated by the original
PR96 coordinator at commit `907a1f1`, with its source hash recorded. Upgrade tests
import the data into SQLite; they do not execute historical code. They verify
unchanged valid admissions and artifact bytes, owner-only revision and fresh
review for incompatible records, preserved budgets, reserved/unknown recovery,
old fence rejection and historical retries that do not overwrite newer effects.

## Verification and pass limits

Limits were posted in the issue before development:

| Scenario | Required pass limit |
| --- | --- |
| Stale fence, duplicate protected effect, incorrect acceptance | Zero across deterministic adversarial tests. |
| Worker loss / authority reopen | 100% committed patch bytes and applicable evidence retained. |
| Two workers; conductor absent; one resource busy | Both independent tasks produce candidates; busy task allocates zero model calls. |
| Unknown termination / expired attempt | Same task remains reserved; old destination permission fails; independent tasks remain ready. |
| Failed or changed review/candidate/requirements | No acceptance or current admission until applicable evidence passes. |
| Lost response | Identical retries return saved receipt; changed payload rejected; one destination effect. |
| Repeated advice / exhausted budget | Fixed throttle/expiry and task-wide attempt/time caps. |
| Wakeup | Event recovery within 1 second in tests; at most 30-second idle recovery interval by default. |
| Legacy completion | Zero managed-task status changes or baton releases. |

Run deterministic verification:

```bash
PYTHONPATH=src:. .venv/bin/pytest -q tests/test_coordinator.py tests/test_coordinator_adversarial.py tests/test_coordinator_upgrade.py
```

Run the live two-worker trial with the key in the environment or `.env`:

```bash
.venv/bin/python scripts/trial-durable-collaboration.py --seconds 120
```

The trial uses a disposable repository, temporary HOME, explicit approved
provider environment and the repository's default free OpenRouter CLI model.
It starts two real OpenCode processes through the service and adapter, verifies
their file contents, independently records those checks under the reviewer
principal, accepts candidates, and reopens the store. No conductor runs. One
shared resource remains occupied. The JSON result reports elapsed time,
preserved candidates, blocked-task inference count, incorrect acceptances and
unknown cost. Provider/model availability may fail the live trial; such failure
must not be reported as a protocol pass. `--failure-logs` optionally saves
private diagnostic logs locally; never commit or publish them.
