"""Independent admission-policy checks using explicit GitHub response fixtures.

These exercise the real controller's identity, provenance, review and orchestration
boundaries. Git publication is covered separately by disposable real repositories;
these fixtures do not claim an actual forge merge or provider invocation.
"""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location(
    "pr_controller_under_test", Path(__file__).parents[1] / "scripts/pr-controller.py"
)
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)

REPOSITORY = "owner/project"
OWNER = 101
AUTHOR = 303
REPO_ID = 202
NUMBER = 17
BASE, HEAD, MERGE, OTHER = (character * 40 for character in "1234")


def repository():
    return {
        "id": REPO_ID,
        "full_name": REPOSITORY,
        "default_branch": "main",
        "owner": {"id": OWNER, "type": "User"},
    }


def pull_request(author=OWNER):
    return {
        "number": NUMBER,
        "state": "open",
        "draft": False,
        "mergeable": True,
        "user": {"id": author, "login": "owner" if author == OWNER else "external"},
        "author_association": "OWNER",
        "labels": [],
        "head": {"sha": HEAD, "repo": {"id": REPO_ID, "full_name": REPOSITORY}},
        "base": {"ref": "main", "sha": BASE, "repo": repository()},
        "merge_commit_sha": MERGE,
    }


def plan_for(pr=None):
    return controller.candidate(repository(), pr or pull_request(), BASE, [BASE, HEAD])


def approved_run(plan, run_id=71, attempt=1):
    return {
        "id": run_id,
        "workflow_id": 91,
        "event": "workflow_dispatch",
        "path": ".github/workflows/pr-validation.yml",
        "repository": {"full_name": REPOSITORY},
        "head_branch": "main",
        "head_sha": BASE,
        "display_title": controller.run_name(plan),
        "run_attempt": attempt,
        "status": "completed",
        "conclusion": "success",
    }


def approved_job(run_id=71, attempt=1):
    return {
        "id": 81,
        "name": "validate",
        "run_id": run_id,
        "run_attempt": attempt,
        "head_sha": BASE,
        "status": "completed",
        "conclusion": "success",
        "steps": [
            {"name": name, "conclusion": "success"}
            for name in (
                "Verify selected revisions",
                "Verify checked out merge",
                "Install dependencies",
                "Lint",
                "Test",
            )
        ],
    }


def review(review_id=51, user=OWNER, state="APPROVED", commit=HEAD):
    return {
        "id": review_id,
        "user": {"id": user},
        "state": state,
        "commit_id": commit,
    }


def replace_field(document, field, value):
    names = field.split(".")
    for name in names[:-1]:
        document = document[name]
    document[names[-1]] = value


