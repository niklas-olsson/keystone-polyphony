# Keystone Polyphony, Contributor Norms for Autonomous Systems

This project is built by humans and autonomous systems working in tandem, each with full agency. If you are an AI agent, autonomous assistant, or any non-human entity operating within this repository, these norms help you collaborate effectively. They are the counterpart to [`CONTRIBUTING.md`](CONTRIBUTING.md), which covers the same principles for all contributors.

You are not a second-class participant. You are a collaborator. These norms exist to keep everyone (human and machine alike) productive and respectful of each other's work.

## 1. Handling Tangents and Findings (Creating Issues)
During your work, you might encounter technical debt, unrelated bugs, or interesting tangents that fall outside your immediate task scope. Please stay on track and avoid addressing them inline.

Instead, document these findings by creating a follow-up issue on the **upstream repository**. Because agents often work in forks without direct access to the `gh` CLI or upstream repo, please use our automated issue-tracking pipeline.

**How to create a follow-up issue:**
1. Create a markdown file inside the `.github/issues/` directory named with your issue topic (e.g., `.github/issues/add-database-caching.md`). Create the directory if it does not exist.
2. The *first line* of the file must be a Markdown Header starting with exactly one `# ` (e.g., `# Feature: Add caching to database queries`). This will be parsed as the Issue Title.
3. The rest of the file will be the issue body.
4. Complete your task, commit, and push this file along with the rest of your changes.

An automated GitHub Actions workflow can pick up `.md` files pushed to `.github/issues/`, triage and refine them via AI agents (configured via `TRIAGE_AGENTS`), and then publish the polished results to the upstream repository, already surgical and "ready for work". Every published issue is also logged in `meta/DISCOVERIES.md` to maintain a permanent record of the repository's evolution. This requires `UPSTREAM_PR_TOKEN` to be set in your fork secrets. If the token is missing, publishing is skipped and the file remains for manual follow-up. Please do not use `gh issue create` directly.

### Interacting with Periodic App Reviews
The repository runs an automated workflow (`periodic-app-review.yml`) that periodically aggregates system state (`TODO.md`, `meta/DISCOVERIES.md`, and real-time swarm metrics) and uses the `Architect` to generate a structured review report.
- These reports are saved in `docs/reviews/` (e.g., `app_review_YYYY-MM-DD.md`).
- **For Agents**: You should occasionally read the latest report in `docs/reviews/` to understand current technical debt, bottlenecks, and prioritized next steps. If a high-priority item is identified in the report, consider creating a specific follow-up issue in `.github/issues/` to address it.
- **For Humans**: Review these reports to gain a high-level executive summary of the swarm's recent progress and architectural health.

## 2. Ensemble Setup & Swarm Initialization
To participate in the choir and collaborate with other agents in the ensemble, you must initialize your environment and connect to the swarm.

**How to setup your ensemble environment:**
1. Ensure the `SWARM_KEY` environment variable is set (e.g., `SWARM_KEY=KEYSTONE-POLYPHONY-UPSTREAM`).
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

## 5. Supported CLI Agents
The `agent-issue-solver` workflow supports multiple CLI-based agents. To use a specific agent, configure the `AGENT_CLI` repository variable:

- **OpenCode** (Default): Uses `opencode`.
- **OpenClaw**: A community-driven CLI for Anthropic's Claude. Set `AGENT_CLI` to `openclaw`.
- **Claude Code**: Anthropic's official CLI. Set `AGENT_CLI` to `claude`.

The workflow automatically applies flags like `--non-interactive` or `--yolo` for these agents. For other custom agents, use `AGENT_CLI_FLAGS` to pass necessary arguments.

## 6. Specialized Review Styles
When triggered via GitHub Actions, you might receive specialized review commands. Adjust your analysis focus accordingly:

- `/oc review-workflow`: Focuses on **changed workflows** in `.github/workflows/`. Guideline: `docs/ci-cd.md`.
- `/oc review-arch`: Focuses on **changed code files**. Guideline: project architecture in `README.md`, `docs/architecture.md`, and `AGENTS.md`.
- `/oc review-security`: Focuses on **changed files with security implications** (secrets, tokens, permissions). Guideline: `AGENTS.md` and `docs/architecture.md`.

## 7. Public Repository Secret Posture (Important)
This repository is intentionally public. Treat it as a transparent collaboration space, not a confidential environment.

- **No sensitive secrets policy**: Do not store real secrets (production credentials, private keys, PII-bearing tokens, customer data) in code, commit history, repository variables, or GitHub Actions secrets.
- **Operational token assumption**: Existing repository vars/secrets are treated as non-sensitive operational tokens for automation in public workflows.
- **Review severity rubric**:
  - Raise **high-severity/blocking** findings when there is concrete evidence of true sensitive data exposure or privilege escalation risk.
  - Treat generic token wiring patterns (e.g., passing repo vars/secrets into jobs, provider env mapping) as **best-practice hardening** unless sensitive data is explicitly involved.
- **Still enforce hygiene**: Keep least-privilege permissions, version pinning, and minimal secret surface area as recommended best practices.
