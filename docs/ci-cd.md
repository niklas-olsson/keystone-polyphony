# Pull requests and direct main admission

Owner-authored pull requests need deterministic CI. They do not need OpenRouter,
review labels, a model verdict, a staging branch or a periodic promotion job.
External-author pull requests get a bounded advisory model review and require the
repository owner's approval of the current head before automatic admission.

| Contributor | Required for automatic main admission | Provider use |
| --- | --- | --- |
| Repository owner | Ready PR, current proposed merge passes lint/full deterministic tests, no unresolved review findings | None |
| Anyone else, including collaborators and bots | The same CI and review conditions, plus repository-owner approval on the current head | At most one advisory request per PR head |

Ownership comes from GitHub's numeric PR author ID and the personal repository
owner ID. The triggering actor, branch name, labels, association and fork owner
cannot grant the owner path. An owner-authored fork is supported. Missing or
deleted head-repository metadata holds admission.

## Normal flow

Open a focused PR to `main`, and mark it ready when it can be integrated. No
special label is required. Keep it draft, or apply `hold-merge`, to prevent
admission while work or discussion continues.

**Direct Main Admission** fetches the current PR, main and proposed merge. It
asks **PR Validation** to test the exact merge with ordered parents
`[current main, current PR head]`. Validation runs Python 3.12, Node 22, the
repository lint and the full deterministic pytest suite. A failed, skipped,
cancelled, missing or pending check cannot admit a PR. An actual
changes-requested review or unresolved review thread also holds admission.

After qualification, the controller publishes that already-tested merge object
to main. It checks fast-forward ancestry and uses an explicit selected-main Git
lease. If main changes, the old candidate cannot be published, even if the new
main would ordinarily permit a fast-forward. The next candidate needs fresh
current-base CI. There is no conflict-preference merge or automatic force rewind.

GitHub's PR metadata and Git refs do not share a transaction. The controller
rechecks author, head, readiness, review state and CI before publication, and
reads main and PR state afterward. Its report distinguishes publication of the
tested tree from observed PR closure. If the PR head moves during publication,
it does not report that moved PR as merged.

A successful publication starts a fresh controller from new main. This both
applies updated policy and reconciles remaining PRs without relying on another
push event from `GITHUB_TOKEN`. No PAT or polling schedule is needed.

The default job credential may be refused permission to publish workflow-file
changes. Such a refusal is reported separately from a failed test or stale-base
lease. A maintainer can publish the qualified change with the necessary existing
permission; the pipeline does not expand its credentials or weaken branch rules.

## Where the code executes

The privileged controller executes only trusted main code. Its entry points are
`pull_request_target`, completed validation/review-signal workflows, main pushes
and manual dispatch on main. A review event is received by **PR Review Signal**,
a separate job with no checkout, no secrets and no permissions. That signal
carries no admission authority; the controller fetches all evidence again.

Candidate code runs in a separate read-only validation workflow on a fresh hosted
runner. The workflow definition comes from the selected main revision, and its
inputs and fetched merge parents are checked before running candidate code. It
has no provider credentials, repository write token, persisted Git credentials
or shared cache. The writer never imports candidate code or uses a candidate
artifact as executable policy.

CI evidence must match the known workflow path and ID, dispatch event, exact
PR/head/base/merge run name, trusted workflow revision and latest actual
`validate` job attempt. Required validation steps must have succeeded. A newer
pending or failed run supersedes an older success. Historical model failures and
retired review labels do not become implicit owner merge gates.

## External advisory review and spending

**External PR Review** is the only Actions workflow with an OpenRouter key. It
runs after deterministic CI for an external PR, using trusted main code and the
immutable proposed-merge diff as data. The advisory is limited to the bounded
GitHub compare text; API diff completeness is not independently established. It
never checks out external code or gives the model tools or a GitHub mutation token.

The reviewer reserves a bot-authored attempt marker before loading the provider
key. A later run for that same PR head, including a new base or a manual rerun,
does not spend again. The report retains the actual reviewed head, base and merge;
it does not claim a new review merely because main advanced. Owner PRs are
excluded by fresh API identity checks before credential access.

The request uses one fixed free model, `recursive/flint-v1.0:free`, bounded input
and output, a timeout, no automatic retry, no model catalog lookup and no paid
fallback. Missing credentials, an unsupported/oversized diff, provider failure
or an ambiguous attempt remain visible as unavailable advisory evidence. They
never become an approval. The repository owner's explicit current-head approval
is authoritative for external contributions. There is no second model call to
interpret the first review.

Standalone runtime decision/model tools remain available for intentional local
use. Automatic issue solving, AI issue triage, model smoke calls and scheduled
provider-backed swarm seeding have been removed from Actions.

## Recovery

Start with the **Direct Main Admission** Actions summary and its
`admission-results.json` artifact. It records the selected revisions and a
concrete held/dispatched/published result for each examined PR.