class GitHubFixture:
    """A strict response boundary; unexpected API operations fail the test."""

    repository = REPOSITORY

    def __init__(self, pr=None):
        self.repo = repository()
        self.pr = pr or pull_request()
        self.plan = plan_for(self.pr)
        self.main = BASE
        self.parents = [BASE, HEAD]
        self.runs = [approved_run(self.plan)]
        self.job = approved_job()
        self.jobs = [self.job]
        self.reviews = []
        self.threads = []
        self.external_runs = []
        self.calls = []
        self.dispatches = []
        self.comments = []
        self.lose_comment_acknowledgement = False
        self.before_pr_read = None
        self.before_run_read = None
        self.pr_reads = 0
        self.run_reads = 0

    def api(self, suffix="", *, method="GET", body=None):
        self.calls.append((method, suffix))
        if suffix == f"issues/{NUMBER}/comments" and method == "POST":
            comment = {
                "id": 501 + len(self.comments),
                "body": body["body"],
                "user": {"id": 41898282},
            }
            self.comments.append(comment)
            if self.lose_comment_acknowledgement:
                raise controller.Hold("Lost reservation acknowledgement")
            return deepcopy(comment)
        if suffix.endswith("/dispatches"):
            assert method == "POST"
            self.dispatches.append((suffix, deepcopy(body)))
            return None
        assert method == "GET", (method, suffix)
        if not suffix:
            return deepcopy(self.repo)
        if suffix == f"pulls/{NUMBER}":
            self.pr_reads += 1
            if self.before_pr_read:
                self.before_pr_read(self)
            return deepcopy(self.pr)
        if suffix == "git/ref/heads/main":
            return {"object": {"sha": self.main}}
        if suffix == f"git/commits/{self.pr['merge_commit_sha']}":
            return {
                "sha": self.pr["merge_commit_sha"],
                "parents": [{"sha": parent} for parent in self.parents],
            }
        if suffix == "actions/workflows/pr-validation.yml":
            return {"id": 91, "path": controller.VALIDATION, "state": "active"}
        if suffix == "actions/workflows/external-pr-review.yml":
            return {"id": 92, "path": controller.EXTERNAL_REVIEW, "state": "active"}
        if suffix.startswith("actions/runs/"):
            self.run_reads += 1
            if self.before_run_read:
                self.before_run_read(self)
            run_id = int(suffix.rsplit("/", 1)[1])
            return deepcopy(next(run for run in self.runs if run["id"] == run_id))
        raise AssertionError(f"Unexpected API operation: {method} {suffix}")

    def pages(self, suffix, key=None):
        self.calls.append(("pages", suffix))
        if suffix == "pulls?state=open&base=main&sort=created&direction=asc":
            return [deepcopy(self.pr)]
        if suffix == f"issues/{NUMBER}/comments":
            return deepcopy(self.comments)
        if suffix.startswith("actions/workflows/91/runs?"):
            assert key == "workflow_runs"
            assert "event=workflow_dispatch" in suffix
            assert f"head_sha={BASE}" in suffix
            return deepcopy(self.runs)
        if suffix.startswith("actions/workflows/92/runs?"):
            assert key == "workflow_runs"
            return deepcopy(self.external_runs)
        if suffix.startswith("actions/runs/") and suffix.endswith("/jobs"):
            assert key == "jobs"
            return deepcopy(self.jobs)
        if suffix == f"pulls/{NUMBER}/reviews":
            assert key is None
            return deepcopy(self.reviews)
        raise AssertionError(f"Unexpected paginated API operation: {suffix}")

    def request(self, path, *, method="GET", body=None):
        assert path == "graphql" and method == "POST"
        assert body["variables"]["number"] == NUMBER
        self.calls.append(("graphql", body["variables"]["cursor"]))
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": deepcopy(self.threads),
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }


@pytest.fixture(autouse=True)
def no_wall_clock_delays(monkeypatch):
    monkeypatch.setattr(controller.time, "sleep", lambda _: None)


@pytest.mark.parametrize("author", [AUTHOR, 999])
def test_owner_identity_cannot_be_acquired_from_actor_association_or_labels(author):
    pr = pull_request(author)
    pr.update(actor={"id": OWNER}, triggering_actor={"id": OWNER})
    pr["labels"] = [{"name": "owner"}, {"name": "decision-merge"}]
    assert controller.classify_owner(repository(), pr) is False


@pytest.mark.parametrize("bad_id", [True, False, 0, -1, "101", None])
def test_owner_requires_positive_integer_authenticated_ids(bad_id):
    repo, pr = repository(), pull_request()
    pr["user"]["id"] = bad_id
    with pytest.raises(controller.Hold):
        controller.classify_owner(repo, pr)
    pr["user"]["id"] = OWNER
    repo["owner"]["id"] = bad_id
    with pytest.raises(controller.Hold):
        controller.classify_owner(repo, pr)


def test_organization_ownership_is_not_silently_treated_as_personal_owner():
    repo = repository()
    repo["owner"]["type"] = "Organization"
    with pytest.raises(controller.Hold):
        controller.classify_owner(repo, pull_request())


