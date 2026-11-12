"""Tests for task CLI."""

import pytest
import subprocess
import tempfile
import os
import sys
import json


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_task_cli_workflow(temp_db):
    env = os.environ.copy()
    env["PYTHONPATH"] = ".:src"

    # 1. Enroll
    cmd_enroll = [
        sys.executable,
        "scripts/task.py",
        "--db",
        temp_db,
        "enroll",
        "--task-id",
        "t_cli_1",
        "--title",
        "CLI Task",
        "--baseline-sha",
        "base_cli",
        "--criteria",
        "c1,c2",
    ]
    res = subprocess.run(
        cmd_enroll, check=True, stdout=subprocess.PIPE, text=True, env=env
    )
    task_data = json.loads(res.stdout)
    assert task_data["task_id"] == "t_cli_1"

    # 2. Claim
    cmd_claim = [
        sys.executable,
        "scripts/task.py",
        "--db",
        temp_db,
        "claim",
        "--task-id",
        "t_cli_1",
        "--actor-id",
        "w_cli_1",
    ]
    res_claim = subprocess.run(
        cmd_claim, check=True, stdout=subprocess.PIPE, text=True, env=env
    )
    claim_data = json.loads(res_claim.stdout)
    assert claim_data["success"] is True
    asgn_id = claim_data["assignment_id"]
    fence = claim_data["fence_token"]

    # 3. Submit
    diff_text = "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new\n"
    cmd_submit = [
        sys.executable,
        "scripts/task.py",
        "--db",
        temp_db,
        "submit",
        "--task-id",
        "t_cli_1",
        "--assignment-id",
        asgn_id,
        "--actor-id",
        "w_cli_1",
        "--fence",
        str(fence),
        "--diff-file",
        "-",
    ]
    res_sub = subprocess.run(
        cmd_submit,
        input=diff_text,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    sub_data = json.loads(res_sub.stdout)
    assert sub_data["success"] is True
    candidate_id = sub_data["submission"]["candidate_sha"]

    # 4. Review
    cmd_rev = [
        sys.executable,
        "scripts/task.py",
        "--db",
        temp_db,
        "review",
        "--task-id",
        "t_cli_1",
        "--candidate-id",
        candidate_id,
        "--reviewer-id",
        "rev_independent",
        "--verdict",
        "PASS",
        "--blocking-findings",
        "0",
    ]
    res_rev = subprocess.run(
        cmd_rev, check=True, stdout=subprocess.PIPE, text=True, env=env
    )
    rev_data = json.loads(res_rev.stdout)
    assert rev_data["success"] is True

    # 5. Local inspection preserves identity without asserting forge provenance.
    cmd_ev = [
        sys.executable,
        "scripts/task.py",
        "--db",
        temp_db,
        "local-receipt",
        "--task-id",
        "t_cli_1",
    ]
    res_ev = subprocess.run(
        cmd_ev, check=True, stdout=subprocess.PIPE, text=True, env=env
    )
    ev_data = json.loads(res_ev.stdout)
    assert ev_data["receipt_type"] == "polyphony-experimental-local"
    assert ev_data["candidate_id"] == candidate_id
    assert ev_data["release_authorized"] is False
    assert ev_data["managed_authority"] is False
    assert "pr_number" not in ev_data
    assert "head_sha" not in ev_data

    alias = list(cmd_ev)
    alias[4] = "evidence"
    legacy = subprocess.run(alias, check=True, capture_output=True, text=True, env=env)
    assert json.loads(legacy.stdout) == ev_data
    assert "experimental local receipt only" in legacy.stderr
    rejected = subprocess.run(
        alias + ["--pr", "42"], capture_output=True, text=True, env=env
    )
    assert rejected.returncode == 1
    assert "cannot certify GitHub" in json.loads(rejected.stdout)["error"]
