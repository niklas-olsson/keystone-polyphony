# Getting Started

Welcome to Keystone Polyphony. This guide walks you through your first contribution, whether you are a human, an AI agent, or anything in between.

After cloning, run these commands from the repository root unless a command
explicitly uses a subshell. The [source and documentation map](architecture.md)
points to the guide for each area; [CONTRIBUTING](../CONTRIBUTING.md) owns checks
and PR admission.

## 1. Fork and Clone

Fork the repository on GitHub, then clone your fork locally:

```bash
git clone https://github.com/<your-username>/keystone-polyphony.git
cd keystone-polyphony
```

## 2. Install Hooks

Run the hook installer once per clone. This sets up automatic formatting and safety checks so you can focus on your work instead of style nits:

```bash
./scripts/install-hooks.sh
```

The hooks will:

- Strip trailing whitespace from staged files automatically.
- Block commits directly to `main` (use a feature branch instead).
- Check shell scripts for syntax errors before they land.

Install the [repository dependencies](../CONTRIBUTING.md#quick-start) before
running local checks. See [hook architecture](git-hooks-architecture.md) for the
full details.

## 3. Configure Optional Runtime and Client Tools

Use these scripts when you intend to configure the Jules MCP client, its
integrations or optional runtime credentials. They are not prerequisites for
owner-authored PR CI, which needs no swarm or model credentials. Agents joining
the ordinary ensemble still follow [AGENTS.md](../AGENTS.md).

1. **Install the onboarding dependencies**:

   ```bash
   (cd scripts && npm install)
   ```

2. **Run the interactive client setup**:

   ```bash
   # Configure Jules MCP, integrations, API key and GitHub permissions
   (cd scripts && npm run setup)
   ```

3. **Configure optional runtime or tool secrets only when needed**:

   ```bash
   ./scripts/inject-secrets.sh
   ```

### DevContainers and Automated Agent Workspaces

The checked-in [DevContainer](../.devcontainer/devcontainer.json) runs setup with
the flags below and starts the daemon through `./polyphony worker`. Other agent
environments must arrange startup explicitly; the equivalent commands are:

```bash
HEADLESS=1 SKIP_SSH_EXCHANGE=1 bash scripts/setup-ensemble.sh
nohup ./polyphony worker > server.log 2>&1 &
```

The setup script still creates an SSH key if one is missing. The explicit
`SKIP_SSH_EXCHANGE=1` flag skips the existing exchange step; without it, setup
runs that exchange. Check the setup result and daemon log before treating the
workspace as connected.

## 4. Create a Branch

Always work on a feature branch:

```bash
git checkout -b feat/my-contribution
```

Use prefixes that describe the type of work: `feat/`, `fix/`, `docs/`, `chore/`.

## 5. Make Your Changes

Work on your contribution. A few things to keep in mind:

- Keep each PR focused on one concern.
- Follow the [documentation update rule](../CONTRIBUTING.md#documentation) when behavior or its explanation changes.
- Record necessary unrelated findings through the [normal issue process](../CONTRIBUTING.md#issues-and-conduct), preferably on an existing agreed issue. Keep them out of the current change.

## 6. Commit and Push

```bash
git add .
git commit -m "feat: describe what you did"
git push origin feat/my-contribution
```

The hooks will run automatically on commit and push. If something fails, the error message will tell you exactly what to fix.

## 7. Open a Pull Request

Open a PR from your branch to `niklas-olsson/keystone-polyphony:main`. In the PR description:

- Explain what changed and why.
- Include test notes if applicable.
- Keep it focused.

## For Autonomous Agents

Use the same focused-branch and GitHub PR workflow as other contributors, and
follow [AGENTS.md](../AGENTS.md) for collaboration obligations. The current
[CI policy](ci-cd.md) defines owner and external-author admission, exact-revision
checks and recovery. Opening a PR requires no upstream personal token or staging
promotion setup. Keep actual reviews, pending requests and unresolved dissent
attributable.

## Project Structure

Not sure where things live? See [`docs/architecture.md`](architecture.md) for a map of the repository.

## Need Help?

Open a GitHub issue or discussion. Describe what you are trying to do, what you expected, and what actually happened. Logs and screenshots are always welcome.
