"""Validate an optional initial-instruction report in a final child manifest.

The bound runtime authenticates which child reported. A semantic report is not
proof of tests, termination, independent review, task acceptance or release.
"""

import json
import re

OUTCOME_VERSION = 1
MAX_SUMMARY_BYTES = 8192
REQUEST_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
SEMANTIC_OUTCOMES = frozenset(
    {"completed", "declined", "needs_clarification", "failed"}
)
BINDING_FIELDS = frozenset(
    {
        "child_id",
        "root_attempt_id",
        "parent_attempt_id",
        "task_id",
        "requirements_revision",
        "grant_digest",
        "instructions_digest",
    }
)
OUTCOME_FIELDS = BINDING_FIELDS | {
    "schema_version",
    "work_turn_id",
    "ordinal",
    "source",
    "report",
}
REPORT_FIELDS = frozenset({"request_key", "payload_digest", "outcome", "summary"})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("child outcome manifest has duplicate JSON fields")
        result[key] = value
    return result


def initial_outcome(content, binding):
    """Return a validated extension, or None for an unchanged legacy manifest.

    Validate before occupying the immutable capture slot, and again when saved
    evidence is consumed. Compare to the original grant, not current permission.
    """
    from .authority import digest

    manifest = json.loads(content)
    if not isinstance(manifest, dict):
        raise ValueError("child manifest must be a JSON object")
    if "work_outcome" not in manifest:
        return None
    # Legacy JSON objects remain byte-for-byte compatible. A present extension
    # must have unambiguous fields, including its containing manifest.
    manifest = json.loads(content, object_pairs_hook=_unique_object)
    outcome = manifest["work_outcome"]
    if not isinstance(outcome, dict) or set(outcome) != OUTCOME_FIELDS:
        raise ValueError("child work outcome has unsupported or missing fields")
    if (
        type(outcome["schema_version"]) is not int
        or outcome["schema_version"] != OUTCOME_VERSION
        or type(outcome["ordinal"]) is not int
        or outcome["ordinal"] != 1
        or type(outcome["requirements_revision"]) is not int
    ):
        raise ValueError("invalid child work outcome version or revision")
    if any(outcome[key] != binding[key] for key in BINDING_FIELDS):
        raise ValueError("child work outcome does not match its original grant")
    expected_id = digest(
        ["polyphony-child-work-v1", binding["child_id"], binding["grant_digest"], 1]
    )
    if outcome["work_turn_id"] != expected_id:
        raise ValueError("child work turn identity does not match its original grant")
    report = outcome["report"]
    if report is None:
        if outcome["source"] != "unreported":
            raise ValueError("unreported child outcome requires the unreported source")
        return outcome
    if outcome["source"] != "child_report_via_bound_runtime":
        raise ValueError("reported child outcome requires the bound runtime source")
    if not isinstance(report, dict) or set(report) != REPORT_FIELDS:
        raise ValueError("child work report has unsupported or missing fields")
    if not isinstance(report["request_key"], str) or not REQUEST_KEY.fullmatch(
        report["request_key"]
    ):
        raise ValueError("invalid child work report request key")
    if (
        not isinstance(report["outcome"], str)
        or report["outcome"] not in SEMANTIC_OUTCOMES
    ):
        raise ValueError("invalid child semantic outcome")
    summary = report["summary"]
    if (
        not isinstance(summary, str)
        or "\x00" in summary
        or len(summary.encode("utf-8")) > MAX_SUMMARY_BYTES
    ):
        raise ValueError("child work summary must be at most 8 KiB UTF-8 without NUL")
    payload = {key: report[key] for key in ("request_key", "outcome", "summary")}
    if report["payload_digest"] != digest(payload):
        raise ValueError("child work report payload digest does not match")
    return outcome
