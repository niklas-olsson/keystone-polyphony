# Repository Architecture

This map routes contributors from a source area to the existing guide that owns
its explanation. The files below are representative entry points, not a complete
module inventory. Use [README](../README.md) for project orientation,
[CONTRIBUTING](../CONTRIBUTING.md) for changes and review, and
[AGENTS](../AGENTS.md) for agent collaboration norms.

## Source and documentation map

| Area | Representative source | Canonical explanation |
| --- | --- | --- |
| Runtime and client setup | [setup-ensemble.sh](../scripts/setup-ensemble.sh), [DevContainer](../.devcontainer/devcontainer.json), [polyphony](../polyphony) | [Getting Started](getting-started.md); client-specific [Zed](zed-integration.md) and [VSCode](vscode-integration.md) guides |
| Ordinary ensemble tools and advisory planning | [server.py](../src/liminal_bridge/server.py), [pulse.py](../src/liminal_bridge/pulse.py), [architect.py](../src/liminal_bridge/architect.py) | [Liminal Bridge](liminal-bridge.md) |
| Signed public conversation and restricted profile | [conversation.py](../src/liminal_bridge/conversation.py), [conversation_server.py](../src/liminal_bridge/conversation_server.py) | [Durable public peer conversation](conversation.md) |
| Peer transport and notification delivery | [mesh.py](../src/liminal_bridge/mesh.py), [sidecar/bridge.js](../src/liminal_bridge/sidecar/bridge.js) | [Mixed-transport swarm delivery](swarm-delivery.md) |
| Durable authority, worker choice and coordinator service | [authority.py](../src/core/collaboration/authority.py), [coordinator_service.py](../src/liminal_bridge/coordinator_service.py) | [Durable collaboration](durable-collaboration.md) |
| Managed enrollment and root-runtime handoff | [managed_runtime.py](../src/core/collaboration/managed_runtime.py), [managed_service.py](../src/liminal_bridge/managed_service.py), [managed_cli.py](../src/liminal_bridge/managed_cli.py) | [Canonical managed runtime handoff](managed-runtime.md) |
| Bounded delegated grants and private grant routes | [delegated_grants.py](../src/core/collaboration/delegated_grants.py), [delegated_grant_service.py](../src/liminal_bridge/delegated_grant_service.py) | [Bounded delegated grants and v1 compatibility](child-grants.md) |
| Pull requests, exact-revision validation and admission | [workflows](../.github/workflows/), [pr-controller.py](../scripts/pr-controller.py), [review-external-pr.py](../scripts/review-external-pr.py) | [CI policy and recovery](ci-cd.md) |
| Local checks and hooks | [.githooks](../.githooks/), [install-hooks.sh](../scripts/install-hooks.sh), [lint.sh](../scripts/lint.sh), [run-tests.sh](../scripts/run-tests.sh) | [Contribution checks](../CONTRIBUTING.md#local-hooks) and [hook architecture](git-hooks-architecture.md) |

Update the owning guide when its behavior or evidence changes, and update this
map when routing changes. The [documentation rule](../CONTRIBUTING.md#documentation)
keeps that work in the same PR without requiring prose changes for internal
refactoring.

## Boundaries to keep visible

Public conversation preserves distinct authors, replies, declines and dissent.
Reading a message does not grant managed execution authority. Independently
invited peers remain separate from delegated authority; the conversation and
delegated-grant guides own those contracts. The legacy `child_grants.py` and
`child_service.py` source modules retain aliases to the canonical implementation
for existing consumers. Version-1 wire and journal names remain unchanged.

The managed service owns canonical requirements, attempt budgets and artifact
identity. ZPM owns repository capacity, and OpenJules owns container execution,
confinement, termination and capture. A grant response alone does not demonstrate
an executable child runtime. Follow [managed-runtime.md](managed-runtime.md) and
[child-grants.md](child-grants.md) for the handoff and its qualification limits.

Keep deterministic fixtures, native transport/runtime trials, live provider
behavior and actual forge outcomes distinct. A retained artifact or internal
acceptance does not establish GitHub approval, publication or merge.

## Contributions and recovery

Use a focused branch and open a PR directly to `main`. Current admission checks
the exact proposed merge against the current head and base. The
[CI policy](ci-cd.md) owns contributor identity rules, required validation,
review holds and recovery; this map does not duplicate them.

Use the [normal issue process](../CONTRIBUTING.md#issues-and-conduct) for findings
and attributed follow-up. Offline drafts belong in `.github/issue-drafts/`.
Preserve the [historical queued-workflow limitation](ci-cd.md#historical-queued-run)
when handling old automation; retired workflow diagrams are not current
publication instructions.