| Situation | Action |
| --- | --- |
| CI failed | Fix the failure and push a new head; for an infrastructure failure, rerun the existing validation after inspecting it |
| Main advanced or GitHub is still calculating a merge | Run Direct Main Admission on main again if no later event has reconciled it |
| Conflict | Resolve it on the PR branch, run required local checks and push |
| Draft or `hold-merge` | Mark ready or remove the label when integration is intended |
| Review hold | Resolve the thread or have the reviewer update/dismiss the actual changes-requested review; after thread resolution alone, run reconciliation manually |
| External approval missing or stale | Repository owner reviews and approves the current PR head |
| Validation dispatch was recorded but no run appears | Inspect Actions first, then use the targeted manual retry below |
| Test tree published but PR closure not observed | Inspect actual main and PR head; do not claim the moved PR merged |
| GitHub refuses workflow-file publication or a branch rule | A maintainer uses the applicable existing permission after verifying the reported exact candidate and CI; no model retry is needed |

Ordinary reconciliation:

```bash
gh workflow run pr-controller.yml --ref main
```

A dispatch is reserved in a bot comment before the request so an ambiguous API
reply cannot create an automatic retry loop. If no run exists, deliberately retry
only that PR's missing dispatch:

```bash
gh workflow run pr-controller.yml --ref main -f retry_missing_validation_pr=123
```

This does not rerun a visible failed validation or override any merge condition.
It does not retry provider review. Manual runs from a feature branch cannot serve
as trusted-main validation or admission evidence.

## Native runtime qualification

Local required hooks, lint and tests remain required. Native transport trials,
traffic capture and storage measurements are separate evidence from deterministic
CI and from actual forge outcomes. **Collaboration Validation** runs its native
suite and measurements only when explicitly requested. Ordinary PRs and feature
branch pushes use the deterministic admission flow without this duplicate suite
or capture benchmark.

Request native qualification on main manually:

```bash
gh workflow run collaboration-validation.yml --ref main
```

An authorized maintainer or connector can also create a branch named
`qualification/<purpose>-<short-sha>` pointing to the exact revision to qualify.
Only pushes to `qualification/**` trigger this workflow automatically. Use a
revision containing this opt-in workflow definition. For example, replace both
uppercase placeholders before publishing the qualification branch:

```bash
git push origin FULL_COMMIT_SHA:refs/heads/qualification/PURPOSE-SHORT_SHA
```

Record the run URL and actual checked-out commit with its measurement artifacts.
Creating a qualification branch does not grant approval or admit that revision
to main. Current-head/current-base **PR Validation** remains the admission gate.

## Retired automation maintenance

The retired workflows are the two legacy staging writers, decision-model
admission, periodic promotion, duplicate CLI reviewers, issue solvers/triage,
live decision smoke and scheduled swarm seed. Their definitions are removed from
current main, so those entry points no longer start new automatic promotion or
provider work. Historical findings and failed review attempts remain in PR
history; their absence of usable evidence is not rewritten as a successful review.

Removing a workflow file does not cancel copies already queued or running.
Retirement is explicit maintenance and is not invoked by normal PR admission.
To inspect and stop remaining or restarted copies of the exact retired workflows,
run:

```bash
gh workflow run retired-automation.yml --ref main
```

**Retired Automation Maintenance** executes only trusted main in the canonical
repository, with contents read and Actions write permission. Its existing bounded
helper uses normal cancellation and verifies completion. It retains
`retired-automation.json`, including observed conclusions and any incomplete
inventory. An unconfirmed result fails that maintenance run for inspection and
manual retry; it is never reported as successful cleanup. The maintenance job
cannot publish repository content and does not call a provider.

### Historical queued run

At the November 11, 2026 UTC installation checkpoint,
[maintenance run 43119385184](https://github.com/niklas-olsson/keystone-polyphony/actions/runs/43119385184)
observed seed run `43118087829` already completed successfully, without a
cancellation request. Historical periodic run
[42527277309](https://github.com/niklas-olsson/keystone-polyphony/actions/runs/42527277309)
remained queued with no jobs after ordinary cancellation and one force-cancel
attempt both returned HTTP 409. It is unresolved, not reported as cancelled.
These observations match GitHub's documented
[abnormal workflow-run condition](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows),
which may require GitHub Support to clear. Retain the failed maintenance evidence;
normal owner admission does not depend on indefinite repair of this platform state.

At that checkpoint, main was
`8868577f0ebf673845c6e561f6f99e44f0b6c737` and staging was
`bc38767a320b9e3c6fd2f54d19bf9d0fd64d3ec1`. Staging was an ancestor with zero
commits ahead of main. The old periodic workflow fetches live refs and skips
promotion under that condition. Current main also contained no `.github/issues/`
files, so its separate issue sweep would not invoke the old provider-backed
triage. Use `.github/issue-drafts/` for future unpublished drafts and leave the
legacy watched path empty; no existing draft files were moved or deleted.

This is a recorded input/state boundary, not proof that the historical run can
never resume. Deliberately advancing staging or repopulating its watched issue
path could change its behavior. Keep the orphan visible as a maintenance/support
limitation without treating failed cancellation as completed cleanup.
