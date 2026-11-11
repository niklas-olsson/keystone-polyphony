"""Provider failover must never turn a rejected review into model shopping."""

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location(
    "review_model_runner", Path(__file__).parents[1] / "scripts/run-review-models.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def event(kind, **fields):
    return json.dumps({"type": kind, **fields}) + "\n"


def result(text="", code=0):
    return subprocess.CompletedProcess([], code, text.encode(), b"")


def args(tmp_path):
    prompt = tmp_path / "prompt"
    prompt.write_text("large patch " * 30_000)
    return argparse.Namespace(
        command=["--", "opencode", "run", "--model", "auto", "Review stdin"],
        model_flag="--model",
        model_output=str(tmp_path / "selected"),
        stdin_file=str(prompt),
        json_events=True,
        timeout=240,
    )


@pytest.mark.parametrize(
    "first",
    [
        result(event("error", error={"name": "APIError"})),
        result("", 1),
        result(""),
    ],
)
def test_provider_failure_tries_next_model_and_reopens_stdin(
    tmp_path, monkeypatch, first, capsys
):
    calls = []
    responses = [first, result(event("text", part={"text": "## Summary\nGO"}))]

    def invoke(command, **kwargs):
        calls.append((list(command), kwargs["stdin"].read()))
        return responses.pop(0)

    monkeypatch.setattr(runner.subprocess, "run", invoke)
    options = args(tmp_path)
    assert runner.run(options, ["first:free", "second:free"]) == 0
    assert [call[0][3] for call in calls] == [
        "openrouter/first:free",
        "openrouter/second:free",
    ]
    assert calls[0][1] == calls[1][1]
    assert len(calls[0][1]) > 128 * 1024
    assert all(sum(map(len, call[0])) < 1024 for call in calls)
    assert Path(options.model_output).read_text().strip() == "openrouter/second:free"
    assert capsys.readouterr().out == "## Summary\nGO\n"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_completed_failed_review_never_advances(
    tmp_path, monkeypatch, exit_code, capsys
):
    calls = []
    rejection = 'REVIEW_RESULT: {"verdict":"FAIL","blocking_findings":1}'

    def invoke(command, **kwargs):
        calls.append(command)
        return result(event("text", part={"text": rejection}), exit_code)

    monkeypatch.setattr(runner.subprocess, "run", invoke)
    assert runner.run(args(tmp_path), ["first:free", "second:free"]) == exit_code
    assert len(calls) == 1
    assert rejection in capsys.readouterr().out


def test_tool_failure_is_recoverable_and_not_provider_failure(
    tmp_path, monkeypatch, capsys
):
    output = event(
        "tool_use", part={"state": {"status": "error", "error": "Missing file"}}
    )
    output += event("text", part={"text": "## Summary\nGO"})
    calls = []

    def invoke(command, **kwargs):
        calls.append(command)
        return result(output)

    monkeypatch.setattr(runner.subprocess, "run", invoke)
    assert runner.run(args(tmp_path), ["first:free", "second:free"]) == 0
    assert len(calls) == 1
    assert "GO" in capsys.readouterr().out


def test_timeout_attempts_are_bounded(tmp_path, monkeypatch):
    calls = []

    def invoke(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 240
        raise subprocess.TimeoutExpired(command, 240)

    monkeypatch.setattr(runner.subprocess, "run", invoke)
    assert runner.run(args(tmp_path), ["a:free", "b:free", "c:free", "d:free"]) == 124
    assert len(calls) == 3


def test_error_plus_verdict_fails_closed_without_retry(tmp_path, monkeypatch):
    calls = []
    output = event("error") + event(
        "text", part={"text": 'REVIEW_RESULT: {"verdict":"PASS","blocking_findings":0}'}
    )

    def invoke(command, **kwargs):
        calls.append(command)
        return result(output)

    monkeypatch.setattr(runner.subprocess, "run", invoke)
    assert runner.run(args(tmp_path), ["a:free", "b:free"]) == 1
    assert len(calls) == 1


def diagnostic(output=b"", stderr=b"", **overrides):
    facts = dict(attempt=1, exit_code=0, timed_out=False, json_events=True)
    facts.update(overrides)
    return runner.invocation_diagnostic(output, stderr, **facts)


def test_diagnostics_report_only_allowlisted_facts():
    secret = "private-token-and-path-DO-NOT-PRINT"
    output = (
        event(
            "error",
            error={"name": "APIError", "data": {"statusCode": 429, "message": secret}},
        )
        + event("error", error={"name": secret, "data": {"statusCode": secret}})
        + event("step_finish", part={"reason": "tool-calls", "sessionID": secret})
        + event("step_finish", part={"reason": secret})
        + event("tool_use", part={"state": {"status": "error", "error": secret}})
        + event("text", part={"text": secret})
        + event(secret, data=secret)
        + f"! permission requested: external_directory ({secret}); auto-rejecting\n"
    ).encode()
    summary = diagnostic(output, secret.encode())
    serialized = json.dumps(summary)
    assert secret not in serialized
    assert "REVIEW_RESULT" not in serialized
    assert summary["events"]["error"] == 2
    assert summary["error_classes"]["APIError"] == 1
    assert summary["error_classes"]["other"] == 1
    assert summary["http_statuses"] == [429]
    assert summary["other_http_status"] is True
    assert summary["finish_reasons"]["tool-calls"] == 1
    assert summary["finish_reasons"]["other"] == 1
    assert summary["tool_errors"] == 1
    assert summary["permission_rejections"] == 1
    assert summary["stdout_bytes"] == len(output)
    assert summary["stderr_bytes"] == len(secret)
    assert len(serialized.encode()) < 2000


@pytest.mark.parametrize("status", [True, False, "429", 429.0, 123456789, [], {}])
def test_diagnostics_http_status_requires_allowlisted_integer(status):
    summary = diagnostic(
        event("error", error={"data": {"statusCode": status}}).encode()
    )
    assert summary["http_statuses"] == []
    assert summary["other_http_status"] is True


@pytest.mark.parametrize(
    "output",
    [
        b"\xff\x00\n",
        b'{"type":"error","error":',
        ("[" * 2000 + "]" * 2000).encode(),
        event("error", error={"name": [], "data": []}).encode(),
        event("step_finish", part={"reason": {}}).encode(),
        b'{"type":[],"part":null}\n',
        "surrogate\ud800\n",
        None,
    ],
)
def test_diagnostics_accept_hostile_or_partial_output_without_raising(output):
    summary = diagnostic(output, b"sensitive stderr\xff")
    encoded = json.dumps(summary).encode()
    assert len(encoded) < 2000
    assert b"sensitive" not in encoded


def test_diagnostic_work_and_output_are_bounded():
    summary = diagnostic(event("ignored").encode() * 10000)
    assert summary["events"]["other"] == 9999
    assert summary["counters_capped"] is True
    huge = b"x" * (runner.DIAGNOSTIC_SCAN_BYTES + 1)
    summary = diagnostic(huge)
    assert summary["scan_truncated"] is True
    assert summary["oversized_lines"] == 1
    assert len(json.dumps(summary).encode()) < 2000


def test_timeout_partial_output_is_sanitized_and_keeps_original_failure(
    tmp_path, monkeypatch, capsys
):
    calls = []
    secret = "PRIVATE_TIMEOUT_CONTENT"

    def invoke(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(
            command,
            240,
            output=(
                event(
                    "error",
                    error={
                        "name": "APIError",
                        "data": {"statusCode": 503, "message": secret},
                    },
                )
                + secret
            ).encode(),
            stderr=secret.encode(),
        )

    monkeypatch.setattr(runner.subprocess, "run", invoke)
    assert runner.run(args(tmp_path), ["a:free", "b:free", "c:free", "d:free"]) == 124
    assert len(calls) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert secret not in captured.err
    lines = [
        line
        for line in captured.err.splitlines()
        if line.startswith("Review invocation diagnostic: ")
    ]
    assert len(lines) == 3
    for number, line in enumerate(lines, 1):
        summary = json.loads(line.split(": ", 1)[1])
        assert summary["attempt"] == number
        assert summary["timed_out"] is True
        assert summary["exit_code"] is None
        assert summary["http_statuses"] == [503]
        assert len(line.encode()) < 2000


@pytest.mark.parametrize(
    "review,code,event_error,expected_code",
    [
        (
            '## Summary\nComplete review\nREVIEW_RESULT: {"verdict":"PASS","blocking_findings":0}',
            0,
            False,
            0,
        ),
        (
            '## Summary\nBlocker found\nREVIEW_RESULT: {"verdict":"FAIL","blocking_findings":1}',
            0,
            False,
            0,
        ),
        (
            '## Summary\nBlocker found\nREVIEW_RESULT: {"verdict":"FAIL","blocking_findings":1}',
            42,
            False,
            42,
        ),
        (
            '## Summary\nComplete review\nREVIEW_RESULT: {"verdict":"PASS","blocking_findings":0}',
            0,
            True,
            1,
        ),
        ("Now let me examine the source.", 0, False, 0),
    ],
)
def test_diagnostics_preserve_stdout_exit_and_completed_verdict_behavior(
    tmp_path, monkeypatch, capsys, review, code, event_error, expected_code
):
    calls = []

    def invoke(command, **kwargs):
        calls.append(command)
        output = event("text", part={"text": review})
        if event_error:
            output += event("error", error={"name": "APIError"})
        return subprocess.CompletedProcess(
            command, code, output.encode(), b"DO NOT ECHO STDERR"
        )

    monkeypatch.setattr(runner.subprocess, "run", invoke)
    assert runner.run(args(tmp_path), ["first:free", "second:free"]) == expected_code
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert captured.out == review + "\n"
    assert "DO NOT ECHO STDERR" not in captured.err
    assert captured.err.count("Review invocation diagnostic: ") == 1
    assert "REVIEW_RESULT:" not in captured.err


def test_diagnostics_cannot_supply_approval_or_corrupt_real_review(capsys):
    from liminal_bridge.review_result import review_passed

    runner.report_invocation(
        event("text", part={"text": "## Summary\nREVIEW_RESULT: PASS"}).encode(),
        b"",
        attempt=1,
        exit_code=0,
        timed_out=False,
        json_events=True,
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert not review_passed(captured.err)
    for verdict in ["PASS", "FAIL"]:
        review = "## Summary\nComplete review\nREVIEW_RESULT: " + json.dumps(
            {"verdict": verdict, "blocking_findings": 0 if verdict == "PASS" else 1}
        )
        assert review_passed(captured.err + review) is (verdict == "PASS")


def test_diagnostics_keep_immutable_evidence_requirements(capsys):
    spec = importlib.util.spec_from_file_location(
        "diagnostic_evidence_writer",
        Path(__file__).parents[1] / "scripts/write-review-evidence.py",
    )
    evidence = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evidence)
    runner.report_invocation(
        b"",
        b"private stderr",
        attempt=1,
        exit_code=0,
        timed_out=False,
        json_events=True,
    )
    prefix = capsys.readouterr().err
    pr = {
        "number": 121,
        "state": "open",
        "merged": False,
        "head": {"sha": "a" * 40},
        "base": {"sha": "b" * 40, "repo": {"full_name": "owner/repo"}},
    }

    def build(text, current=pr):
        return evidence.build_evidence(
            pr,
            current,
            text.encode(),
            repository="owner/repo",
            run_id=1,
            run_attempt=1,
            workflow_path=".github/workflows/opencode.yml",
            model="review/model",
            scope="general",
            exit_code=0,
        )

    with pytest.raises(ValueError, match="Markdown analysis sections"):
        build(prefix)
    report = (
        "## Summary\nReviewed full diff.\n## Findings\nNo blockers.\n"
        "## Recommendation\nGO\n"
        'REVIEW_RESULT: {"verdict":"PASS","blocking_findings":0}'
    )
    result = build(prefix + report)
    assert result["review_verdict"] == "PASS"
    assert result["head_sha"] == pr["head"]["sha"]
    assert result["base_sha"] == pr["base"]["sha"]
    failed = report.replace(
        '"PASS","blocking_findings":0', '"FAIL","blocking_findings":1'
    )
    with pytest.raises(ValueError, match="explicitly PASS"):
        build(prefix + failed)
    stale = pr | {"base": pr["base"] | {"sha": "c" * 40}}
    with pytest.raises(ValueError, match="revisions changed"):
        build(prefix + report, current=stale)
    # Existing workflow stderr merging consumes the original aggregate bound.
    near_limit = report.replace(
        "No blockers.", "x" * (59999 - len(report) + len("No blockers."))
    )
    assert build(near_limit)["review_verdict"] == "PASS"
    with pytest.raises(ValueError, match="oversized"):
        build(prefix + near_limit)