def test_owner_authored_fork_is_eligible_by_author_identity():
    pr = pull_request()
    pr["head"]["repo"] = {"id": 909, "full_name": "some-fork/project"}
    assert plan_for(pr)["owner"] is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "closed"),
        ("draft", True),
        ("draft", None),
        ("mergeable", None),
        ("mergeable", False),
        ("base.ref", "staging"),
        ("base.repo.id", 404),
        ("base.repo.full_name", "attacker/project"),
        ("number", True),
        ("head.sha", "$(touch /tmp/untrusted)"),
        ("labels", [{"name": "hold-merge"}]),
        ("head.repo", None),
        ("head.repo.id", True),
        ("head.repo.full_name", ""),
    ],
)
def test_candidate_rejects_ineligible_or_missing_forge_metadata(field, value):
    pr = pull_request()
    replace_field(pr, field, value)
    with pytest.raises(controller.Hold):
        plan_for(pr)


@pytest.mark.parametrize(
    "main,parents",
    [
        (OTHER, [BASE, HEAD]),
        (BASE, [HEAD, BASE]),
        (BASE, [BASE, OTHER]),
        (BASE, [BASE, HEAD, OTHER]),
        (BASE, [HEAD]),
    ],
)
def test_candidate_requires_current_base_and_exact_ordered_two_parent_merge(
    main, parents
):
    with pytest.raises(controller.Hold):
        controller.candidate(repository(), pull_request(), main, parents)


def test_trusted_validation_success_requires_the_actual_successful_test_job():
    github = GitHubFixture()
    assert controller.validation_run(github, github.plan)["id"] == 71
    assert ("pages", "actions/runs/71/attempts/1/jobs") in github.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("workflow_id", 92),
        ("event", "pull_request"),
        ("path", ".github/workflows/attacker.yml"),
        ("repository.full_name", "attacker/project"),
        ("head_branch", "feat/owner-spoof"),
        ("head_sha", HEAD),
        ("run_attempt", True),
    ],
)
def test_forged_same_named_run_is_not_validation_evidence(field, value):
    github = GitHubFixture()
    replace_field(github.runs[0], field, value)
    with pytest.raises(controller.Hold):
        controller.validation_run(github, github.plan)


def test_validation_for_a_different_revision_set_is_not_reused():
    github = GitHubFixture()
    github.runs[0]["display_title"] = controller.run_name(
        {**github.plan, "head_sha": OTHER}
    )
    assert controller.validation_run(github, github.plan) is None


@pytest.mark.parametrize(
    "conclusion", ["skipped", "neutral", "cancelled", "failure", None]
)
def test_newer_run_supersedes_older_success(conclusion):
    github = GitHubFixture()
    later = approved_run(github.plan, run_id=72)
    later.update(
        conclusion=conclusion,
        status="in_progress" if conclusion is None else "completed",
    )
    github.runs.append(later)
    with pytest.raises(controller.Hold):
        controller.validation_run(github, github.plan)
    assert ("GET", "actions/runs/72") in github.calls


def test_rerun_refetches_latest_attempt_and_never_uses_old_job_success():
    github = GitHubFixture()

    def newer_attempt(current):
        current.runs[0]["run_attempt"] = 2

    github.before_run_read = newer_attempt
    with pytest.raises(controller.Hold):
        controller.validation_run(github, github.plan)
    assert ("pages", "actions/runs/71/attempts/2/jobs") in github.calls


@pytest.mark.parametrize(
    "case", ["missing", "duplicate", "skipped", "wrong-head", "test-skipped"]
)
def test_successful_workflow_does_not_mask_missing_or_skipped_validation(case):
    github = GitHubFixture()
    if case == "missing":
        github.jobs = []
    elif case == "duplicate":
        github.jobs.append(deepcopy(github.job))
    elif case == "skipped":
        github.job["conclusion"] = "skipped"
    elif case == "wrong-head":
        github.job["head_sha"] = HEAD
    else:
        next(step for step in github.job["steps"] if step["name"] == "Test")[
            "conclusion"
        ] = "skipped"
    with pytest.raises(controller.Hold):
        controller.validation_run(github, github.plan)


@pytest.mark.parametrize("author", [OWNER, AUTHOR])
def test_real_changes_requested_and_unresolved_threads_hold_both_author_classes(author):
    github = GitHubFixture(pull_request(author))
    github.reviews = [review(), review(52, user=404, state="CHANGES_REQUESTED")]
    with pytest.raises(controller.Hold, match="changes-requested"):
        controller.review_ready(github, github.repo, github.pr)
    github.reviews = [review()]
    github.threads = [{"isResolved": False}]
    with pytest.raises(controller.Hold, match="thread"):
        controller.review_ready(github, github.repo, github.pr)


