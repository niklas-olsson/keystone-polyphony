"""Credential-free adversarial fixtures for the external advisory perimeter."""

import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "external_pr_review", ROOT / "scripts/review-external-pr.py"
)
review = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = review
SPEC.loader.exec_module(review)
BASE, HEAD, MERGE = "a" * 40, "b" * 40, "c" * 40
CANDIDATE = review.Candidate(123, HEAD, BASE, MERGE)
PREFIX = f"/repos/{review.REPOSITORY}"
DIFF = b"diff --git a/file.py b/file.py\n--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
BOT = {"id": review.ACTIONS_BOT_ID, "login": "github-actions[bot]", "type": "Bot"}


class FakeGitHub:
    def __init__(self, author=20):
        self.repository = {
            "id": 100,
            "full_name": review.REPOSITORY,
            "default_branch": "main",
            "owner": {"id": 10, "type": "User"},
        }
        self.pr = {
            "number": 123,
            "user": {"id": author},
            "state": "open",
            "draft": False,
            "mergeable": True,
            "head": {"sha": HEAD, "repo": {"id": 200, "full_name": "someone/fork"}},
            "base": {
                "sha": BASE,
                "ref": "main",
                "repo": {"id": 100, "full_name": review.REPOSITORY},
            },
            "merge_commit_sha": MERGE,
        }
        self.ref = {"object": {"type": "commit", "sha": BASE}}
        self.commit = {"sha": MERGE, "parents": [{"sha": BASE}, {"sha": HEAD}]}
        self.comments = []
        self.diff = DIFF
        self.calls = []
        self.pr_reads = 0
        self.on_pr_read = lambda count: None

    def request(self, path, *, method="GET", body=None, diff=False):
        self.calls.append((method, path, copy.deepcopy(body)))
        if diff:
            assert (
                path
                == f"{PREFIX}/compare/{self.pr['base']['sha']}...{self.commit['sha']}"
            )
            assert method == "GET"
            return self.diff
        if path == PREFIX:
            return copy.deepcopy(self.repository)
        if path == f"{PREFIX}/pulls/123":
            self.pr_reads += 1
            self.on_pr_read(self.pr_reads)
            return copy.deepcopy(self.pr)
        if path == f"{PREFIX}/git/ref/heads/main":
            return copy.deepcopy(self.ref)
        if path == f"{PREFIX}/git/commits/{self.commit['sha']}":
            return copy.deepcopy(self.commit)
        if path.startswith(f"{PREFIX}/issues/123/comments?"):
            page = int(path.rsplit("=", 1)[-1])
            start = (page - 1) * review.COMMENT_PAGE_SIZE
            return copy.deepcopy(
                self.comments[start : start + review.COMMENT_PAGE_SIZE]
            )
        if path == f"{PREFIX}/issues/123/comments" and method == "POST":
            comment = {"id": len(self.comments) + 1, "user": BOT, "body": body["body"]}
            self.comments.append(copy.deepcopy(comment))
            return comment
        if path.startswith(f"{PREFIX}/issues/comments/") and method == "PATCH":
            number = int(path.rsplit("/", 1)[-1])
            self.comments[number - 1]["body"] = body["body"]
            return self.comments[number - 1]
        raise AssertionError((method, path))


def forbidden(*args, **kwargs):
    pytest.fail(
        "Provider credential or request accessed outside eligible external review"
    )


def environment():
    return {
        "GITHUB_REPOSITORY": review.REPOSITORY,
        "GITHUB_REPOSITORY_ID": "100",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": BASE,
    }


@pytest.mark.parametrize("actor", ["niklas-olsson", "outsider", "github-actions[bot]"])
def test_owner_never_reads_key_or_calls_provider_regardless_of_actor(actor):
    github = FakeGitHub(author=10)
    github.pr.update(
        actor=actor, author_association="COLLABORATOR", labels=[{"name": "review"}]
    )
    result = review.perform_review(github, CANDIDATE, 100, forbidden, forbidden)
    assert result["status"] == "skipped_owner"
    assert result["provider_requests"] == 0
    assert not github.comments
    assert all(method == "GET" for method, _, _ in github.calls)


@pytest.mark.parametrize("same_repo", [False, True])
def test_external_author_stays_external_when_owner_triggers_or_owns_head(same_repo):
    github = FakeGitHub()
    github.pr.update(actor="niklas-olsson", author_association="OWNER")
    if same_repo:
        github.pr["head"]["repo"] = {"id": 100, "full_name": review.REPOSITORY}
    assert review.preflight(github, CANDIDATE, 100)["status"] == "ready"


