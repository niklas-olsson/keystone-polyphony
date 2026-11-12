#!/usr/bin/env python3
"""Qualify an exact proposed merge, then publish that object to main.

This trusted controller never checks out or executes candidate code.
Provider reviews are advisory and dispatched only for external authors.
"""

from collections import deque
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

VALIDATION = ".github/workflows/pr-validation.yml"
EXTERNAL_REVIEW = ".github/workflows/external-pr-review.yml"
CONTROLLER = ".github/workflows/pr-controller.yml"
OBSERVATION_SECONDS = 300
OBSERVATION_LIMIT = 20
OBSERVATION_INTERVAL = 15
STOP_RESULTS = {
    "main published",
    "new-main controller dispatched",
    "current-policy restart required",
}
SHA = re.compile(r"[0-9a-f]{40}")
REQUIRED_STEPS = {
    "Verify selected revisions",
    "Verify checked out merge",
    "Install dependencies",
    "Lint",
    "Test",
}


class Hold(Exception):
    """A visible, recoverable refusal to admit this PR."""


class PolicyMoved(Hold):
    """Restart from new main before interpreting another candidate."""


class ValidationPending(Hold):
    """A correctly attributed validation is explicitly nonterminal."""

    def __init__(self, run):
        self.run = run
        super().__init__(f"Validation {run['id']} is {run['status']}")


def positive_id(value):
    return type(value) is int and value > 0


def sha(value):
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise Hold("Missing or invalid revision")
    return value


class GitHub:
    def __init__(self, repository):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise Hold("Invalid repository identity")
        self.repository = repository

    def request(self, path, *, method="GET", body=None):
        command = ["gh", "api", "--hostname", "github.com", "--method", method, path]
        if body is not None:
            command += ["--input", "-"]
        result = subprocess.run(
            command,
            input=json.dumps(body).encode() if body is not None else None,
            capture_output=True,
            timeout=60,
        )
        if result.returncode:
            raise Hold(f"GitHub {method} failed for {path.split('?')[0]}")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except (ValueError, UnicodeDecodeError):
            raise Hold("Invalid GitHub response") from None

    def api(self, suffix="", **kwargs):
        return self.request(
            f"repos/{self.repository}" + (f"/{suffix}" if suffix else ""), **kwargs
        )

    def pages(self, suffix, key=None):
        result = []
        for page in range(1, 11):
            separator = "&" if "?" in suffix else "?"
            response = self.api(f"{suffix}{separator}per_page=100&page={page}")
            items = (
                response.get(key) if key and isinstance(response, dict) else response
            )
            if not isinstance(items, list):
                raise Hold("Incomplete paginated GitHub evidence")
            if key and response.get("total_count", 0) > 1000:
                raise Hold("GitHub evidence exceeds the 1000-item search limit")
            result.extend(items)
            if len(items) < 100:
                return result
        raise Hold("GitHub evidence exceeds the pagination limit")


def classify_owner(repo, pr):
    owner, author = repo.get("owner", {}), pr.get("user", {})
    if (
        owner.get("type") != "User"
        or not positive_id(owner.get("id"))
        or not positive_id(author.get("id"))
    ):
        raise Hold("Authenticated personal-owner and PR-author identities are required")
    return owner["id"] == author["id"]