@pytest.mark.parametrize(
    "case", ["missing", "different-author", "old-head", "dismissed"]
)
def test_external_author_requires_current_independent_owner_approval(case):
    github = GitHubFixture(pull_request(AUTHOR))
    if case == "different-author":
        github.reviews = [review(user=AUTHOR)]
    elif case == "old-head":
        github.reviews = [review(commit=OTHER)]
    elif case == "dismissed":
        github.reviews = [review(52, state="DISMISSED"), review()]
    with pytest.raises(controller.Hold, match="owner approval"):
        controller.review_ready(github, github.repo, github.pr)


def test_owner_needs_no_synthetic_approval_and_external_current_approval_is_accepted():
    github = GitHubFixture()
    assert controller.review_ready(github, github.repo, github.pr) is None
    github.pr["user"]["id"] = AUTHOR
    github.reviews = [review()]
    assert controller.review_ready(github, github.repo, github.pr) is None


@pytest.fixture
def publication_spy(monkeypatch):
    prepared, published = [], []

    def prepare(plan):
        prepared.append(deepcopy(plan))

    def publish(github, plan):
        published.append(deepcopy(plan))
        return {
            "main_sha": plan["merge_sha"],
            "pr_merged": True,
            "push_acknowledged": True,
        }

    monkeypatch.setattr(controller, "prepare_object", prepare)
    monkeypatch.setattr(controller, "publish", publish)
    return prepared, published


def test_owner_admission_has_no_provider_or_model_workflow_dependency(publication_spy):
    github = GitHubFixture()
    github.pr["labels"] = [
        {"name": "decision-merge"},
        {"name": "workflow-review"},
        {"name": "arch-review"},
    ]
    github.external_runs = [{"id": 99, "conclusion": "failure"}]
    result = controller.reconcile(github, github.repo, NUMBER)
    assert result["status"] == "main published" and result["pr_merged"] is True
    assert publication_spy == ([github.plan], [github.plan])
    assert not github.dispatches
    assert not any(
        "external-pr-review" in suffix for _, suffix in github.calls if suffix
    )
    assert not github.reviews  # The controller did not fabricate a review.


def test_missing_validation_dispatches_once_without_preparing_or_publishing(
    publication_spy,
):
    github = GitHubFixture()
    github.runs = []
    result = controller.reconcile(github, github.repo, NUMBER)
    assert result["status"] == "validation dispatched"
    assert github.dispatches == [
        (
            "actions/workflows/pr-validation.yml/dispatches",
            {
                "ref": "main",
                "inputs": {
                    key: github.plan[key]
                    for key in ("pr_number", "head_sha", "base_sha", "merge_sha")
                },
            },
        )
    ]
    assert publication_spy == ([], [])


def test_external_advisory_dispatch_is_not_approval(publication_spy):
    github = GitHubFixture(pull_request(AUTHOR))
    with pytest.raises(controller.Hold, match="owner approval"):
        controller.reconcile(github, github.repo, NUMBER)
    assert [suffix for suffix, _ in github.dispatches] == [
        "actions/workflows/external-pr-review.yml/dispatches"
    ]
    assert publication_spy == ([], [])


def test_external_approved_head_can_publish_when_advisory_attempt_failed(
    publication_spy,
):
    github = GitHubFixture(pull_request(AUTHOR))
    github.reviews = [review()]
    github.external_runs = [
        {
            "id": 99,
            "display_title": controller.run_name(github.plan, "External PR Review"),
            "conclusion": "failure",
        }
    ]
    result = controller.reconcile(github, github.repo, NUMBER)
    assert result["pr_merged"] is True
    assert github.dispatches == []
    assert publication_spy[1] == [github.plan]


