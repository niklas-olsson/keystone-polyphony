"""Guard the active workflow authority/cost perimeter after retiring staging."""

from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def test_only_external_review_has_provider_credentials_or_model_entrypoints():
    forbidden = (
        "OPENROUTER_API_KEY",
        "DUCKY_API_KEY",
        "AGENT_API_KEY",
        "run-review-models.py",
        "run-issue-agent.py",
        "probe-review-areas.py",
        "demo-decision-ensemble.py",
        "refine_issue.py",
        "deduplicate.py",
        "--mode=seed",
    )
    for path in WORKFLOWS.glob("*.yml"):
        if path.name != "external-pr-review.yml":
            assert not any(token in path.read_text() for token in forbidden), path


def test_old_staging_and_provider_workflows_are_not_active():
    retired = (
        "auto-merge-staging",
        "workflow-review",
        "periodic-merge-main",
        "decision-merge",
        "opencode",
        "workflow-review-dispatch",
        "agent-issue-solver",
        "agent-parallel-solver",
        "agent-issue-triage",
        "decision-model-smoke",
        "swarm-node",
    )
    assert all(not (WORKFLOWS / (name + ".yml")).exists() for name in retired)
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text()
        assert "git merge" not in text and "git push" not in text, path
        assert "secrets: inherit" not in text, path


def test_candidate_validation_is_read_only_and_has_no_provider_or_cache():
    text = (WORKFLOWS / "pr-validation.yml").read_text()
    assert "workflow_dispatch:" in text and "pull_request_target:" not in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "os.environ['GITHUB_SHA'] == base" in text
    assert "contents: read" in text and ": write" not in text
    assert "secrets." not in text and "cache:" not in text
    assert "persist-credentials: false" in text
    assert "python -m pytest -q" in text and "bash scripts/lint.sh" in text
    assert "[p['sha'] for p in commit['parents']] == [base, head]" in text
    assert "decision-merge" not in text and "continue-on-error" not in text


def test_privileged_controller_uses_main_and_review_event_is_only_a_signal():
    text = (WORKFLOWS / "pr-controller.yml").read_text()
    assert "pull_request_review:" not in text
    assert "pull_request:" not in text
    assert "pull_request_target:" in text and "workflow_run:" in text
    assert "ref: main" in text and "ref: ${{" not in text
    assert "queue: max" in text and "cancel-in-progress: true" not in text
    assert "pip install" not in text and "npm " not in text
    signal = (WORKFLOWS / "pr-review-signal.yml").read_text()
    assert "permissions: {}" in signal and "pull_request_review:" in signal
    assert "pull_request_review_thread:" not in signal
    assert "checkout@" not in signal and "secrets." not in signal
