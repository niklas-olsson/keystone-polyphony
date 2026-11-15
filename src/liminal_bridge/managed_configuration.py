"""Pure policy, credential and capability validation shared by native consumers."""

import json
import math

from src.core.collaboration.authority import digest, immutable_revision
from src.core.collaboration.child_outcomes import OUTCOME_VERSION
from src.core.collaboration.delegated_grants import (
    DELEGATED_GRANT_POLICY_FIELDS,
    delegated_grant_policy,
)
from src.core.collaboration.managed_runtime import bounded_text, repository_identity

ROLE_OPERATIONS = {
    "owner": {"enroll", "revise", "accept", "admission", "observe"},
    "worker": {
        "start_attempt",
        "acquire",
        "write_resource",
        "save_work",
        "candidate",
        "problem",
        "respond",
    },
    "reviewer": {"evidence"},
    "conductor": {"advise"},
    "ingress": set(),
    "runtime": set(),
}


def _fields(value, required, name):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ValueError(f"{name} has unsupported or missing fields")
    return value


def _names(value, name, empty=False):
    if (
        not isinstance(value, list)
        or (not empty and not value)
        or len(value) > 100
        or any(not isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{name} must be a bounded list of distinct identifiers")
    for item in value:
        bounded_text(item, name)
    return value


def _seconds(value, name):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 < value <= 604800
    ):
        raise ValueError(f"{name} must be positive and at most seven days")


def validate_managed_config(value):
    if value is None:
        return None
    _fields(
        value, {"schema_version", "deployment_id", "repositories"}, "managed config"
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported managed config schema")
    bounded_text(value["deployment_id"], "deployment_id")
    repositories = value["repositories"]
    if not isinstance(repositories, dict) or not 1 <= len(repositories) <= 1000:
        raise ValueError("one or more approved repositories required")
    for repository, policy in repositories.items():
        repository_identity(repository)
        _fields(
            policy,
            {"checks", "reviewers", "max_attempts", "budget_seconds", "profiles"},
            "repository policy",
        )
        _names(policy["checks"], "checks")
        _names(policy["reviewers"], "reviewers")
        if (
            type(policy["max_attempts"]) is not int
            or not 1 <= policy["max_attempts"] <= 1000
        ):
            raise ValueError("max_attempts must be a positive integer at most 1000")
        _seconds(policy["budget_seconds"], "budget_seconds")
        if (
            not isinstance(policy["profiles"], dict)
            or not 1 <= len(policy["profiles"]) <= 100
        ):
            raise ValueError("approved profiles required")
        for name, profile in policy["profiles"].items():
            bounded_text(name, "profile")
            if not isinstance(profile, dict):
                raise ValueError("profile must be an object")
            required = {"base", "attempt_seconds", "capabilities"}
            optional = set(profile) & DELEGATED_GRANT_POLICY_FIELDS
            _fields(profile, required | optional, "profile")
            immutable_revision(profile["base"])
            _seconds(profile["attempt_seconds"], "attempt_seconds")
            if profile["attempt_seconds"] > policy["budget_seconds"]:
                raise ValueError("attempt duration exceeds repository task budget")
            _names(profile["capabilities"], "capabilities", empty=True)
            if optional:
                if (
                    "max_children" not in profile
                    or type(profile["max_children"]) is not int
                    or not 0 <= profile["max_children"] <= 64
                ):
                    raise ValueError("max_children must be an integer from 0 to 64")
                if profile["max_children"] or optional != {"max_children"}:
                    if optional != DELEGATED_GRANT_POLICY_FIELDS:
                        raise ValueError(
                            "enabled child profiles require every child policy field"
                        )
                    _seconds(profile["max_child_seconds"], "max_child_seconds")
                    if profile["max_child_seconds"] > profile["attempt_seconds"]:
                        raise ValueError("child duration exceeds root duration")
                    if (
                        type(profile["max_child_turns"]) is not int
                        or not 1 <= profile["max_child_turns"] <= 1000
                    ):
                        raise ValueError(
                            "max_child_turns must be an integer from 1 to 1000"
                        )
                    _names(
                        profile["child_capabilities"], "child_capabilities", empty=True
                    )
                    if not set(profile["child_capabilities"]) <= set(
                        profile["capabilities"]
                    ):
                        raise ValueError(
                            "child capabilities exceed parent capabilities"
                        )
    # Copy configuration so a caller cannot mutate policy after validation.
    return json.loads(json.dumps(value, allow_nan=False))


def validate_principal_scope(principal):
    if principal["role"] in {"runtime", "ingress"} or "repositories" in principal:
        bounded_text(principal.get("deployment_id"), "credential deployment_id")
        _names(principal.get("repositories"), "credential repositories")
        for repository in principal["repositories"]:
            repository_identity(repository)


def validate_credentials(value):
    """Validate explicit principals without requiring optional role coverage.

    A well-formed scope may be unavailable in current managed policy. Existing
    request authorization owns that decision; validation grants no access.
    """
    if not isinstance(value, dict) or not value:
        raise ValueError("explicit credential mapping required")
    for token, principal in value.items():
        if (
            not isinstance(token, str)
            or len(token) < 32
            or not isinstance(principal, dict)
            or not isinstance(principal.get("identity"), str)
            or not principal["identity"]
            or not isinstance(principal.get("role"), str)
            or principal["role"] not in ROLE_OPERATIONS
        ):
            raise ValueError("invalid credential configuration")
        # Bearer comparison and authenticated identities require valid UTF-8.
        token.encode("utf-8")
        principal["identity"].encode("utf-8")
        validate_principal_scope(principal)
    return json.loads(json.dumps(value, allow_nan=False))


def profile_capabilities(profile):
    """Project native capabilities without changing approved profile bytes."""
    child_policy = delegated_grant_policy(profile)
    return {
        "base": profile["base"] if profile else None,
        "profile_digest": digest(profile) if profile else None,
        "attempt_seconds": profile["attempt_seconds"] if profile else None,
        "capabilities": list(profile["capabilities"]) if profile else [],
        "managed_dispatch": profile is not None,
        "external_root": profile is not None,
        "children": False,
        "child_grants": child_policy is not None,
        "child_initial_outcome": OUTCOME_VERSION,
        "child_policy": child_policy,
        "profile_supported": profile is not None,
    }