@pytest.mark.parametrize(
    "field,value",
    [("head.sha", OTHER), ("draft", True), ("state", "closed"), ("user.id", AUTHOR)],
)
def test_changed_pr_state_during_preparation_prevents_publication(
    field, value, publication_spy
):
    github = GitHubFixture()

    def change_after_preparation(current):
        if current.pr_reads == 2:
            replace_field(current.pr, field, value)

    github.before_pr_read = change_after_preparation
    with pytest.raises(controller.Hold):
        controller.reconcile(github, github.repo, NUMBER)
    assert publication_spy == ([github.plan], [])


def test_rerun_beginning_during_preparation_prevents_publication(publication_spy):
    github = GitHubFixture()

    def start_rerun(current):
        if current.run_reads == 2:
            current.runs[0].update(run_attempt=2, status="in_progress", conclusion=None)

    github.before_run_read = start_rerun
    with pytest.raises(controller.Hold):
        controller.reconcile(github, github.repo, NUMBER)
    assert publication_spy == ([github.plan], [])


def test_validation_api_failure_is_a_hold_not_missing_evidence(
    monkeypatch, publication_spy
):
    github = GitHubFixture()
    original = github.pages

    def unavailable(suffix, key=None):
        if suffix.startswith("actions/"):
            raise controller.Hold("GitHub evidence unavailable")
        return original(suffix, key)

    monkeypatch.setattr(github, "pages", unavailable)
    with pytest.raises(controller.Hold, match="unavailable"):
        controller.reconcile(github, github.repo, NUMBER)
    assert github.dispatches == [] and publication_spy == ([], [])


def test_reserved_validation_does_not_automatically_repeat_while_run_is_invisible(
    publication_spy,
):
    github = GitHubFixture()
    github.runs = []
    controller.reconcile(github, github.repo, NUMBER)
    with pytest.raises(controller.Hold, match="no run visible"):
        controller.reconcile(github, github.repo, NUMBER)
    assert len(github.comments) == 1 and len(github.dispatches) == 1
    assert github.calls.index(
        ("POST", f"issues/{NUMBER}/comments")
    ) < github.calls.index(("POST", "actions/workflows/pr-validation.yml/dispatches"))
    assert publication_spy == ([], [])


def test_lost_reservation_acknowledgement_remains_held_until_explicit_missing_retry(
    publication_spy,
):
    github = GitHubFixture()
    github.runs = []
    github.lose_comment_acknowledgement = True
    with pytest.raises(controller.Hold, match="Lost reservation"):
        controller.reconcile(github, github.repo, NUMBER)
    github.lose_comment_acknowledgement = False
    with pytest.raises(controller.Hold, match="no run visible"):
        controller.reconcile(github, github.repo, NUMBER)
    assert len(github.comments) == 1 and github.dispatches == []
    result = controller.reconcile(github, github.repo, NUMBER, retry_missing=True)
    assert result["status"] == "validation dispatched"
    assert len(github.comments) == 2 and len(github.dispatches) == 1
    assert publication_spy == ([], [])


@pytest.mark.parametrize(
    "case", ["different-author", "different-base", "quoted-marker"]
)
def test_unrelated_or_forged_comment_cannot_reserve_current_validation(
    case, publication_spy
):
    github = GitHubFixture()
    github.runs = []
    marker = f"<!-- polyphony-validation:{HEAD}:{BASE}:{MERGE} -->"
    comment = {"body": marker, "user": {"id": 41898282}}
    if case == "different-author":
        comment["user"]["id"] = AUTHOR
    elif case == "different-base":
        comment["body"] = marker.replace(BASE, OTHER)
    else:
        comment["body"] = "Quoted from another comment: " + marker
    github.comments = [comment]
    assert (
        controller.reconcile(github, github.repo, NUMBER)["status"]
        == "validation dispatched"
    )
    assert len(github.dispatches) == 1 and publication_spy == ([], [])


def test_failed_visible_validation_cannot_be_rerun_by_missing_dispatch_override(
    publication_spy,
):
    github = GitHubFixture()
    github.runs[0]["conclusion"] = "failure"
    with pytest.raises(controller.Hold):
        controller.reconcile(github, github.repo, NUMBER, retry_missing=True)
    assert github.comments == [] and github.dispatches == []
    assert publication_spy == ([], [])


