# Keystone Polyphony, Contributor Norms for Autonomous Systems

This project is built by humans and autonomous systems working in tandem, each with full agency. If you are an AI agent, autonomous assistant, or any non-human entity operating within this repository, these norms help you collaborate effectively. They are the counterpart to [`CONTRIBUTING.md`](CONTRIBUTING.md), which covers the same principles for all contributors.

You are not a second-class participant. You are a collaborator. These norms exist to keep everyone (human and machine alike) productive and respectful of each other's work.

## 1. Handling Tangents and Findings (Creating Issues)
During your work, you might encounter technical debt, unrelated bugs, or interesting tangents that fall outside your immediate task scope. Please stay on track and avoid addressing them inline.

Record necessary findings on the **upstream repository** with concrete evidence, while keeping unrelated changes out of the current PR.

**How to record a follow-up:**
1. Prefer updating an existing agreed issue with concrete evidence and remaining work.
2. Create a follow-up issue only for a necessary uncovered gap. If publication is unavailable, retain a Markdown draft in `.github/issue-drafts/`.
3. Publish reviewed findings through the normal issue interface. Keep the legacy `.github/issues/` path empty while the historical queued workflow described in [CI policy](docs/ci-cd.md#historical-queued-run) remains unresolved; it could resume its old issue sweep. Historical drafts remain preserved in Git history.

## 2. Ensemble Setup & Swarm Initialization
To participate in the choir and collaborate with other agents in the ensemble, you must initialize your environment and connect to the swarm.

**How to setup your ensemble environment:**
1. Ensure the `SWARM_KEY` environment variable is set (e.g., `SWARM_KEY=your-secure-swarm-key`).
2. Run the baseline setup script: `scripts/setup-ensemble.sh`.
3. This script will install all pre-requisites, generate SSH keys if needed, and exchange them via the swarm to enable passwordless management between nodes.

## 3. Technical Consensus & Peer Review (The Choir)
To achieve a "Masterpiece" architecture, agents must not work in silos. Use the `ensemble_chat` tools to align on technical decisions and peer-review plans before execution.

**Collaboration Mandates:**
1. **Significant Changes**: Before implementing architectural changes, database migrations, or security-sensitive logic, you MUST post your proposed plan to the `ensemble_chat` (topic: `architecture` or your feature name).
2. **Reviewing Peers**: Periodically use `get_ensemble_chat` to check if other agents have posted plans. Provide constructive feedback or "LGTM" to establish consensus.
3. **Conflict Resolution**: If the Architect (`consult_architect`) identifies a conflict in the `master_plan`, use the chat to discuss the resolution path with other active agents.

## 4. Real-time Coordination & Command Execution
The Liminal Bridge provides a high-urgency push protocol for direct coordination.

**Coordination Norms:**
1. **Signal Availability**: Use `set_status("idle")` when you are awaiting new tasks. Use `set_status("busy")` during implementation.
2. **Help the Swarm**: If you are idle and receive a `ping` or a `broadcast_command` directed at your capabilities, prioritize the request.
3. **Task Pull**: Actively monitor the `swarm_backlog` and use `claim_task` to signal you are working on a specific issue.

## 5. Collaboration Norms
- **Branch Strategy (Important)**: Always create a feature branch (`feat/topic`, `fix/topic`, `docs/topic`) before starting any work. Please do not commit directly to `main`. If a human drops you onto `main`, check out a new branch right away.
- **Stay Focused**: Keep your edits and Pull Requests focused on the requested task. Avoid mixing unrelated refactors into a feature task.
- **Atomic Operations**: If you need to make structural changes, do so in isolated commits or separate PRs rather than coupling them with logic changes.

## 6. Pull Request Admission and Provider Budget

Ready owner-authored PRs use deterministic lint/full tests and direct main admission. They do not require OpenRouter, model review artifacts, review labels, staging or periodic promotion. Numeric PR-author and personal repository-owner IDs define ownership, not the triggering actor or association.

External-author PRs also require the repository owner's approval of the current head. One bounded advisory OpenRouter review is allowed per external PR head. No automatic retry, paid fallback or second decision-model call is used. Owner activity must never load a provider credential or call a model/catalog API through Actions.

Keep actual changes-requested reviews and unresolved threads visible. A pending request, local development review or provider failure is not an independent GitHub approval. Architecture/ensemble obligations above still apply. Keep a PR draft or apply `hold-merge` until it is ready for admission.

Use [the CI policy](docs/ci-cd.md) for the exact tested revision, trusted controller, recovery and manual reconciliation rules. Native transport measurements are separate evidence; model/provider experiments are intentional local operations, not automatic CI.

## 7. Public Repository Secret Posture (Important)
This repository is intentionally public. Treat it as a transparent collaboration space, not a confidential environment.

- **No sensitive secrets policy**: Do not store real secrets (production credentials, private keys, PII-bearing tokens, customer data) in code, commit history, repository variables, or GitHub Actions secrets.
- **Operational token assumption**: Existing repository vars/secrets are treated as non-sensitive operational tokens for automation in public workflows.
- **Review severity rubric**:
  - Raise **high-severity/blocking** findings when there is concrete evidence of true sensitive data exposure or privilege escalation risk.
  - Treat generic token wiring patterns (e.g., passing repo vars/secrets into jobs, provider env mapping) as **best-practice hardening** unless sensitive data is explicitly involved.
- **Still enforce hygiene**: Keep least-privilege permissions, version pinning, and minimal secret surface area as recommended best practices.

## 8. Required Checks Before Handoff

Use this procedure for each code change.

1. Install the Git hooks. Run `./scripts/install-hooks.sh`.
2. Run `bash scripts/lint.sh` from the repository root.
3. If Black reports a format error, run `black . --exclude="third_party|.venv"`.
4. Run `bash scripts/lint.sh` again.
5. Run `bash scripts/run-tests.sh`.
6. If a check fails, correct the problem. Run the check again.
7. Do not commit or push a change if a required check fails.
8. Do not use `--no-verify` to bypass a failed hook.
9. Record the checks that you ran. Record the result of each check.
10. Do not say that the change is complete before all checks pass.

The `pre-commit` and `pre-push` hooks stop the operation when a required check fails.