@pytest.mark.parametrize("field", ["owner", "author"])
@pytest.mark.parametrize("identity", [None, True, False, 0, -1, "10", 10.0])
def test_invalid_identity_never_reaches_key(field, identity):
    github = FakeGitHub()
    value = github.repository["owner"] if field == "owner" else github.pr["user"]
    value["id"] = identity
    with pytest.raises(review.Unavailable, match="identity"):
        review.perform_review(github, CANDIDATE, 100, forbidden, forbidden)
    assert not github.comments


@pytest.mark.parametrize(
    "change",
    [
        lambda g: g.repository.update(id=101),
        lambda g: g.repository.update(full_name="somewhere/else"),
        lambda g: g.repository["owner"].update(type="Organization"),
        lambda g: g.pr.update(state="closed"),
        lambda g: g.pr.update(draft=True),
        lambda g: g.pr.update(mergeable=None),
        lambda g: g.pr["head"].update(sha="d" * 40),
        lambda g: g.pr["head"].update(repo=None),
        lambda g: g.pr["base"].update(ref="staging"),
        lambda g: g.pr["base"].update(sha="d" * 40),
        lambda g: g.pr["base"]["repo"].update(id=101),
        lambda g: g.pr.update(merge_commit_sha="d" * 40),
        lambda g: g.ref["object"].update(sha="d" * 40),
        lambda g: g.commit.update(parents=[{"sha": HEAD}, {"sha": BASE}]),
        lambda g: g.commit["parents"].append({"sha": "d" * 40}),
    ],
)
def test_wrong_or_stale_metadata_never_reaches_key(change):
    github = FakeGitHub()
    change(github)
    with pytest.raises(review.Unavailable):
        review.perform_review(github, CANDIDATE, 100, forbidden, forbidden)
    assert not github.comments


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_REF", "refs/heads/evil"),
        ("GITHUB_SHA", "d" * 40),
        ("GITHUB_EVENT_NAME", "pull_request_target"),
        ("GITHUB_REPOSITORY", "evil/repo"),
        ("GITHUB_REPOSITORY_ID", ""),
        ("GITHUB_REPOSITORY_ID", "1\n2"),
    ],
)
def test_dispatch_cannot_run_from_other_workflow_revision(key, value):
    env = environment()
    assert review.trusted_execution(CANDIDATE, env) == 100
    env[key] = value
    with pytest.raises(review.Unavailable):
        review.trusted_execution(CANDIDATE, env)


def test_reservation_precedes_key_and_duplicate_pair_never_retries():
    github = FakeGitHub()
    calls = []

    def key():
        assert github.comments[0]["body"].startswith(CANDIDATE.marker + "\n")
        assert "**reserved**" in github.comments[0]["body"]
        calls.append("key")
        return "provider-fixture"

    def provider(candidate, diff, credential):
        assert diff == DIFF.decode()
        assert candidate == CANDIDATE
        assert credential == "provider-fixture"
        calls.append("request")
        return "reviewed", "Fixture report.", "## Findings\nA concrete concern remains."

    result = review.perform_review(github, CANDIDATE, 100, key, provider)
    assert result["status"] == "reviewed" and result["provider_requests"] == 1
    assert calls == ["key", "request"]
    assert "A concrete concern remains." in github.comments[0]["body"]
    assert "grants no approval" in github.comments[0]["body"]
    rerun = review.perform_review(github, CANDIDATE, 100, forbidden, forbidden)
    assert rerun["status"] == "already_attempted" and len(github.comments) == 1


def test_untrusted_or_nested_marker_does_not_consume_attempt():
    github = FakeGitHub()
    github.comments = [
        {
            "id": 1,
            "user": {"id": 20, "login": "contributor", "type": "User"},
            "body": CANDIDATE.marker + "\n",
        },
        {"id": 2, "user": BOT, "body": "Unrelated report\n" + CANDIDATE.marker + "\n"},
    ]
    assert review.preflight(github, CANDIDATE, 100)["status"] == "ready"


def test_attempt_history_is_paginated_and_bounded():
    github = FakeGitHub()
    comment = {"id": 1, "user": BOT, "body": "Unrelated\n"}
    github.comments = [comment] * review.COMMENT_PAGE_SIZE
    github.comments.append({**comment, "body": CANDIDATE.marker + "\n"})
    assert review.attempted(github, CANDIDATE)
    github.comments = [comment] * (review.COMMENT_PAGE_SIZE * review.MAX_COMMENT_PAGES)
    with pytest.raises(review.Unavailable, match="bounded lookup"):
        review.attempted(github, CANDIDATE)