def candidate(repo, pr, main_sha, parents):
    owner = classify_owner(repo, pr)
    number = pr.get("number")
    if not positive_id(number) or not positive_id(repo.get("id")):
        raise Hold("Invalid repository or PR identity")
    base = pr.get("base", {})
    base_repo = base.get("repo", {})
    if (
        repo.get("default_branch") != "main"
        or base.get("ref") != "main"
        or base_repo.get("id") != repo["id"]
        or base_repo.get("full_name") != repo.get("full_name")
    ):
        raise Hold("PR must target this repository's main branch")
    if pr.get("state") != "open" or pr.get("draft") is not False:
        raise Hold("PR is closed or draft")
    head_repo = pr.get("head", {}).get("repo") or {}
    if (
        not positive_id(head_repo.get("id"))
        or not isinstance(head_repo.get("full_name"), str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", head_repo["full_name"])
    ):
        raise Hold("PR head repository is unavailable")
    if any(label.get("name") == "hold-merge" for label in pr.get("labels", [])):
        raise Hold("hold-merge is set; remove it when ready")
    if pr.get("mergeable") is not True:
        raise Hold(
            "Mergeability is pending or conflicts need resolution; reconcile after resolving"
        )
    head = sha(pr.get("head", {}).get("sha"))
    base_sha = sha(base.get("sha"))
    merge = sha(pr.get("merge_commit_sha"))
    if sha(main_sha) != base_sha or parents != [base_sha, head]:
        raise Hold(
            "Proposed merge is stale or has unexpected parents; reconcile current main"
        )
    return {
        "pr_number": str(number),
        "head_sha": head,
        "base_sha": base_sha,
        "merge_sha": merge,
        "owner": owner,
        "author_id": pr["user"]["id"],
    }


def run_name(plan, name="PR Validation"):
    return (
        f"{name} #{plan['pr_number']} head={plan['head_sha']} "
        f"base={plan['base_sha']} merge={plan['merge_sha']}"
    )


def workflow_runs(github, path, plan, name):
    workflow = github.api(f"actions/workflows/{path.rsplit('/', 1)[1]}")
    if (
        workflow.get("path") != path
        or workflow.get("state") != "active"
        or not positive_id(workflow.get("id"))
    ):
        raise Hold("Required trusted workflow is unavailable")
    runs = github.pages(
        f"actions/workflows/{workflow['id']}/runs?event=workflow_dispatch&head_sha={plan['base_sha']}",
        "workflow_runs",
    )
    matches = [run for run in runs if run.get("display_title") == run_name(plan, name)]
    if any(not positive_id(run.get("id")) for run in matches):
        raise Hold("Invalid workflow run identity")
    return workflow, sorted(matches, key=lambda run: run["id"], reverse=True)


def validation_run(github, plan):
    workflow, runs = workflow_runs(github, VALIDATION, plan, "PR Validation")
    if not runs:
        return None
    # A rerun replaces the same run's attempt/result. Refetch its latest state.
    run = github.api(f"actions/runs/{runs[0]['id']}")
    if (
        run.get("id") != runs[0]["id"]
        or run.get("workflow_id") != workflow["id"]
        or run.get("event") != "workflow_dispatch"
        or run.get("path", "").split("@", 1)[0] != VALIDATION
        or run.get("repository", {}).get("full_name") != github.repository
        or run.get("head_branch") != "main"
        or run.get("head_sha") != plan["base_sha"]
        or run.get("display_title") != run_name(plan)
        or not positive_id(run.get("run_attempt"))
    ):
        raise Hold(
            "Validation provenance does not match the trusted selected revision set"
        )
    if (
        run.get("status")
        in (
            "queued",
            "in_progress",
            "waiting",
            "pending",
            "requested",
        )
        and run.get("conclusion") is None
    ):
        raise ValidationPending(run)
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise Hold(
            f"Validation {run['id']} is {run.get('conclusion') or run.get('status')}; inspect or rerun that validation"
        )
    jobs = github.pages(
        f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs"
    )
    checks = [job for job in jobs if job.get("name") == "validate"]
    if len(checks) != 1:
        raise Hold("Exactly one actual validate job is required")
    job = checks[0]
    successful_steps = {
        step.get("name")
        for step in job.get("steps", [])
        if step.get("conclusion") == "success"
    }
    if (
        job.get("run_id") != run["id"]
        or job.get("run_attempt") != run["run_attempt"]
        or job.get("head_sha") != plan["base_sha"]
        or job.get("status") != "completed"
        or job.get("conclusion") != "success"
        or not REQUIRED_STEPS.issubset(successful_steps)
    ):
        raise Hold(
            "Validation job or required steps did not succeed on the latest attempt"
        )
    return run


def unresolved_threads(github, number):
    owner, name = github.repository.split("/")
    cursor = None
    query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){
      repository(owner:$owner,name:$name){pullRequest(number:$number){
        reviewThreads(first:100,after:$cursor){nodes{isResolved}
          pageInfo{hasNextPage endCursor}}}}}"""
    for _ in range(10):
        result = github.request(
            "graphql",
            method="POST",
            body={
                "query": query,
                "variables": {
                    "owner": owner,
                    "name": name,
                    "number": number,
                    "cursor": cursor,
                },
            },
        )
        try:
            if result.get("errors"):
                raise Hold("Review thread evidence is unavailable")
            threads = result["data"]["repository"]["pullRequest"]["reviewThreads"]
            if any(item["isResolved"] is not True for item in threads["nodes"]):
                return True
            if threads["pageInfo"]["hasNextPage"] is False:
                return False
            cursor = threads["pageInfo"]["endCursor"]
            if not cursor:
                raise Hold("Review pagination is incomplete")
        except (KeyError, TypeError):
            raise Hold("Review thread evidence is unavailable") from None
    raise Hold("Review threads exceed the pagination limit")


def review_ready(github, repo, pr):
    latest = {}
    for review in github.pages(f"pulls/{pr['number']}/reviews"):
        if review.get("state") not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        user = review.get("user", {}).get("id")
        if not positive_id(user) or not positive_id(review.get("id")):
            raise Hold("Invalid reviewer identity")
        if user not in latest or review["id"] > latest[user]["id"]:
            latest[user] = review
    if any(review["state"] == "CHANGES_REQUESTED" for review in latest.values()):
        raise Hold("An actual changes-requested review remains unresolved")
    if unresolved_threads(github, pr["number"]):
        raise Hold("An actual review thread remains unresolved")
    if not classify_owner(repo, pr):
        approval = latest.get(repo["owner"]["id"], {})
        if (
            approval.get("state") != "APPROVED"
            or approval.get("commit_id") != pr["head"]["sha"]
        ):
            raise Hold(
                "External PR awaits repository-owner approval on the current head"
            )


def dispatch(github, path, plan=None):
    body = {"ref": "main"}
    if plan is not None:
        body["inputs"] = {
            key: plan[key] for key in ("pr_number", "head_sha", "base_sha", "merge_sha")
        }
    github.api(
        f"actions/workflows/{path.rsplit('/', 1)[1]}/dispatches",
        method="POST",
        body=body,
    )


def main_sha(github):
    return sha(github.api("git/ref/heads/main")["object"]["sha"])


def snapshot(github, repo, number):
    # GitHub can still be calculating a synthetic merge after the event.
    for attempt in range(4):
        pr = github.api(f"pulls/{number}")
        main = main_sha(github)
        if pr.get("mergeable") is None or not pr.get("merge_commit_sha"):
            if attempt < 3:
                time.sleep(1)
                continue
            raise Hold(
                "GitHub is still calculating the merge; run reconciliation again"
            )
        merge = sha(pr.get("merge_commit_sha"))
        commit = github.api(f"git/commits/{merge}")
        if commit.get("sha") != merge:
            raise Hold("Merge object identity mismatch")
        parents = [item.get("sha") for item in commit.get("parents", [])]
        if parents != [main, pr.get("head", {}).get("sha")] and attempt < 3:
            time.sleep(1)
            continue
        return pr, candidate(repo, pr, main, parents)


def reserve_validation(github, plan, retry_missing=False):
    marker = (
        f"<!-- polyphony-validation:{plan['head_sha']}:{plan['base_sha']}:"
        f"{plan['merge_sha']} -->"
    )
    comments = github.pages(f"issues/{plan['pr_number']}/comments")
    previous = [
        item
        for item in comments
        if item.get("user", {}).get("id") == 41898282
        and item.get("body", "").startswith(marker)
    ]
    if previous and not retry_missing:
        raise Hold(
            "Validation dispatch recorded but no run visible; use manual retry_missing_validation_pr after checking Actions"
        )
    body = (
        marker
        + "\nDeterministic validation requested for this exact proposed merge.\n\n"
        f"Head `{plan['head_sha']}`; main `{plan['base_sha']}`; merge `{plan['merge_sha']}`.\n\n"
        "No model review is required for an owner-authored PR. See Actions > PR Validation. "
        "If no run appears, use Direct Main Admission's manual retry_missing_validation_pr "
        "input for this PR. A recorded dispatch is not passing test evidence."
    )
    github.api(
        f"issues/{plan['pr_number']}/comments", method="POST", body={"body": body}
    )


def external_attempt_exists(github, plan):
    marker = (
        f"<!-- polyphony-external-review:v1 pr={plan['pr_number']} "
        f"head={plan['head_sha']} -->\n"
    )
    for item in github.pages(f"issues/{plan['pr_number']}/comments"):
        if item.get("user", {}).get("id") == 41898282 and item.get(
            "body", ""
        ).startswith(marker):
            return True
    workflow = github.api("actions/workflows/external-pr-review.yml")
    if workflow.get("path") != EXTERNAL_REVIEW or not positive_id(workflow.get("id")):
        raise Hold("External review workflow is unavailable")
    runs = github.pages(
        f"actions/workflows/{workflow['id']}/runs?event=workflow_dispatch",
        "workflow_runs",
    )
    prefix = f"External PR Review #{plan['pr_number']} head={plan['head_sha']} base="
    return any(run.get("display_title", "").startswith(prefix) for run in runs)


def git(*args, check=True):
    result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=90)
    if check and result.returncode:
        raise Hold("Git object verification or publication failed")
    return result


def prepare_object(plan):
    merge, base, head = (
        sha(plan[key]) for key in ("merge_sha", "base_sha", "head_sha")
    )
    git("fetch", "--no-tags", "--no-recurse-submodules", "origin", merge)
    if git("show", "-s", "--format=%P", merge).stdout.strip().split() != [base, head]:
        raise Hold("Fetched merge object has unexpected parents")
    if git("merge-base", "--is-ancestor", base, merge, check=False).returncode:
        raise Hold("Publication must be a fast-forward from the selected main")


def publish(github, plan):
    # The lease matters even if concurrent main is an ancestor of this merge.
    diagnostic = "Git publication outcome is unknown"
    try:
        result = git(
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!gh auth git-credential",
            "push",
            f"--force-with-lease=refs/heads/main:{plan['base_sha']}",
            "origin",
            f"{plan['merge_sha']}:refs/heads/main",
            check=False,
        )
        push_ok = result.returncode == 0
        if not push_ok:
            stderr = (result.stderr or "").lower()
            if (
                "refusing to allow a github app" in stderr
                and "without `workflows` permission" in stderr
            ):
                diagnostic = "GitHub refused workflow-file changes by the job credential; a maintainer with workflow permission must publish the tested merge"
            elif "stale info" in stderr:
                diagnostic = "Git rejected the selected-main lease"
            elif "gh006" in stderr or "gh013" in stderr:
                diagnostic = "GitHub branch policy refused publication"
            else:
                diagnostic = "Git refused publication; inspect repository write permission and branch policy"
    except subprocess.TimeoutExpired:
        push_ok = False
    # Reconcile a lost push acknowledgement against the original exact object.
    actual_main = main_sha(github)
    if actual_main != plan["merge_sha"]:
        raise Hold(
            f"{diagnostic}. Main publication was not confirmed at the tested merge; reconcile current main"
        )
    pr = github.api(f"pulls/{plan['pr_number']}")
    merged = (
        pr.get("merged") is True and pr.get("head", {}).get("sha") == plan["head_sha"]
    )
    return {"main_sha": actual_main, "pr_merged": merged, "push_acknowledged": push_ok}


def reconcile(
    github, repo, number, *, policy_sha=None, retry_missing=False, expected_plan=None
):
    pr, plan = snapshot(github, repo, number)
    if policy_sha is not None and plan["base_sha"] != policy_sha:
        raise PolicyMoved("Main changed; restart the controller from current policy")
    if expected_plan is not None and plan != expected_plan:
        raise Hold(
            "PR changed while validation was pending; reconcile the new candidate"
        )
    try:
        run = validation_run(github, plan)
    except ValidationPending as error:
        if expected_plan is not None:
            raise
        return {
            "pr": number,
            "status": "validation pending",
            "reason": str(error),
            "plan": plan,
            "validation_run": error.run["id"],
            "validation_attempt": error.run["run_attempt"],
        }
    if run is None:
        if expected_plan is not None:
            raise Hold("Observed validation is no longer visible; inspect Actions")
        reserve_validation(github, plan, retry_missing)
        dispatch(github, VALIDATION, plan)
        return {"pr": number, "status": "validation dispatched", "plan": plan}
    advisory = "not applicable: owner PR"
    if not plan["owner"]:
        try:
            if not external_attempt_exists(github, plan):
                dispatch(github, EXTERNAL_REVIEW, plan)
                advisory = "external advisory dispatched"
            else:
                advisory = "external head already attempted"
        except (Hold, subprocess.TimeoutExpired) as error:
            # The provider is advisory. Missing model infrastructure cannot
            # override or replace the independent owner's actual approval.
            advisory = f"external advisory unavailable: {error}"
            print(json.dumps({"pr": number, "advisory": advisory}), flush=True)
    review_ready(github, repo, pr)
    prepare_object(plan)
    fresh_repo = github.api()
    fresh_pr, fresh_plan = snapshot(github, fresh_repo, number)
    if policy_sha is not None and fresh_plan["base_sha"] != policy_sha:
        raise PolicyMoved("Main changed before publication; restart current policy")
    if fresh_plan != plan or fresh_repo.get("id") != repo.get("id"):
        raise Hold(
            "PR or repository changed before publication; reconcile the new candidate"
        )
    review_ready(github, fresh_repo, fresh_pr)
    final_run = validation_run(github, plan)
    if final_run is None or (final_run["id"], final_run["run_attempt"]) != (
        run["id"],
        run["run_attempt"],
    ):
        raise Hold("Validation changed before publication; inspect its latest attempt")
    try:
        result = publish(github, plan)
    except (Hold, subprocess.TimeoutExpired) as error:
        reason = (
            "Publication confirmation timed out; reconcile actual main and PR state"
            if isinstance(error, subprocess.TimeoutExpired)
            else str(error)
        )
        return {
            "pr": number,
            "status": "held",
            "reason": reason,
            "plan": plan,
            "validation_run": run["id"],
            "validation_attempt": run["run_attempt"],
            "advisory": advisory,
        }
    return {
        "pr": number,
        "status": "main published",
        "plan": plan,
        "validation_run": run["id"],
        "validation_attempt": run["run_attempt"],
        "advisory": advisory,
        **result,
    }


def observe_validations(github, pending):
    """Yield qualified plans without reserving, dispatching or publishing work.

    One fair queue shares 20 probes and a 300-second scheduling budget. Existing
    API timeouts may finish after the deadline; the job's outer limit is 10 minutes.
    """
    queue = deque(
        result
        for result in pending
        if result["status"] in {"validation dispatched", "validation pending"}
    )
    deadline = time.monotonic() + OBSERVATION_SECONDS
    for _ in range(OBSERVATION_LIMIT):
        if not queue or time.monotonic() + OBSERVATION_INTERVAL > deadline:
            break
        time.sleep(OBSERVATION_INTERVAL)
        if time.monotonic() > deadline:
            break
        result = queue.popleft()
        result["validation_observations"] = result.get("validation_observations", 0) + 1
        try:
            run = validation_run(github, result["plan"])
            if run is None:
                if result["status"] != "validation dispatched":
                    raise Hold(
                        "Previously visible validation disappeared; inspect Actions"
                    )
                queue.append(result)
                continue
        except ValidationPending as error:
            result.update(
                status="validation pending",
                reason=str(error),
                validation_run=error.run["id"],
                validation_attempt=error.run["run_attempt"],
            )
            queue.append(result)
            continue
        except (Hold, subprocess.TimeoutExpired) as error:
            result.update(
                status="held",
                reason=(
                    "Validation observation timed out; inspect Actions"
                    if isinstance(error, subprocess.TimeoutExpired)
                    else str(error)
                ),
            )
            print(json.dumps(result, sort_keys=True), flush=True)
            continue
        result.update(
            status="validation qualified",
            validation_run=run["id"],
            validation_attempt=run["run_attempt"],
        )
        result.pop("reason", None)
        yield result
    for result in queue:
        result.update(
            status="held",
            reason="Validation observation budget exhausted; inspect Actions and reconcile",
            observation_budget_exhausted=True,
        )
        print(json.dumps(result, sort_keys=True), flush=True)


def reconciliation_result(github, repo, number, **options):
    try:
        result = reconcile(github, repo, number, **options)
    except PolicyMoved as error:
        try:
            dispatch(github, CONTROLLER)
        except (Hold, subprocess.TimeoutExpired) as restart_error:
            reason = (
                "successor dispatch acknowledgement timed out; inspect Actions before retry"
                if isinstance(restart_error, subprocess.TimeoutExpired)
                else str(restart_error)
            )
            return {
                "pr": number,
                "status": "current-policy restart required",
                "reason": str(error),
                "followup": f"manual reconciliation required: {reason}",
            }
        return {
            "pr": number,
            "status": "new-main controller dispatched",
            "reason": str(error),
        }
    except (Hold, subprocess.TimeoutExpired) as error:
        return {
            "pr": number,
            "status": "held",
            "reason": (
                "Admission request timed out; inspect Actions and reconcile"
                if isinstance(error, subprocess.TimeoutExpired)
                else str(error)
            ),
        }
    if result["status"] == "main published":
        # GITHUB_TOKEN pushes do not trigger push workflows. New policy must
        # handle all remaining candidates after this exact merge is published.
        try:
            dispatch(github, CONTROLLER)
            result["followup"] = "new-main controller dispatched"
        except (Hold, subprocess.TimeoutExpired) as error:
            reason = (
                "successor dispatch acknowledgement timed out; inspect Actions before retry"
                if isinstance(error, subprocess.TimeoutExpired)
                else str(error)
            )
            result["followup"] = f"manual reconciliation required: {reason}"
    return result


def main():
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise Hold("Controller must execute from trusted main")
    github = GitHub(os.environ["GITHUB_REPOSITORY"])
    repo = github.api()
    policy_sha = sha(git("rev-parse", "HEAD").stdout.strip())
    if policy_sha != main_sha(github):
        dispatch(github, CONTROLLER)
        raise Hold(
            "Trusted main moved before startup; current-policy reconciliation requested"
        )
    retry_pr = os.environ.get("RETRY_MISSING_VALIDATION_PR", "")
    if retry_pr and not re.fullmatch(r"[1-9][0-9]*", retry_pr):
        raise Hold("retry_missing_validation_pr must be a positive PR number")
    results = []
    prs = github.pages("pulls?state=open&base=main&sort=created&direction=asc")
    for pr in prs:
        number = pr.get("number")
        if not positive_id(number):
            raise Hold("Invalid PR number in repository response")
        if pr.get("draft") is True:
            continue
        result = reconciliation_result(
            github,
            repo,
            number,
            policy_sha=policy_sha,
            retry_missing=retry_pr == str(number),
        )
        results.append(result)
        print(json.dumps(result, sort_keys=True), flush=True)
        if result["status"] in STOP_RESULTS:
            break
    else:
        # Consider every ready PR before waiting, so queued work cannot delay an
        # already-qualified candidate. Observation itself performs only reads.
        for waiting in observe_validations(github, results):
            result = reconciliation_result(
                github,
                repo,
                waiting["pr"],
                policy_sha=policy_sha,
                expected_plan=waiting["plan"],
            )
            waiting.update(result)
            print(json.dumps(waiting, sort_keys=True), flush=True)
            if result["status"] in STOP_RESULTS:
                break
    report = {"repository": github.repository, "results": results}
    Path("admission-results.json").write_text(json.dumps(report, indent=2) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as output:
            output.write(
                "## Direct main admission\n\n```json\n"
                + json.dumps(report, indent=2)
                + "\n```\n"
            )


if __name__ == "__main__":
    try:
        main()
    except (Hold, KeyError, subprocess.TimeoutExpired) as error:
        print(f"Admission held: {error}", file=sys.stderr)
        sys.exit(1)
