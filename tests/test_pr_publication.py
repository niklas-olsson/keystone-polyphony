"""Real bare Git exercises the production selected-main publication boundary."""

import importlib.util
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location(
    "pr_publication_controller", Path(__file__).parents[1] / "scripts/pr-controller.py"
)
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


@pytest.fixture
def graph(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Lease Fixture")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Lease Fixture")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(remote), str(work)], check=True, capture_output=True
    )
    monkeypatch.chdir(work)

    def git(*args):
        return subprocess.check_output(
            ["git", *args], text=True, stderr=subprocess.DEVNULL
        ).strip()

    tree = subprocess.check_output(["git", "mktree"], input="", text=True).strip()
    base = git("commit-tree", tree, "-m", "base")
    head = git("commit-tree", tree, "-p", base, "-m", "head")
    merge = git("commit-tree", tree, "-p", base, "-p", head, "-m", "tested merge")
    other = git("commit-tree", tree, "-p", base, "-m", "concurrent main")
    # Make the candidate available for fetch without admitting it to main.
    git("push", "origin", base + ":refs/heads/main", merge + ":refs/heads/candidate")
    plan = {"pr_number": "7", "head_sha": head, "base_sha": base, "merge_sha": merge}

    class Forge:
        repository = "owner/repo"
        moved = False

        def api(self, path):
            if path == "git/ref/heads/main":
                return {
                    "object": {
                        "sha": git("ls-remote", "origin", "refs/heads/main").split()[0]
                    }
                }
            assert path == "pulls/7"
            return {
                "merged": not self.moved,
                "head": {"sha": other if self.moved else head},
            }

    return git, plan, Forge(), other


def test_exact_tested_object_publishes_and_reports_real_remote_state(graph):
    git, plan, forge, _ = graph
    controller.prepare_object(plan)
    result = controller.publish(forge, plan)
    assert result == {
        "main_sha": plan["merge_sha"],
        "pr_merged": True,
        "push_acknowledged": True,
    }
    assert git("ls-remote", "origin", "refs/heads/main").split()[0] == plan["merge_sha"]


@pytest.mark.parametrize("included_ancestor", [False, True])
def test_concurrent_main_is_preserved_even_when_normal_fast_forward_would_work(
    graph, included_ancestor
):
    git, plan, forge, other = graph
    controller.prepare_object(plan)
    concurrent = plan["head_sha"] if included_ancestor else other
    git("push", "origin", concurrent + ":refs/heads/main")
    if included_ancestor:
        git("merge-base", "--is-ancestor", concurrent, plan["merge_sha"])
    with pytest.raises(controller.Hold, match="not confirmed"):
        controller.publish(forge, plan)
    assert git("ls-remote", "origin", "refs/heads/main").split()[0] == concurrent


@pytest.mark.parametrize("invalid", ["rewind", "wrong_head", "extra_parent"])
def test_invalid_candidate_refuses_before_publication(graph, invalid):
    git, plan, forge, other = graph
    if invalid == "rewind":
        plan["merge_sha"] = plan["base_sha"]
    elif invalid == "wrong_head":
        plan["head_sha"] = other
    else:
        tree = git("rev-parse", plan["merge_sha"] + "^{tree}")
        plan["merge_sha"] = git(
            "commit-tree",
            tree,
            "-p",
            plan["base_sha"],
            "-p",
            plan["head_sha"],
            "-p",
            other,
            "-m",
            "unexpected parent",
        )
        git("push", "origin", plan["merge_sha"] + ":refs/heads/invalid")
    with pytest.raises(controller.Hold, match="parents"):
        controller.prepare_object(plan)
    assert forge.api("git/ref/heads/main")["object"]["sha"] == plan["base_sha"]


def test_unknown_push_ack_reconciles_original_object_without_another_push(
    graph, monkeypatch
):
    _, plan, forge, _ = graph
    controller.prepare_object(plan)
    actual_git = controller.git
    pushes = []

    def lost_reply(*args, **kwargs):
        result = actual_git(*args, **kwargs)
        if "push" in args:
            pushes.append(args)
            raise subprocess.TimeoutExpired("git push", 90)
        return result

    monkeypatch.setattr(controller, "git", lost_reply)
    result = controller.publish(forge, plan)
    assert result["main_sha"] == plan["merge_sha"]
    assert result["push_acknowledged"] is False and len(pushes) == 1


def test_pr_metadata_movement_does_not_claim_pr_closure(graph):
    _, plan, forge, _ = graph
    controller.prepare_object(plan)
    forge.moved = True
    result = controller.publish(forge, plan)
    assert result["main_sha"] == plan["merge_sha"] and result["pr_merged"] is False


@pytest.mark.parametrize(
    "error,classification",
    [
        (
            "remote: refusing to allow a GitHub App to create or update workflow `.github/workflows/example.yml` without `workflows` permission",
            "workflow permission",
        ),
        ("! [rejected] candidate -> main (stale info)", "selected-main lease"),
        ("remote: error: GH013: Repository rule violations found", "branch policy"),
    ],
)
def test_publication_reports_fixed_failure_class_without_echoing_remote_text(
    graph, monkeypatch, error, classification
):
    _, plan, forge, _ = graph
    monkeypatch.setattr(
        controller,
        "git",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, "", error + "\nprivate-canary-remote-text"
        ),
    )
    with pytest.raises(controller.Hold) as caught:
        controller.publish(forge, plan)
    assert classification in str(caught.value)
    assert "private-canary" not in str(caught.value)
    assert forge.api("git/ref/heads/main")["object"]["sha"] == plan["base_sha"]
