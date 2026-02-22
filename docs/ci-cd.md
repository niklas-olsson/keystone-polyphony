# Continuous Integration & Delivery (CI/CD)

This document outlines the automated pipelines that power the development lifecycle of this project. All automation is handled via GitHub Actions and can be found in the `[.github/workflows](../.github/workflows)` directory.

## Workflows Overview

### 1. Auto-merge PR to Staging
**Trigger:** `pull_request_target` (opened, synchronize, reopened, ready_for_review) against `main`.
**File:** `auto-merge-staging.yml`

This workflow enforces an "open-by-default" staging environment. Whenever a non-draft Pull Request is opened against the `main` branch, it is automatically merged into the `staging` branch so that it can be tested in an integrated environment.

```mermaid
graph TD
    A[PR to main opened or updated] --> B{Is PR a Draft?}
    B -- Yes --> End((Skip Workflow))
    B -- No --> C[Fetch or Create staging branch]
    C --> D[Fetch PR branch changes]
    D --> E[Merge PR branch into staging]
    E --> F[Push staging to remote]
```

### 2. Hourly Merge to Main
**Trigger:** `schedule` (cron) or `workflow_dispatch`.
**File:** `hourly-merge-main.yml`

This scheduled job acts as the gatekeeper to production (`main`). It periodically checks the `staging` branch, runs all automated tests, and if everything passes cleanly, promotes staging into `main`.

```mermaid
graph TD
    A[Hourly Cron or Manual Dispatch] --> B[detect-staging: Check for changes]
    B --> C{Are there new commits?}
    C -- No --> Skip((Skip Workflow))
    C -- Yes --> D[test-staging: Run tests in Docker container]
    D --> E{Do tests pass?}
    E -- No --> Fail((Pipeline Fails))
    E -- Yes --> F[merge-to-main: Verify & Merge staging to main]
    F --> G{Running on a Fork?}
    G -- No --> End((Done))
    G -- Yes --> H[create-upstream-pr: Open PR to upstream]
    H --> End
```

### 3. Publish Agent Issues
**Trigger:** `push` modifying `.github/issues/*.md`.
**File:** `agent-issues.yml`

This workflow handles automated issue generation by AI agents. When markdown files are pushed to `.github/issues/`, this action parses them and translates them into actual GitHub Issues on the upstream repository.

```mermaid
graph TD
    A[Agent pushes markdown to .github/issues/] --> B{Is GH_TOKEN set?}
    B -- No --> C((Skip Publish))
    B -- Yes --> D[Iterate markdown issue files]
    D --> E{Issue fingerprint exists upstream?}
    E -- Yes --> F[Skip creation]
    E -- No --> G[Create GitHub Issue on upstream]
    F --> H[Remove markdown file from git]
    G --> H
    H --> I[Commit file removal & Push]
```

### 4. Label Contributors
**Trigger:** `pull_request` (closed).
**File:** `add-contributors.yml`

A community management workflow that ensures everyone who successfully merges code gets recognized. When a PR is merged, the author automatically receives the `CONTRIBUTOR` label (unless they are a bot).

```mermaid
graph TD
    A[Pull Request Closed] --> B{Is PR merged to upstream?}
    B -- No --> C((Skip))
    B -- Yes --> D{"Is Author a [bot]?"}
    D -- Yes --> C
    D -- No --> E{Does 'CONTRIBUTOR' label exist?}
    E -- No --> F[Create 'CONTRIBUTOR' label via API]
    F --> G[Apply label to the merged PR]
    E -- Yes --> G
```
