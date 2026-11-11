"""Deterministic retirement fixtures; never call GitHub or a provider."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location(
    "retire_automation", Path(__file__).parents[1] / "scripts/retire-automation.py"
)
retirement = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retirement)
BASE, HEAD = "a" * 40, "b" * 40
REPOSITORY_ID = 1164123634
PATH = ".github/workflows/periodic-merge-main.yml"


def run(run_id=42527277309, status="queued", path=PATH):
    return {
        "id": run_id,
        "path": path,
        "workflow_id": 237375600,
        "head_sha": HEAD,
        "run_attempt": 1,
        "repository": {"id": REPOSITORY_ID, "full_name": retirement.REPOSITORY},
        "status": status,
        "conclusion": "success" if status == "completed" else None,
    }


def environment():
    return {
        "GITHUB_REPOSITORY": retirement.REPOSITORY,
        "GITHUB_REPOSITORY_ID": str(REPOSITORY_ID),
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_run",
    }


def report():
    return {"status": "held", "runs": [], "inventory": []}


class Fixture:
    def __init__(self, runs=None):
        self.runs = {item["id"]: deepcopy(item) for item in runs or []}
        self.calls = []
        self.cancel_error = None
        self.finish_on_cancel = True
        self.before_get = None
        self.main = BASE
        self.incomplete_status = None
        self.scoped_incomplete = False
        self.workflows = [
            {"id": 500 + index, "path": path}
            for index, path in enumerate(sorted(retirement.RETIRED))
        ]
        for item in self.workflows:
            if item["path"] == PATH:
                item["id"] = 237375600

    def api(self, suffix="", *, method="GET", body=None):
        self.calls.append((method, suffix))
        if not suffix:
            return {
                "id": REPOSITORY_ID,
                "full_name": retirement.REPOSITORY,
                "default_branch": "main",
            }
        if suffix == "git/ref/heads/main":
            return {"object": {"sha": self.main}}
        if suffix.startswith("actions/runs/") and "?" not in suffix:
            run_id = int(suffix.split("/")[2])
            if suffix.endswith("/cancel"):
                assert method == "POST"
                if self.finish_on_cancel:
                    self.runs[run_id]["status"] = "completed"
                    self.runs[run_id]["conclusion"] = "cancelled"
                if self.cancel_error:
                    raise self.cancel_error
                return None
            assert method == "GET"
            if self.before_get:
                self.before_get(self, run_id)
            return deepcopy(self.runs[run_id])
        if suffix.startswith("actions/workflows?"):
            return {
                "total_count": len(self.workflows),
                "workflows": deepcopy(self.workflows),
            }
        if "runs?status=" in suffix:
            status = suffix.split("status=", 1)[1].split("&", 1)[0]
            page = int(suffix.rsplit("page=", 1)[1])
            items = [item for item in self.runs.values() if item["status"] == status]
            scoped = suffix.startswith("actions/workflows/")
            if scoped:
                workflow_id = int(suffix.split("/")[2])
                items = [item for item in items if item["workflow_id"] == workflow_id]
            count = len(items)
            if (not scoped and status == self.incomplete_status) or (
                scoped and self.scoped_incomplete
            ):
                count += 1
            return {
                "total_count": count,
                "workflow_runs": deepcopy(items[(page - 1) * 100 : page * 100]),
            }
        raise AssertionError((method, suffix, body))


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(retirement.time, "sleep", lambda _: None)


def test_known_queued_legacy_run_is_cancelled_and_freshly_completed():
    github = Fixture([run()])
    result = report()
    retirement.retire(github, REPOSITORY_ID, BASE, result)
    assert result["status"] == "clear"
    assert result["runs"] == [
        {
            "run_id": 42527277309,
            "workflow_path": PATH,
            "head_sha": HEAD,
            "cancel_requested": True,
            "cancel_acknowledged": True,
            "status": "completed",
            "run_attempt": 1,
            "conclusion": "cancelled",
        }
    ]
    assert [call for call in github.calls if call[0] == "POST"] == [
        ("POST", "actions/runs/42527277309/cancel")
    ]
    assert all("force" not in suffix for _, suffix in github.calls)


@pytest.mark.parametrize("status", retirement.ACTIVE)
def test_every_active_status_is_examined(status):
    github = Fixture([run(status=status)])
    retirement.retire(github, REPOSITORY_ID, BASE, report())
    assert github.runs[42527277309]["status"] == "completed"


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/pr-controller.yml",
        ".github/workflows/pr-validation.yml",
        PATH + "@main",
        ".github/workflows/periodic-merge-main.yaml",
    ],
)
def test_unrelated_or_similar_paths_are_never_cancelled(path):
    github = Fixture([run(path=path)])
    retirement.retire(github, REPOSITORY_ID, BASE, report())
    assert all(method == "GET" for method, _ in github.calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", True),
        ("workflow_id", 0),
        ("run_attempt", None),
        ("head_sha", "invalid"),
        ("repository", {"id": REPOSITORY_ID, "full_name": "other/repo"}),
    ],
)
def test_invalid_identity_refuses_before_mutation(field, value):
    item = run()
    item[field] = value
    github = Fixture([item])
    with pytest.raises(retirement.Hold):
        retirement.retire(github, REPOSITORY_ID, BASE, report())
    assert all(method == "GET" for method, _ in github.calls)


def test_identity_change_on_fresh_read_cannot_cancel_another_run():
    github = Fixture([run()])
    github.before_get = lambda current, run_id: current.runs[run_id].update(
        path=".github/workflows/pr-controller.yml"
    )
    with pytest.raises(retirement.Hold, match="identity changed"):
        retirement.retire(github, REPOSITORY_ID, BASE, report())
    assert all(method == "GET" for method, _ in github.calls)


def test_completed_race_needs_no_cancellation():
    github = Fixture([run()])
    github.before_get = lambda current, run_id: current.runs[run_id].update(
        status="completed", conclusion="success"
    )
    result = report()
    retirement.retire(github, REPOSITORY_ID, BASE, result)
    assert result["status"] == "clear"
    assert result["runs"][0]["cancel_requested"] is False
    assert result["runs"][0]["conclusion"] == "success"


@pytest.mark.parametrize(
    "error",
    [retirement.Hold("Fixture lost reply"), subprocess.TimeoutExpired("gh", 10)],
)
def test_ambiguous_cancel_reply_reconciles_completion_without_second_cancel(error):
    github = Fixture([run()])
    github.cancel_error = error
    result = report()
    retirement.retire(github, REPOSITORY_ID, BASE, result)
    assert (
        result["status"] == "clear"
        and result["runs"][0]["cancel_acknowledged"] is False
    )
    assert len([call for call in github.calls if call[0] == "POST"]) == 1


def test_unconfirmed_cancellation_has_bounded_reads_and_holds():
    github = Fixture([run()])
    github.finish_on_cancel = False
    result = report()
    with pytest.raises(retirement.Hold, match="completion is unconfirmed"):
        retirement.retire(github, REPOSITORY_ID, BASE, result)
    assert result["runs"][0]["status"] == "queued"
    assert (
        len(
            [
                call
                for call in github.calls
                if call == ("GET", "actions/runs/42527277309")
            ]
        )
        == 5
    )


def test_policy_change_before_cancel_prevents_mutation():
    github = Fixture([run()])
    github.main = HEAD
    with pytest.raises(retirement.Hold, match="Main changed"):
        retirement.retire(github, REPOSITORY_ID, BASE, report())
    assert all(method == "GET" for method, _ in github.calls)


def test_pagination_finds_retired_run_after_unrelated_first_page():
    others = [
        run(index + 1, path=".github/workflows/pr-validation.yml")
        for index in range(100)
    ]
    github = Fixture(others + [run()])
    retirement.retire(github, REPOSITORY_ID, BASE, report())
    assert ("GET", "actions/runs?status=queued&per_page=100&page=2") in github.calls
    assert len([call for call in github.calls if call[0] == "POST"]) == 1


def test_unrelated_count_race_is_scoped_to_retired_workflows():
    github = Fixture([run()])
    github.incomplete_status = "queued"
    result = report()
    retirement.retire(github, REPOSITORY_ID, BASE, result)
    assert result["status"] == "clear"
    assert any(
        item.get("complete") is True and "workflows/" in item["query"]
        for item in result["inventory"]
    )


def test_unresolved_scoped_count_mismatch_is_a_concrete_hold():
    github = Fixture([run()])
    github.incomplete_status = "queued"
    github.scoped_incomplete = True
    result = report()
    with pytest.raises(
        retirement.Hold,
        match="Retired workflow inventory remains incomplete: actions/workflows/",
    ):
        retirement.retire(github, REPOSITORY_ID, BASE, result)
    assert [call for call in github.calls if call[0] == "POST"] == [
        ("POST", "actions/runs/42527277309/cancel")
    ]
    assert result["status"] == "held" and result["runs"][0]["conclusion"] == "cancelled"
    assert "actions/workflows/" in result["inventory"][-1]["unresolved"]


@pytest.mark.parametrize("changed_total", [False, True])
def test_duplicate_or_changing_page_counts_cannot_conceal_retired_run(
    monkeypatch, changed_total
):
    others = [
        run(index + 1, path=".github/workflows/pr-validation.yml")
        for index in range(102 if changed_total else 101)
    ]
    for item in others:
        item["workflow_id"] = 999
    github = Fixture(others + [run()])
    original = github.api
    incomplete = others if changed_total else others + [others[0]]

    def incomplete_pages(suffix="", **kwargs):
        if suffix.startswith("actions/runs?status=queued&"):
            page = int(suffix.rsplit("page=", 1)[1])
            return {
                "total_count": 103 if changed_total and page == 1 else 102,
                "workflow_runs": deepcopy(incomplete[(page - 1) * 100 : page * 100]),
            }
        return original(suffix, **kwargs)

    monkeypatch.setattr(github, "api", incomplete_pages)
    result = report()
    retirement.retire(github, REPOSITORY_ID, BASE, result)
    assert github.runs[42527277309]["status"] == "completed"
    assert [call for call in github.calls if call[0] == "POST"] == [
        ("POST", "actions/runs/42527277309/cancel")
    ]
    assert any(
        item.get("returned_rows") == 102 and item.get("complete") is False
        for item in result["inventory"]
    )


def test_unavailable_registry_still_cancels_verified_target_then_holds(monkeypatch):
    github = Fixture([run()])
    github.incomplete_status = "queued"
    original = github.api

    def unavailable(suffix="", **kwargs):
        if suffix.startswith("actions/workflows?"):
            raise retirement.APIUnavailable("Fixture registry unavailable")
        return original(suffix, **kwargs)

    monkeypatch.setattr(github, "api", unavailable)
    result = report()
    with pytest.raises(retirement.InventoryIncomplete, match="actions/workflows"):
        retirement.retire(github, REPOSITORY_ID, BASE, result)
    assert result["runs"][0]["status"] == "completed"
    assert len([call for call in github.calls if call[0] == "POST"]) == 1


def test_malformed_registry_identity_remains_a_hard_stop():
    github = Fixture([run()])
    github.incomplete_status = "queued"
    github.workflows[0]["id"] = True
    with pytest.raises(retirement.Hold, match="malformed identity"):
        retirement.retire(github, REPOSITORY_ID, BASE, report())
    assert all(method == "GET" for method, _ in github.calls)


def test_new_retired_run_in_final_inventory_is_not_ignored():
    github = Fixture([run()])

    def add_after_completion(current, run_id):
        if current.runs[run_id]["status"] == "completed":
            current.runs[99] = run(99)

    github.before_get = add_after_completion
    with pytest.raises(retirement.Hold, match="remains active after final inventory"):
        retirement.retire(github, REPOSITORY_ID, BASE, report())
    assert [call for call in github.calls if call[0] == "POST"] == [
        ("POST", "actions/runs/42527277309/cancel")
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("GITHUB_REPOSITORY", "other/repo"),
        ("GITHUB_REPOSITORY_ID", "1"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_EVENT_NAME", "pull_request"),
    ],
)
def test_trusted_main_context_is_required(field, value):
    env = environment()
    env[field] = value
    github = Fixture()
    with pytest.raises(retirement.Hold):
        retirement.trusted_context(github, env, BASE)
    assert all(method == "GET" for method, _ in github.calls)


def test_main_writes_safe_report_for_unconfirmed_cleanup(monkeypatch, tmp_path):
    github = Fixture([run()])
    github.finish_on_cancel = False
    monkeypatch.setattr(retirement, "GitHub", lambda _: github)
    monkeypatch.setattr(
        retirement.controller,
        "git",
        lambda *args: subprocess.CompletedProcess(args, 0, stdout=BASE),
    )
    for name, value in environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GH_TOKEN", "secret-fixture-value")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert retirement.main() == 1
    text = (tmp_path / "retired-automation.json").read_text()
    assert "secret-fixture-value" not in text
    assert json.loads(text)["runs"][0]["status"] == "queued"