def test_new_base_does_not_spend_again_for_unchanged_head():
    github = FakeGitHub()
    result = review.perform_review(github, CANDIDATE, 100, lambda: "", forbidden)
    assert result["status"] == "unavailable"
    new_base, new_merge = "d" * 40, "e" * 40
    github.pr["base"]["sha"] = new_base
    github.pr["merge_commit_sha"] = new_merge
    github.ref["object"]["sha"] = new_base
    github.commit = {
        "sha": new_merge,
        "parents": [{"sha": new_base}, {"sha": HEAD}],
    }
    candidate = review.Candidate(123, HEAD, new_base, new_merge)
    result = review.perform_review(github, candidate, 100, forbidden, forbidden)
    assert result["status"] == "already_attempted"
    assert len(github.comments) == 1
    assert f"Base: `{BASE}`" in github.comments[0]["body"]
    assert f"Tested merge: `{MERGE}`" in github.comments[0]["body"]


@pytest.mark.parametrize(
    "diff",
    [
        b"x" * (review.MAX_DIFF_BYTES + 1),
        b"diff --git a/x b/x\nBinary files a/x and b/x differ\n",
        b"diff --git a/x b/x\nGIT binary patch\n",
        b"diff --git a/x b/x\n\x00",
        b"diff --git a/x b/x\n\xff",
        b"",
    ],
)
def test_unsupported_diff_consumes_no_provider_request(diff):
    github = FakeGitHub()
    github.diff = diff
    result = review.perform_review(github, CANDIDATE, 100, forbidden, forbidden)
    assert result["status"] == "unavailable" and result["provider_requests"] == 0
    assert "**unavailable**" in github.comments[0]["body"]


def test_diff_uses_exact_merge_and_accepts_boundary():
    github = FakeGitHub()
    github.diff = DIFF + b"+" * (review.MAX_DIFF_BYTES - len(DIFF))
    assert (
        len(review.immutable_diff(github, CANDIDATE).encode()) == review.MAX_DIFF_BYTES
    )
    assert github.calls[-1][1] == f"{PREFIX}/compare/{BASE}...{MERGE}"


def test_revision_change_before_key_is_unavailable_without_request():
    github = FakeGitHub()

    def change(count):
        if count == 2:
            github.pr["head"]["sha"] = "d" * 40

    github.on_pr_read = change
    result = review.perform_review(github, CANDIDATE, 100, forbidden, forbidden)
    assert result["status"] == "unavailable" and result["provider_requests"] == 0


def test_revision_change_after_request_preserves_unverified_findings():
    github = FakeGitHub()

    def provider(*args):
        github.pr["head"]["sha"] = "d" * 40
        return "reviewed", "Completed.", "A real finding remains visible."

    result = review.perform_review(github, CANDIDATE, 100, lambda: "fixture", provider)
    assert result["status"] == "unverified" and result["provider_requests"] == 1
    assert "A real finding remains visible." in github.comments[0]["body"]


def test_missing_key_and_provider_failure_do_not_retry():
    for missing_key in (True, False):
        github = FakeGitHub()
        calls = []

        def provider(*args):
            calls.append("attempt")
            raise review.Unavailable("Provider request unavailable.")

        result = review.perform_review(
            github, CANDIDATE, 100, lambda: "" if missing_key else "fixture", provider
        )
        assert result["status"] == "unavailable"
        assert result["provider_requests"] == (0 if missing_key else 1)
        assert calls == ([] if missing_key else ["attempt"])
        assert (
            review.perform_review(github, CANDIDATE, 100, forbidden, forbidden)[
                "status"
            ]
            == "already_attempted"
        )


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_limits = []

    def read(self, size=-1):
        self.read_limits.append(size)
        return super().read(size)


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def completion(content="## Findings\nNo concrete findings.", finish="stop", **extra):
    return json.dumps(
        {
            "choices": [
                {"finish_reason": finish, "message": {"content": content, **extra}}
            ]
        }
    ).encode()


def test_provider_request_has_one_fixed_free_model_and_no_tools_or_github_token():
    response = FakeResponse(completion())
    opener = FakeOpener(response)
    status, _, text = review.provider_review(
        CANDIDATE, DIFF.decode(), "provider-fixture", opener
    )
    assert status == "reviewed" and text.startswith("## Findings")
    assert len(opener.calls) == 1
    request, timeout = opener.calls[0]
    payload = json.loads(request.data)
    assert request.full_url == review.PROVIDER_URL and request.method == "POST"
    assert request.get_header("Authorization") == "Bearer provider-fixture"
    assert timeout == 45 and response.read_limits == [review.MAX_PROVIDER_BYTES + 1]
    assert payload["model"] == "recursive/flint-v1.0:free"
    assert payload["max_tokens"] == 3000 and payload["stream"] is False
    assert payload["provider"] == {"allow_fallbacks": False}
    assert "tools" not in payload and "tool_choice" not in payload
    assert "provider-fixture" not in request.data.decode()


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"\xff",
        b"{}",
        completion(""),
        completion("x" * (review.MAX_REVIEW_BYTES + 1)),
        b"x" * (review.MAX_PROVIDER_BYTES + 1),
    ],
)
def test_provider_bad_output_is_unavailable_and_not_retried(body):
    opener = FakeOpener(FakeResponse(body))
    with pytest.raises(review.Unavailable):
        review.provider_review(CANDIDATE, DIFF.decode(), "fixture", opener)
    assert len(opener.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        completion("Partial finding", finish="length"),
        completion("Tool-looking result", tool_calls=[{"id": "untrusted"}]),
    ],
)
def test_partial_or_tool_response_is_preserved_without_approval(body):
    status, reason, text = review.provider_review(
        CANDIDATE, DIFF.decode(), "fixture", FakeOpener(FakeResponse(body))
    )
    assert status == "unavailable" and text and "partial text is retained" in reason