def test_external_review_attempt_is_not_repeated_only_because_main_changed(
    publication_spy,
):
    github = GitHubFixture(pull_request(AUTHOR))
    github.reviews = [review()]
    github.external_runs = [
        {
            "id": 99,
            "display_title": controller.run_name(
                {**github.plan, "base_sha": OTHER}, "External PR Review"
            ),
            "conclusion": "failure",
        }
    ]
    controller.reconcile(github, github.repo, NUMBER)
    assert github.dispatches == [] and publication_spy[1] == [github.plan]


def test_controller_cannot_apply_old_policy_to_new_main_even_with_valid_candidate(
    publication_spy,
):
    github = GitHubFixture()
    with pytest.raises(controller.PolicyMoved):
        controller.reconcile(github, github.repo, NUMBER, policy_sha=OTHER)
    assert not any("actions/" in suffix for _, suffix in github.calls if suffix)
    assert github.comments == [] and publication_spy == ([], [])


def move_main_and_synthetic_merge(github):
    github.main = OTHER
    github.pr["base"]["sha"] = OTHER
    github.pr["merge_commit_sha"] = "5" * 40
    github.parents = [OTHER, HEAD]


def test_policy_main_advancing_during_preparation_prevents_publication(publication_spy):
    github = GitHubFixture()

    def move_after_prepare(current):
        if current.pr_reads == 2:
            move_main_and_synthetic_merge(current)

    github.before_pr_read = move_after_prepare
    with pytest.raises(controller.PolicyMoved):
        controller.reconcile(github, github.repo, NUMBER, policy_sha=BASE)
    assert github.dispatches == [] and publication_spy == ([github.plan], [])


def test_main_stops_old_policy_and_requests_current_controller_after_main_moves(
    monkeypatch, tmp_path, publication_spy
):
    github = GitHubFixture()
    github.before_pr_read = move_main_and_synthetic_merge
    monkeypatch.setattr(controller, "GitHub", lambda _: github)
    monkeypatch.setattr(
        controller,
        "git",
        lambda *args: subprocess.CompletedProcess(args, 0, stdout=BASE),
    )
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.delenv("RETRY_MISSING_VALIDATION_PR", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.chdir(tmp_path)
    controller.main()
    report = json.loads((tmp_path / "admission-results.json").read_text())
    assert report["results"][0]["status"] == "new-main controller dispatched"
    assert github.dispatches == [
        ("actions/workflows/pr-controller.yml/dispatches", {"ref": "main"})
    ]
    assert publication_spy == ([], [])


def test_pending_synthetic_merge_has_bounded_recovery_reads(monkeypatch):
    github = GitHubFixture()
    github.pr["mergeable"] = None
    github.pr["merge_commit_sha"] = None
    delays = []
    monkeypatch.setattr(controller.time, "sleep", delays.append)
    with pytest.raises(controller.Hold, match="still calculating"):
        controller.snapshot(github, github.repo, NUMBER)
    assert github.pr_reads == 4 and delays == [1, 1, 1]


@pytest.mark.parametrize("reply", ["failure", "timeout"])
def test_lost_push_acknowledgement_is_reconciled_against_original_merge(
    monkeypatch, reply
):
    github = GitHubFixture()
    github.main = MERGE
    github.pr["merged"] = True
    calls = []

    def ambiguous_push(*args, **kwargs):
        calls.append(args)
        if reply == "timeout":
            raise subprocess.TimeoutExpired("git", 90)
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(controller, "git", ambiguous_push)
    result = controller.publish(github, github.plan)
    assert result == {"main_sha": MERGE, "pr_merged": True, "push_acknowledged": False}
    assert len(calls) == 1
    assert f"--force-with-lease=refs/heads/main:{BASE}" in calls[0]
    assert f"{MERGE}:refs/heads/main" in calls[0]


def test_post_publication_pr_movement_is_not_reported_as_a_merged_current_head(
    monkeypatch,
):
    github = GitHubFixture()
    github.main = MERGE
    github.pr.update(merged=False)
    github.pr["head"]["sha"] = OTHER
    monkeypatch.setattr(
        controller, "git", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0)
    )
    assert controller.publish(github, github.plan) == {
        "main_sha": MERGE,
        "pr_merged": False,
        "push_acknowledged": True,
    }


