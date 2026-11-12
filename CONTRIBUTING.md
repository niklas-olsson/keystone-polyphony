# Contributing to Keystone Polyphony

Use a focused branch and open a PR to `main`. Manual PRs and autonomous
contributors use the same GitHub workflow; no upstream PAT or staging setup is
required.

## Quick start

1. Fork or clone the repository and create a feature branch from current main.
2. Install `requirements.txt` and the Node sidecar dependencies.
3. Run `./scripts/install-hooks.sh` once per clone.
4. Make a focused change, update relevant docs and run `bash scripts/lint.sh` and
   `bash scripts/run-tests.sh`.
5. Open a PR with the problem, resulting behavior and test evidence. Keep it draft
   until ready, or apply `hold-merge` to pause admission.

Agents must also follow [AGENTS.md](AGENTS.md), including ensemble architecture
review for significant or security-sensitive changes. Independent development
review and truthful evidence remain useful even when a provider is unavailable.

## Review and merge

Owner-authored ready PRs need current deterministic CI and resolution of actual
review findings. They do not need a model review or review label. External-author
PRs, including collaborator and bot PRs, also require the repository owner's
approval on the current head. They receive at most one bounded advisory
OpenRouter review per head, with no automatic retries or paid fallback.

The trusted controller tests and publishes the exact proposed merge into main.
A changed head or base needs fresh qualification. It does not merge through
staging, fabricate approvals or use a model to override failing tests. See
[CI policy and recovery](docs/ci-cd.md) for precise identity, evidence and race
boundaries and the manual reconciliation command.

Respond to returned findings on the same PR. Keep pending requests separate from
completed reviews; do not report a model failure or an unanswered request as an
approval. Record deterministic fixtures, native runtime trials, live provider
behavior and actual forge outcomes separately.

## Local hooks

The pre-commit hook normalizes trailing whitespace/newlines in staged text only,
checks the staged diff and runs lint. The pre-push hook runs the repository health
checks and tests. Install the dependencies first. Correct failed required checks;
do not use `--no-verify` to bypass them.

## Credentials and provider cost

`OPENROUTER_API_KEY` is used only by the external advisory reviewer in Actions.
Owner CI and the merge controller need no model credential. The normal pipeline
uses GitHub's scoped job token and no personal token. Automatic issue solving,
AI triage, scheduled seed calls and provider smoke workflows are retired.
Standalone runtime tools can still be used intentionally outside Actions.

Never commit credentials. Store operational credentials in Actions secrets and
keep provider calls in the intended review boundary. Fork contributors manage
their own credentials and should never request a maintainer's personal token.

## Issues and conduct

Update existing agreed issues when possible. Create a follow-up only for a
necessary uncovered gap. If publication is unavailable, retain a Markdown draft
in `.github/issue-drafts/`. Keep the legacy `.github/issues/` path empty while the
[historical queued workflow](docs/ci-cd.md#historical-queued-run) remains unresolved.
Earlier drafts remain preserved in Git history. Publish reviewed findings through
the normal issue interface when appropriate.

Follow the [Code of Conduct](CODE_OF_CONDUCT.md). Respectful collaboration applies
to human and autonomous contributors alike.
