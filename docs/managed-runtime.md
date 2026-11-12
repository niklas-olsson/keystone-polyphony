# Canonical managed runtime handoff

This service lets a trusted OpenJules supervisor execute a root attempt owned by
the existing Polyphony `Coordinator`. ZPM owns repository capacity; OpenJules
owns containers, process termination and workspace capture. Polyphony owns task
requirements, attempt budgets, artifact identity and independent acceptance.
Ordinary peer discussion remains independent of this service.

This guide describes the root-runtime handoff from
[#109](https://github.com/niklas-olsson/keystone-polyphony/issues/109).
The authenticated service is the managed authority. Experimental task CLI/MCP
operations refuse reserved `task_v2_` identities, and advisory task batons also
refuse backlog entries marked as managed. Historical experimental records remain
inspectable; caller-supplied labels cannot authenticate managed ownership. See
the [compatibility boundary](durable-collaboration.md).

The [bounded child grant API](child-grants.md) adds optional canonical delegation
for [#112](https://github.com/niklas-olsson/keystone-polyphony/issues/112).
Capabilities retain `children: false`: an authority response does not prove that
a connected runtime can execute children. Separate `child_grants`, `child_policy`
and `child_initial_outcome` fields describe the authority's supported contracts.
Real root and bounded child execution are demonstrated for the exact installed
revisions in the [qualified installation record](https://github.com/niklas-olsson/zero-point-modulator/pull/28#issuecomment-6811017999).
That evidence does not qualify an arbitrary component combination.

Automatic forge publication and cross-host authority failover are separate from
this handoff. Internal acceptance always retains `release_authorized: false`
and is not GitHub approval or merge provenance. Remaining integrated acceptance
is maintained in [ZPM #3](https://github.com/niklas-olsson/zero-point-modulator/issues/3).

## Single-host setup

Keep the authority database on persistent local storage outside worker
lifetimes. Run one authority service on the trusted host. Remote clients use
HTTP; never share a SQLite WAL file across hosts. The service listens on
loopback. An operator may place an authenticated HTTPS reverse proxy in front
of it for remote access, retaining bearer authorization.

The owner-provisioned credentials file has mode `0600`. Each token must contain
at least 32 characters. Generate and store real tokens using the deployment's
secret mechanism. The values below describe structure and are not usable
credentials:

```json
{
  "<operator-token>": {"identity": "operator", "role": "owner"},
  "<ingress-token>": {
    "identity": "repository-ingress", "role": "ingress",
    "deployment_id": "installation-one",
    "repositories": ["github:github.com/example/project"]
  },
  "<runtime-token>": {
    "identity": "openjules-runtime", "role": "runtime",
    "deployment_id": "installation-one",
    "repositories": ["github:github.com/example/project"]
  },
  "<reviewer-token>": {
    "identity": "independent-review", "role": "reviewer",
    "deployment_id": "installation-one",
    "repositories": ["github:github.com/example/project"]
  }
}
```

The managed policy file is operator-owned. Use the exact immutable Git base
also approved in the OpenJules profile, not a branch name:

```json
{
  "schema_version": 1,
  "deployment_id": "installation-one",
  "repositories": {
    "github:github.com/example/project": {
      "checks": ["tests"],
      "reviewers": ["independent-review"],
      "max_attempts": 3,
      "budget_seconds": 900,
      "profiles": {
        "approved-coding": {
          "base": "0123456789012345678901234567890123456789",
          "attempt_seconds": 300,
          "capabilities": ["python"]
        }
      }
    }
  }
}
```

Checks and reviewers are nonempty distinct lists. `max_attempts` is 1–1000.
Time limits are positive finite seconds, at most seven days; a profile's attempt
duration cannot exceed its repository task budget. Profile definitions have
`base`, `attempt_seconds`, and `capabilities`, with optional explicitly approved
[child policy fields](child-grants.md). Unsupported fields are
rejected. Image, repository checkout path, command and runtime secrets belong
in OpenJules' approved profile, not in issue text or this API's launch payload.

```sh
python -m src.liminal_bridge.coordinator_service \
  --db /var/lib/polyphony/coordinator.db \
  --credentials /etc/polyphony/credentials.json \
  --managed-config /etc/polyphony/managed.json \
  --port 8766
```

Existing local execution can still use `--execution-config`. Its attempts and
external attempts select different modes, and cannot launch through both paths.
Without `--managed-config`, managed endpoints return unavailable. A health check
or bearer credential alone does not establish managed runtime compatibility.

## Submit work

Provide `POLYPHONY_INGRESS_TOKEN` through the environment and submit an explicit
execution request:

```sh
python -m src.liminal_bridge.managed_cli enroll \
  --url http://127.0.0.1:8766 \
  --deployment-id installation-one \
  --repository github:github.com/example/project \
  --source github:issue:19 \
  --request-key issue-19-run-1 \
  --title 'Implement the requested change' \
  --requirements-file requirements.txt
```

The same deployment, repository, source and request key returns the same task
across concurrent deliveries and restarts. Changed input or enrollment policy
under that key conflicts. A deliberate new execution uses a new explicit key.
Retries after timeouts must retain the original key. This operation does not
prove that an upstream GitHub webhook is authentic: an ingress adapter must
verify its source before invoking this authenticated endpoint.

The ingress principal cannot choose checks, reviewers or budgets. Runtime
principals cannot enroll, revise, independently review or accept tasks. Runtime
credentials stay in the trusted supervisor and must never enter agent containers.

## Version 1 wire contract

All managed mutations require JSON, `schema_version: 1`, `deployment_id` and
canonical `repository`. Unknown fields, boolean schema versions and unexpected
query fields are rejected. Every endpoint requires a bearer token with the
appropriate role and exact repository scope. The existing global owner is the
trusted host administrator; new ingress/runtime roles have no legacy API access.

| Interface | Additional fields and result |
| --- | --- |
| `GET /managed/capabilities?deployment_id=D&repository=R&profile=P` | Echoes scope/profile, `service: keystone-polyphony`, `protocol: managed-runtime-v1`, approved `base` and `profile_digest`, `managed_dispatch`, `external_root`, `profile_supported`, and `children: false`. |
| `GET /managed/snapshot?deployment_id=D&repository=R` | Canonical task/attempt/work/candidate/evidence arrays restricted to that scope, plus scope and schema version. |
| `POST /managed/enroll` | Ingress: `source`, `request_key`, `title`, `requirements`; returns `{schema_version, deployment_id, repository, task}`. |
| `POST /managed/claim` | Runtime: `profile`, `worker_id`, `task_id`, `request_key`, `base`; returns the binding and current permission described below. |
| `POST /managed/permission` | Runtime: `attempt_id`; returns the same binding and current permission, without allocating or extending execution. |
| `POST /managed/capture` | Runtime: `attempt_id`, `kind` (`patch`, `log`, `manifest`), UTF-8 `content`; returns `artifact_id` (SHA-256), kind, attempt and scope. |
| `POST /managed/observe` | Runtime: `attempt_id`, `state`, boolean `termination_confirmed`, boolean `launch_fenced`, `artifacts` mapping. Returns the current canonical attempt. |
| `POST /managed/candidate` | Runtime: `attempt_id`, `artifact_id` of the final captured patch; returns `candidate`. Uses the bound immutable base. |
| `GET /managed/artifacts/{hash}?deployment_id=D&repository=R` | Returns captured UTF-8 content only when an attempt in that scope owns it. |

Claim and permission return:

```text
schema_version, deployment_id, repository, attempt, requirements,
launch_allowed, remaining_seconds, reason,
binding: {
  attempt_id, deployment_id, repository, runtime_principal, worker_id,
  profile, base, profile_digest, launch_key, spec_digest, deadline,
  requirements_revision
}
```

The worker actor is derived from the authenticated runtime principal and immutable
worker incarnation. The binding, deadline and task budget are committed in the
same transaction as the external execution mode. A task has at most one unresolved
root; a runtime/worker incarnation also has at most one unresolved root. The
worker chooses its task. No conductor or second scheduling ledger is introduced.

A duplicate claim returns the original binding even after expiry, with
`launch_allowed: false`. A changed retry conflicts. Permission is the original
attempt's continuing authorization, **not a new launch grant**. The runtime must
durably journal its original launch key before spawning, reconcile uncertain
outcomes, and recheck current permission immediately before its one launch.
Policy changes, requirements revisions, cancellation, expiry and host reboot
revoke permission. None proves that an already launched process stopped.
The attempt retains its originally approved requirements, so recovery can finish
capture after an owner revision without substituting new instructions. A stale
candidate still conflicts and cannot satisfy the revised task.

## Capture, termination and review

Capture stores at most 4 MB per UTF-8 artifact in the authority database. Each
final kind is immutable for its attempt; identical retries return its hash and
changed content conflicts. A manifest must be a JSON object. Its archive
metadata describes capture held by OpenJules, not an archive independently
downloaded or verified by Polyphony. OpenJules must retain raw workspace
artifacts durably outside workers until its retention policy permits removal.

Nonterminal observations use `artifacts: {}` and `launch_fenced: false`.
Terminal observations require `state: exited` or `timed_out`, confirmed process
tree termination, `launch_fenced: true`, and all three captured content hashes:

```json
{"patch": "<sha256>", "log": "<sha256>", "manifest": "<sha256>"}
```

The authority verifies those bytes exist and match their hashes before releasing
the task reservation. Missing capture, corrupted storage or unknown termination
holds the reservation. Lost responses replay the same receipt. An external
attempt cannot be terminated through the legacy owner `observe` operation.

Candidate creation requires that final capture and uses its patch and original
base. The runtime principal and derived worker actor both count as authors for
review independence. Guest test output is captured evidence, not a passed
independent check. Authorized reviewers submit existing `/call/evidence`
observations, and the owner uses `/call/accept` only after those checks satisfy
the canonical policy. This never creates a GitHub commit, PR, workflow result or
release authorization by assertion.

## Verification

`tests/test_managed_runtime.py` exercises actual localhost HTTP authorization,
scoped reads, concurrent enrollment through distinct SQLite connections,
idempotent claim recovery, profile/reboot revocation, capture corruption,
terminal replay and independent review. Existing coordinator, adapter and
upgrade suites continue to exercise the local mode. These are service contract
tests; real Docker execution and the combined ZPM installation require the
separate cross-repository trial.

For this implementation, the architecture proposal and peer review were posted
through the actual `ensemble_chat` implementation in an isolated local mesh with
loopback-only bootstrap and separate agent identities. This was development
coordination, not approval from an existing external swarm or a production
deployment.