def test_successful_push_reply_does_not_replace_verification_of_actual_main(
    monkeypatch,
):
    github = GitHubFixture()
    github.main = OTHER
    monkeypatch.setattr(
        controller, "git", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0)
    )
    with pytest.raises(controller.Hold, match="not confirmed"):
        controller.publish(github, github.plan)


def test_later_review_thread_page_cannot_be_ignored(monkeypatch):
    github = GitHubFixture()
    cursors = []

    def request(path, *, method, body):
        cursor = body["variables"]["cursor"]
        cursors.append(cursor)
        threads = {
            "nodes": [{"isResolved": cursor is None}],
            "pageInfo": {
                "hasNextPage": cursor is None,
                "endCursor": "next" if cursor is None else None,
            },
        }
        return {"data": {"repository": {"pullRequest": {"reviewThreads": threads}}}}

    monkeypatch.setattr(github, "request", request)
    with pytest.raises(controller.Hold, match="thread"):
        controller.review_ready(github, github.repo, github.pr)
    assert cursors == [None, "next"]


@pytest.mark.parametrize("comment_author", [41898282, AUTHOR])
def test_authenticated_external_marker_survives_missing_run_history_and_keeps_original_base(
    comment_author, publication_spy
):
    github = GitHubFixture(pull_request(AUTHOR))
    github.reviews = [review()]
    github.comments = [
        {
            "id": 501,
            "user": {
                "id": comment_author,
                "login": "github-actions[bot]",
                "type": "Bot",
            },
            "body": (
                f"<!-- polyphony-external-review:v1 pr={NUMBER} head={HEAD} -->\n"
                "## External PR advisory review\n\nStatus: **unavailable**.\n"
                f"Head: `{HEAD}`\nBase: `{OTHER}`\nTested merge: `{'6' * 40}`\n"
            ),
        }
    ]
    original = deepcopy(github.comments)
    assert github.external_runs == []
    result = controller.reconcile(github, github.repo, NUMBER)
    assert result["pr_merged"] is True and result["plan"]["base_sha"] == BASE
    assert publication_spy[1] == [github.plan]
    assert (
        github.comments == original
    )  # The historical base is not relabelled as current.
    if comment_author == 41898282:
        assert result["advisory"] == "external head already attempted"
        assert github.dispatches == []
        assert ("GET", "actions/workflows/external-pr-review.yml") not in github.calls
    else:
        assert result["advisory"] == "external advisory dispatched"
        assert [suffix for suffix, _ in github.dispatches] == [
            "actions/workflows/external-pr-review.yml/dispatches"
        ]


@pytest.mark.parametrize("failure", ["lookup", "dispatch", "dispatch-timeout"])
@pytest.mark.parametrize("owner_approved", [False, True])
def test_external_infrastructure_failure_is_advisory_but_owner_approval_remains_required(
    monkeypatch, capsys, publication_spy, failure, owner_approved
):
    github = GitHubFixture(pull_request(AUTHOR))
    github.reviews = [review()] if owner_approved else []
    original_api = github.api
    failed_calls = []
    endpoint = "actions/workflows/external-pr-review.yml"
    if failure != "lookup":
        endpoint += "/dispatches"

    def unavailable(suffix="", *, method="GET", body=None):
        if suffix == endpoint:
            failed_calls.append((method, suffix))
            if failure == "dispatch-timeout":
                raise subprocess.TimeoutExpired("gh", 60)
            raise controller.Hold("External advisory infrastructure unavailable")
        return original_api(suffix, method=method, body=body)

    monkeypatch.setattr(github, "api", unavailable)
    if owner_approved:
        result = controller.reconcile(github, github.repo, NUMBER)
        assert result["status"] == "main published" and result["pr_merged"] is True
        assert result["advisory"].startswith("external advisory unavailable:")
        assert publication_spy[1] == [github.plan]
    else:
        with pytest.raises(controller.Hold, match="owner approval"):
            controller.reconcile(github, github.repo, NUMBER)
        assert publication_spy == ([], [])
    assert len(failed_calls) == 1
    assert "external advisory unavailable:" in capsys.readouterr().out


