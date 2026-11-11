#!/usr/bin/env python3
"""Run a review with bounded, free-model provider failover.

Only provider/invocation failures advance to another candidate. A completed review,
including a FAIL verdict, is returned unchanged for the independent admission gate.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from liminal_bridge.model_picker import get_model_candidates  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
DIAGNOSTIC_SCAN_BYTES = 1024 * 1024
DIAGNOSTIC_LINE_BYTES = 65536
DIAGNOSTIC_COUNT_LIMIT = 9999
DIAGNOSTIC_BYTE_LIMIT = 1_000_000_000
EVENT_KINDS = ("text", "error", "step_start", "step_finish", "tool_use", "other")
ERROR_CLASSES = (
    "APIError",
    "ProviderAuthError",
    "ContextOverflowError",
    "MessageAbortedError",
    "UnknownError",
    "other",
)
FINISH_REASONS = ("stop", "length", "content-filter", "tool-calls", "error", "other")
HTTP_STATUSES = (400, 401, 402, 403, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504)


def invocation_diagnostic(
    stdout, stderr, *, attempt, exit_code, timed_out, json_events
):
    """Summarize a bounded prefix without retaining provider or tool content.

    Every emitted string is a fixed label. Counts and status codes are facts
    about this invocation, never a review verdict or permission to retry.
    """
    stdout, stderr = (
        (
            value.encode("utf-8", errors="replace")
            if isinstance(value, str)
            else value or b""
        )
        for value in (stdout, stderr)
    )
    summary = {
        "version": 1,
        "attempt": attempt,
        "exit_code": (
            exit_code if type(exit_code) is int and -128 <= exit_code <= 255 else None
        ),
        "timed_out": timed_out,
        "json_events": json_events,
        "stdout_bytes": min(len(stdout), DIAGNOSTIC_BYTE_LIMIT),
        "stderr_bytes": min(len(stderr), DIAGNOSTIC_BYTE_LIMIT),
        "byte_counts_capped": max(len(stdout), len(stderr)) > DIAGNOSTIC_BYTE_LIMIT,
        "scan_truncated": len(stdout) > DIAGNOSTIC_SCAN_BYTES,
        "counters_capped": False,
        "invalid_json": 0,
        "oversized_lines": 0,
        "permission_rejections": 0,
        "tool_errors": 0,
        "events": dict.fromkeys(EVENT_KINDS, 0),
        "error_classes": dict.fromkeys(ERROR_CLASSES, 0),
        "finish_reasons": dict.fromkeys(FINISH_REASONS, 0),
        "http_statuses": [],
        "other_http_status": False,
    }

    def increment(mapping, key):
        if mapping[key] < DIAGNOSTIC_COUNT_LIMIT:
            mapping[key] += 1
        else:
            summary["counters_capped"] = True

    if json_events:
        prefix = stdout[:DIAGNOSTIC_SCAN_BYTES]
        if isinstance(prefix, bytes):
            prefix = prefix.decode("utf-8", errors="replace")
        for raw_line in prefix.splitlines():
            if len(raw_line.encode("utf-8")) > DIAGNOSTIC_LINE_BYTES:
                increment(summary, "oversized_lines")
                continue
            line = ANSI.sub("", raw_line).strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                increment(summary, "invalid_json")
                # OpenCode 1.2.10 emits this warning outside its JSON events.
                # Retain only its occurrence, never requested scopes or paths.
                if "permission requested:" in line and "; auto-rejecting" in line:
                    increment(summary, "permission_rejections")
                continue
            if not isinstance(event, dict):
                increment(summary, "invalid_json")
                continue
            kind = event.get("type")
            kind = kind if isinstance(kind, str) and kind in EVENT_KINDS else "other"
            increment(summary["events"], kind)
            part = event.get("part")
            part = part if isinstance(part, dict) else {}
            if kind == "error":
                error = event.get("error")
                error = error if isinstance(error, dict) else {}
                name = error.get("name")
                name = (
                    name if isinstance(name, str) and name in ERROR_CLASSES else "other"
                )
                increment(summary["error_classes"], name)
                data = error.get("data")
                data = data if isinstance(data, dict) else {}
                status = data.get("statusCode")
                if type(status) is int and status in HTTP_STATUSES:
                    if status not in summary["http_statuses"]:
                        summary["http_statuses"].append(status)
                elif status is not None:
                    summary["other_http_status"] = True
            elif kind == "step_finish":
                reason = part.get("reason")
                reason = (
                    reason
                    if isinstance(reason, str) and reason in FINISH_REASONS
                    else "other"
                )
                increment(summary["finish_reasons"], reason)
            elif kind == "tool_use":
                state = part.get("state")
                if isinstance(state, dict) and state.get("status") == "error":
                    increment(summary, "tool_errors")
        summary["http_statuses"].sort()
    return summary


def report_invocation(stdout, stderr, **facts):
    summary = invocation_diagnostic(stdout, stderr, **facts)
    print(
        "Review invocation diagnostic: " + json.dumps(summary, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


def decode_events(output):
    """Extract assistant text and top-level errors from OpenCode JSON events.

    Tool failures are deliberately not provider failures: the model can recover
    from an unsuccessful file lookup and still complete an accurate review.
    """
    text = []
    errors = []
    for line in output.splitlines():
        line = ANSI.sub("", line).strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # Keep diagnostics separate from authoritative assistant output.
            errors.append("Invalid OpenCode JSON event")
            continue
        if not isinstance(event, dict):
            errors.append("Invalid OpenCode JSON event")
        elif event.get("type") == "error":
            errors.append("OpenCode provider error")
        elif event.get("type") == "text":
            part = event.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text.append(part["text"])
    return "\n".join(text), errors


def run(args, candidates=None):
    candidates = candidates or get_model_candidates("review")
    if not args.model_flag:
        raise ValueError("Automatic model selection requires a model flag")
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    try:
        position = command.index(args.model_flag) + 1
        command[position]
    except (ValueError, IndexError) as error:
        raise ValueError(
            "Review command must contain its model flag and value"
        ) from error
    if args.json_events:
        command.extend(["--format", "json"])
    last_code = 1
    for attempt, raw_model in enumerate(candidates[:3], 1):
        model = "openrouter/" + raw_model.removeprefix("openrouter/")
        command[position] = model
        Path(args.model_output).write_text(model + "\n")
        print(f"Trying review model: {model}", file=sys.stderr, flush=True)
        try:
            with (
                open(args.stdin_file, "rb")
                if args.stdin_file
                else open("/dev/null", "rb")
            ) as prompt:
                result = subprocess.run(
                    command,
                    stdin=prompt,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=args.timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired as error:
            report_invocation(
                error.stdout,
                error.stderr,
                attempt=attempt,
                exit_code=None,
                timed_out=True,
                json_events=args.json_events,
            )
            print(
                "Review model invocation timed out; trying next candidate",
                file=sys.stderr,
            )
            last_code = 124
            continue
        report_invocation(
            result.stdout,
            result.stderr,
            attempt=attempt,
            exit_code=result.returncode,
            timed_out=False,
            json_events=args.json_events,
        )
        output = result.stdout.decode("utf-8", errors="replace")
        errors = []
        if args.json_events:
            output, errors = decode_events(output)
            if not output.strip():
                errors.append("No assistant review text returned")
        # Never shop for an approving model after a completed review. Even when
        # the process exits unsuccessfully, a structured verdict ends failover.
        completed_verdict = "REVIEW_RESULT:" in output
        failed = result.returncode != 0 or bool(errors)
        if not failed or completed_verdict:
            print(output, end="" if output.endswith("\n") else "\n")
            return (result.returncode or 1) if failed else 0
        last_code = result.returncode or 1
        print(
            "Review provider/invocation failed; trying next candidate", file=sys.stderr
        )
    print("All ranked free review models failed", file=sys.stderr)
    return last_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdin-file")
    parser.add_argument("--model-output", required=True)
    parser.add_argument("--model-flag", default="--model")
    parser.add_argument("--json-events", action="store_true")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        return run(args)
    except (ValueError, OSError, RuntimeError) as error:
        print(f"Free review selection failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
