#!/usr/bin/env python3
"""Stop the explicitly retired automation before admitting new canonical PRs."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import time

SPEC = importlib.util.spec_from_file_location(
    "retirement_controller", Path(__file__).with_name("pr-controller.py")
)
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)
Hold = controller.Hold
REPOSITORY = "niklas-olsson/keystone-polyphony"
RETIRED = frozenset(
    f".github/workflows/{name}.yml"
    for name in (
        "agent-issue-solver",
        "agent-issue-triage",
        "agent-parallel-solver",
        "auto-merge-staging",
        "decision-merge",
        "decision-model-smoke",
        "opencode",
        "periodic-merge-main",
        "swarm-node",
        "workflow-review-dispatch",
        "workflow-review",
    )
)
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
MAX_TARGETS = 25


class APIUnavailable(Hold):
    """A request failed without supplying usable metadata."""


class InventoryIncomplete(Hold):
    """Known valid targets may be stopped, but admission remains unqualified."""


class GitHub(controller.GitHub):
    """Bound this small cleanup independently of the enclosing job timeout."""

    def __init__(self, repository):
        super().__init__(repository)
        self.deadline = time.monotonic() + 120
        self.requests = 0

    def request(self, path, *, method="GET", body=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.requests >= 150:
            raise Hold("Retirement API budget exhausted; run reconciliation again")
        self.requests += 1
        command = ["gh", "api", "--hostname", "github.com", "--method", method, path]
        if body is not None:
            command += ["--input", "-"]
        result = subprocess.run(
            command,
            input=json.dumps(body).encode() if body is not None else None,
            capture_output=True,
            timeout=min(10, remaining),
        )
        if result.returncode:
            raise APIUnavailable("Retirement GitHub request is unavailable")
        if len(result.stdout) > 4 * 1024 * 1024:
            raise Hold("Retirement GitHub response exceeded its response limit")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except (ValueError, UnicodeDecodeError):
            raise Hold("Retirement GitHub response was not valid JSON") from None


def current_policy(github, policy_sha):
    ref = github.api("git/ref/heads/main")
    if (
        not isinstance(ref, dict)
        or not isinstance(ref.get("object"), dict)
        or ref["object"].get("sha") != policy_sha
    ):
        raise Hold("Main changed; restart retirement from current trusted policy")


def trusted_context(github, environment, policy_sha):
    if (
        environment.get("GITHUB_REPOSITORY") != REPOSITORY
        or environment.get("GITHUB_REF") != "refs/heads/main"
        or environment.get("GITHUB_EVENT_NAME")
        not in {"push", "pull_request_target", "workflow_run", "workflow_dispatch"}
        or not re.fullmatch(r"[1-9][0-9]*", environment.get("GITHUB_REPOSITORY_ID", ""))
    ):
        raise Hold("Retirement is restricted to the canonical trusted-main workflow")
    controller.sha(policy_sha)
    repo = github.api()
    if (
        not isinstance(repo, dict)
        or not controller.positive_id(repo.get("id"))
        or repo["id"] != int(environment["GITHUB_REPOSITORY_ID"])
        or repo.get("full_name") != REPOSITORY
        or repo.get("default_branch") != "main"
    ):
        raise Hold("Canonical repository identity could not be verified")
    current_policy(github, policy_sha)
    return repo["id"]


def listed(github, suffix, key, evidence):
    """Return bounded pages and whether their declared inventory is complete."""
    items, identities, totals = [], set(), set()
    for page in range(1, 11):
        separator = "&" if "?" in suffix else "?"
        response = github.api(f"{suffix}{separator}per_page=100&page={page}")
        if not isinstance(response, dict):
            raise Hold("Retirement inventory response is malformed")
        batch, count = response.get(key), response.get("total_count")
        if (
            not isinstance(batch, list)
            or len(batch) > 100
            or type(count) is not int
            or not 0 <= count <= 1000
        ):
            raise Hold("Retirement inventory is malformed or exceeds its search limit")
        for item in batch:
            if not isinstance(item, dict) or not controller.positive_id(item.get("id")):
                raise Hold("Retirement inventory contains a malformed identity")
            identities.add(item["id"])
        totals.add(count)
        items.extend(batch)
        if len(batch) < 100:
            complete = len(totals) == 1 and len(identities) == len(items) == count
            if not complete:
                evidence.append(
                    {
                        "query": suffix,
                        "declared_totals": sorted(totals),
                        "returned_rows": len(items),
                        "unique_ids": len(identities),
                        "complete": False,
                    }
                )
            return items, complete
    raise Hold("Retirement inventory exceeds its pagination limit")


def verified_run(run, repository_id, expected=None):
    if not isinstance(run, dict):
        raise Hold("Workflow run identity is malformed")
    repo = run.get("repository")
    if (
        not isinstance(repo, dict)
        or not controller.positive_id(repo.get("id"))
        or repo.get("id") != repository_id
        or repo.get("full_name") != REPOSITORY
        or not controller.positive_id(run.get("id"))
        or not controller.positive_id(run.get("workflow_id"))
        or not controller.positive_id(run.get("run_attempt"))
        or not isinstance(run.get("path"), str)
        or not run["path"]
        or run.get("status") not in {*ACTIVE, "completed"}
        or run.get("conclusion")
        not in {
            None,
            "success",
            "failure",
            "cancelled",
            "neutral",
            "skipped",
            "stale",
            "timed_out",
            "action_required",
            "startup_failure",
        }
    ):
        raise Hold("Workflow run repository or execution identity is invalid")
    controller.sha(run.get("head_sha"))
    if expected and any(
        run.get(key) != expected[key]
        for key in ("id", "path", "workflow_id", "head_sha")
    ):
        raise Hold("Workflow run identity changed during retirement")
    return run


def inventory(github, repository_id, evidence, *, allow_incomplete=False):
    targets, uncertain = {}, []

    def collect(items, expected_path=None, expected_id=None):
        for item in items:
            run = verified_run(item, repository_id)
            if expected_path and (
                run["path"] != expected_path or run["workflow_id"] != expected_id
            ):
                raise Hold("Scoped retirement inventory returned another workflow")
            if run["path"] in RETIRED:
                if run["id"] in targets:
                    verified_run(run, repository_id, targets[run["id"]])
                targets[run["id"]] = run
                if len(targets) > MAX_TARGETS:
                    raise Hold("Retired runs exceed the bounded cancellation batch")

    for status in ACTIVE:
        query = f"actions/runs?status={status}"
        items, complete = listed(github, query, "workflow_runs", evidence)
        collect(items)
        if not complete:
            items, complete = listed(github, query, "workflow_runs", evidence)
            collect(items)
            evidence.append({"query": query, "retry_complete": complete})
        if not complete:
            uncertain.append(status)
    if uncertain:
        query = "actions/workflows"
        try:
            workflows, complete = listed(github, query, "workflows", evidence)
            if not complete:
                raise InventoryIncomplete(
                    f"Workflow registry remains incomplete: {query}"
                )
            scopes = {path: set() for path in RETIRED}
            for workflow in workflows:
                if not isinstance(workflow.get("path"), str):
                    raise Hold("Retired workflow registry identity is invalid")
                if workflow["path"] in RETIRED:
                    scopes[workflow["path"]].add(workflow["id"])
            for run in targets.values():
                scopes[run["path"]].add(run["workflow_id"])
            for path, ids in sorted(scopes.items()):
                if not ids:
                    # Deleted metadata may still be addressable by filename.
                    query = f"actions/workflows/{path.rsplit('/', 1)[1]}"
                    workflow = github.api(query)
                    if (
                        not isinstance(workflow, dict)
                        or workflow.get("path") != path
                        or not controller.positive_id(workflow.get("id"))
                    ):
                        raise Hold("Retired workflow lookup identity is invalid")
                    ids.add(workflow["id"])
                for workflow_id in sorted(ids):
                    for status in uncertain:
                        query = f"actions/workflows/{workflow_id}/runs?status={status}"
                        items, complete = listed(
                            github, query, "workflow_runs", evidence
                        )
                        collect(items, path, workflow_id)
                        evidence.append({"query": query, "complete": complete})
                        if not complete:
                            raise InventoryIncomplete(
                                f"Retired workflow inventory remains incomplete: {query}"
                            )
        except (
            InventoryIncomplete,
            APIUnavailable,
            subprocess.TimeoutExpired,
        ) as error:
            reason = (
                str(error)
                if isinstance(error, InventoryIncomplete)
                else f"Retired workflow scoping is unavailable: {query}"
            )
            evidence.append({"query": query, "unresolved": reason})
            if not allow_incomplete:
                raise InventoryIncomplete(reason) from None
    return targets


def retire(github, repository_id, policy_sha, report):
    targets = inventory(
        github, repository_id, report["inventory"], allow_incomplete=True
    )
    for run in sorted(targets.values(), key=lambda item: item["id"]):
        result = {
            "run_id": run["id"],
            "workflow_path": run["path"],
            "head_sha": run["head_sha"],
            "cancel_requested": False,
            "cancel_acknowledged": False,
            "status": "unverified",
        }
        report["runs"].append(result)
        path = f"actions/runs/{run['id']}"
        fresh = verified_run(github.api(path), repository_id, run)
        if fresh["status"] != "completed":
            current_policy(github, policy_sha)
            result["cancel_requested"] = True
            try:
                github.api(path + "/cancel", method="POST")
                result["cancel_acknowledged"] = True
            except (Hold, subprocess.TimeoutExpired):
                # An API failure can be a lost acknowledgement or completion race.
                pass
            for attempt in range(4):
                try:
                    fresh = verified_run(github.api(path), repository_id, run)
                    if fresh["status"] == "completed":
                        break
                except (Hold, subprocess.TimeoutExpired):
                    fresh = None
                if attempt < 3:
                    time.sleep(1)
        if fresh is not None:
            result.update(
                status=fresh["status"],
                run_attempt=fresh["run_attempt"],
                conclusion=fresh.get("conclusion"),
            )
        if result["status"] != "completed":
            raise Hold(
                f"Retired run {run['id']} completion is unconfirmed; reconcile again"
            )
    # Re-read active state after mutations; a new/late retired run holds admission.
    final = inventory(github, repository_id, report["inventory"])
    for run in final.values():
        fresh = verified_run(
            github.api(f"actions/runs/{run['id']}"), repository_id, run
        )
        if fresh["status"] != "completed":
            raise Hold(f"Retired run {run['id']} remains active after final inventory")
    current_policy(github, policy_sha)
    report.update(status="clear", reason="No matching retired active run remains")


def main():
    report = {
        "schema": 1,
        "repository": REPOSITORY,
        "status": "held",
        "runs": [],
        "inventory": [],
    }
    try:
        github = GitHub(REPOSITORY)
        policy_sha = controller.sha(controller.git("rev-parse", "HEAD").stdout.strip())
        report["policy_sha"] = policy_sha
        repository_id = trusted_context(github, os.environ, policy_sha)
        retire(github, repository_id, policy_sha, report)
    except Hold as error:
        report["reason"] = str(error)
    except (subprocess.TimeoutExpired, OSError):
        report["reason"] = (
            "Retirement request unavailable or timed out; reconcile again"
        )
    text = json.dumps(report, indent=2) + "\n"
    Path("retired-automation.json").write_text(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
            output.write("## Retired automation\n\n```json\n" + text + "```\n")
    print(text, end="")
    return 0 if report["status"] == "clear" else 1


if __name__ == "__main__":
    raise SystemExit(main())