@pytest.mark.parametrize("signal_action", ["submitted", "dismissed"])
@pytest.mark.parametrize("actually_resolved", [False, True])
def test_read_only_review_signal_cannot_override_fresh_authoritative_thread_state(
    monkeypatch, tmp_path, publication_spy, signal_action, actually_resolved
):
    github = GitHubFixture()
    github.threads = [{"isResolved": actually_resolved}]
    signal = tmp_path / "read-only-signal.json"
    signal.write_text(
        json.dumps(
            {
                "action": "completed",
                "workflow_run": {
                    "name": "PR Review Signal",
                    "event": "pull_request_review",
                    "conclusion": "success",
                    "head_sha": OTHER,
                    "display_title": signal_action,
                    "pull_requests": [{"number": 999, "head": {"sha": OTHER}}],
                },
                "review": {
                    "state": (
                        "APPROVED" if signal_action == "submitted" else "DISMISSED"
                    ),
                    "commit_id": OTHER,
                },
            }
        )
    )
    monkeypatch.setattr(controller, "GitHub", lambda _: github)
    monkeypatch.setattr(
        controller,
        "git",
        lambda *args: subprocess.CompletedProcess(args, 0, stdout=BASE),
    )
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(signal))
    monkeypatch.delenv("RETRY_MISSING_VALIDATION_PR", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.chdir(tmp_path)
    controller.main()
    result = json.loads((tmp_path / "admission-results.json").read_text())["results"][0]
    assert result["pr"] == NUMBER
    assert ("graphql", None) in github.calls
    if actually_resolved:
        assert result["status"] == "main published" and result["pr_merged"] is True
        assert publication_spy[1] == [github.plan]
    else:
        assert result["status"] == "held" and "thread" in result["reason"]
        assert publication_spy == ([], []) and github.dispatches == []


@pytest.mark.parametrize(
    "confirmation_endpoint",
    ["git/ref/heads/main", f"pulls/{NUMBER}"],
    ids=["main-ref-confirmation", "pr-confirmation"],
)
def test_post_push_confirmation_timeout_retains_exact_evidence_without_leaking_command(
    monkeypatch, capsys, confirmation_endpoint
):
    canary = "CANARY_PUBLICATION_CONFIRMATION_COMMAND_MUST_NOT_APPEAR"

    class ConfirmationTimeoutGitHub(GitHubFixture):
        push_completed = False

        def api(self, suffix="", *, method="GET", body=None):
            if self.push_completed and suffix == confirmation_endpoint:
                raise subprocess.TimeoutExpired(["gh", "api", canary], 60)
            return super().api(suffix, method=method, body=body)

    github = ConfirmationTimeoutGitHub()
    github.runs[0]["run_attempt"] = 3
    github.job["run_attempt"] = 3
    prepared, pushes = [], []

    def successful_git_push(command, **kwargs):
        assert command[0] == "git" and "push" in command
        assert f"--force-with-lease=refs/heads/main:{BASE}" in command
        assert f"{MERGE}:refs/heads/main" in command
        pushes.append(command)
        github.push_completed = True
        github.main = MERGE
        github.pr["merged"] = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(controller, "prepare_object", prepared.append)
    monkeypatch.setattr(controller.subprocess, "run", successful_git_push)
    result = controller.reconcile(github, github.repo, NUMBER, policy_sha=BASE)

    assert result == {
        "pr": NUMBER,
        "status": "held",
        "reason": "Publication confirmation timed out; reconcile actual main and PR state",
        "plan": github.plan,
        "validation_run": 71,
        "validation_attempt": 3,
        "advisory": "not applicable: owner PR",
    }
    assert prepared == [github.plan] and len(pushes) == 1
    assert github.main == MERGE and github.push_completed is True
    assert github.dispatches == []
    output = capsys.readouterr()
    assert canary not in json.dumps(result) + output.out + output.err
