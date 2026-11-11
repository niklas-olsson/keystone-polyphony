"""Exercise the standalone issue runner without provider or GitHub calls."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "scripts/run-issue-agent.py"


def run_agent(
    tmp_path,
    *,
    event="success",
    exit_code=0,
    router="router-secret",
    legacy="",
    model="",
    timeout="10",
):
    cli = tmp_path / "opencode"
    cli.write_text(f"#!{sys.executable}\n" + """import json, os, pathlib, sys, time
payload = sys.stdin.read()
pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps({'argv':sys.argv[1:], 'stdin':payload,
 'env':{k:os.environ.get(k) for k in ['AGENT_API_KEY','OPENROUTER_API_KEY','ANTHROPIC_API_KEY','OPENAI_API_KEY','HOME','XDG_DATA_HOME','OPENCODE_DB']}}))
if os.environ['EVENT'] == 'timeout': time.sleep(10)
if os.environ['EVENT'] == 'error':
 print(json.dumps({'type':'error','error':{'message':'provider unavailable'}}))
elif os.environ['EVENT'] == 'malformed': print('not an event')
elif os.environ['EVENT'] == 'success':
 print(json.dumps({'type':'text','part':{'text':'Implemented the requested change'}}))
 print(json.dumps({'type':'step_finish','part':{'reason':'stop'}}))
sys.exit(int(os.environ['CLI_EXIT']))
""")
    cli.chmod(0o755)
    prompt = tmp_path / "agent_prompt.txt"
    prompt.write_text("Implement this issue:\n" + "x" * 300_000)
    env = {k: v for k, v in os.environ.items() if not k.endswith("API_KEY")}
    env.update(
        AGENT_CLI=str(cli),
        AGENT_MODEL=model,
        AGENT_CLI_FLAGS='--title "two words"',
        OPENROUTER_API_KEY=router,
        AGENT_API_KEY=legacy,
        AGENT_TIMEOUT_SECONDS=timeout,
        EVENT=event,
        CLI_EXIT=str(exit_code),
        CAPTURE=str(tmp_path / "capture.json"),
        RUNNER_TEMP=str(tmp_path),
        HOME="/shared/home",
        XDG_DATA_HOME="/shared/data",
        OPENCODE_DB="/shared/db",
    )
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--prompt", str(prompt)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    capture = (
        json.loads((tmp_path / "capture.json").read_text())
        if (tmp_path / "capture.json").exists()
        else None
    )
    return result, capture


def test_real_helper_uses_router_secret_and_streams_large_prompt(tmp_path):
    result, capture = run_agent(tmp_path)
    assert result.returncode == 0, result.stderr
    assert capture["argv"][:3] == ["run", "--format", "json"]
    assert (
        "--non-interactive" not in capture["argv"] and "--prompt" not in capture["argv"]
    )
    assert len(capture["stdin"]) > 300_000
    assert all(len(arg) < 1000 for arg in capture["argv"])
    assert "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free" in capture["argv"]
    assert "two words" in capture["argv"]
    assert capture["env"]["OPENROUTER_API_KEY"] == "router-secret"
    assert capture["env"]["OPENAI_API_KEY"] is None
    assert capture["env"]["HOME"] != "/shared/home"
    assert capture["env"]["XDG_DATA_HOME"] != "/shared/data"
    assert capture["env"]["OPENCODE_DB"] is None
    assert "router-secret" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "event,exit_code,expected",
    [("error", 0, 1), ("malformed", 0, 1), ("empty", 0, 1), ("success", 3, 3)],
)
def test_provider_errors_and_incomplete_or_failed_runs_fail_closed(
    tmp_path, event, exit_code, expected
):
    result, _ = run_agent(tmp_path, event=event, exit_code=exit_code)
    assert result.returncode == expected


def test_missing_credentials_fail_before_launch(tmp_path):
    result, capture = run_agent(tmp_path, router="", legacy="")
    assert result.returncode == 2 and capture is None


def test_router_key_preferred_and_legacy_fallback_supported(tmp_path):
    result, capture = run_agent(tmp_path, legacy="legacy-secret")
    assert result.returncode == 0
    assert capture["env"]["OPENROUTER_API_KEY"] == "router-secret"
    other = tmp_path / "fallback"
    other.mkdir()
    result, capture = run_agent(other, router="", legacy="legacy-secret")
    assert result.returncode == 0
    assert capture["env"]["OPENROUTER_API_KEY"] == "legacy-secret"


def test_native_provider_does_not_receive_router_key(tmp_path):
    result, capture = run_agent(
        tmp_path, legacy="anthropic-secret", model="anthropic/test"
    )
    assert result.returncode == 0
    assert capture["env"]["ANTHROPIC_API_KEY"] == "anthropic-secret"
    assert capture["env"]["OPENROUTER_API_KEY"] is None


def test_timeout_is_bounded_and_reports_failure(tmp_path):
    result, _ = run_agent(tmp_path, event="timeout", timeout="1")
    assert result.returncode == 124