def test_http_error_is_bounded_redacted_and_not_retried():
    opener = FakeOpener(
        urllib.error.HTTPError(
            review.PROVIDER_URL, 429, "secret-provider-detail", {}, None
        )
    )
    with pytest.raises(review.Unavailable, match="status 429") as error:
        review.provider_review(CANDIDATE, DIFF.decode(), "fixture", opener)
    assert "secret-provider-detail" not in str(error.value)
    assert len(opener.calls) == 1


def test_malformed_credential_never_reaches_provider_or_leaks_value():
    github = FakeGitHub()
    result = review.perform_review(
        github, CANDIDATE, 100, lambda: "secret\nheader-value", forbidden
    )
    assert result["status"] == "unavailable"
    assert result["provider_requests"] == 0
    assert "secret" not in json.dumps(result)


def test_default_opener_disables_ambient_proxy_and_redirects(monkeypatch):
    handlers = []

    def build(*args):
        handlers.extend(args)
        return FakeOpener(FakeResponse(completion()))

    monkeypatch.setattr(review.urllib.request, "build_opener", build)
    review.provider_review(CANDIDATE, DIFF.decode(), "fixture")
    assert isinstance(handlers[0], urllib.request.ProxyHandler)
    assert handlers[0].proxies == {}
    assert isinstance(handlers[1], review.NoRedirect)


def test_redirect_is_refused_before_forwarding_credentials():
    handler = review.NoRedirect()
    with pytest.raises(review.Unavailable, match="redirect"):
        handler.redirect_request(
            urllib.request.Request(review.PROVIDER_URL),
            None,
            302,
            "redirect",
            {},
            "https://attacker.invalid",
        )


def test_model_markup_and_shell_text_are_comment_data():
    result = CANDIDATE.result(
        "reviewed",
        "Fixture.",
        review="```\n$(touch /tmp/unwanted)\n@someone\n" + CANDIDATE.marker,
    )
    body = review.comment_body(CANDIDATE, result)
    assert body.startswith(CANDIDATE.marker + "\n")
    assert "~~~text\n```\n$(touch /tmp/unwanted)" in body
    assert body.endswith("\n~~~\n")


def test_hostile_fences_cannot_exceed_comment_budget():
    text = "`" * (review.MAX_REVIEW_BYTES // 2) + "~" * (review.MAX_REVIEW_BYTES // 2)
    result = CANDIDATE.result("reviewed", "Fixture.", review=text)
    assert len(review.comment_body(CANDIDATE, result).encode()) < 60000


def test_maximum_escaped_diff_stays_within_input_budget():
    prefix = "diff --git a/x b/x\n+"
    diff = prefix + "\\" * (review.MAX_DIFF_BYTES - len(prefix))
    opener = FakeOpener(FakeResponse(completion()))
    review.provider_review(CANDIDATE, diff, "fixture", opener)
    assert len(opener.calls[0][0].data) <= review.MAX_INPUT_BYTES


def test_workflow_has_only_trusted_dispatch_and_conditional_provider_credentials():
    # This wiring assertion complements behavioral tests above. It deliberately
    # needs no YAML package in the repository's deterministic test dependencies.
    workflow = (ROOT / ".github/workflows/external-pr-review.yml").read_text()
    triggers = workflow.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
    assert [
        line
        for line in triggers.splitlines()
        if line.startswith("  ") and not line.startswith("   ")
    ] == ["  workflow_dispatch:"]
    assert "permissions:\n  contents: read\n  pull-requests: write\n" in workflow
    assert "cancel-in-progress: false\n  queue: max" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "ref: ${{ github.sha }}\n          persist-credentials: false" in workflow
    before, after = workflow.split("      - name: Produce one external advisory\n", 1)
    provider = after.split("      - name: Preserve advisory outcome\n", 1)[0]
    assert "OPENROUTER_API_KEY" not in before
    assert "--check" in before
    assert "if: steps.eligibility.outputs.eligible == 'true'" in provider
    assert workflow.count("OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}") == 1
    assert "install" not in workflow.lower()
