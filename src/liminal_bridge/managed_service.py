"""Narrow authenticated external-runtime HTTP surface for the authority."""

import json
import math

from aiohttp import web

from src.core.collaboration.authority import digest, immutable_revision
from src.core.collaboration.delegated_grants import (
    DELEGATED_GRANT_POLICY_FIELDS,
    delegated_grant_policy,
)
from src.core.collaboration.child_outcomes import OUTCOME_VERSION
from .delegated_grant_service import install_delegated_grant_routes
from src.core.collaboration.managed_runtime import (
    ClaimNotAllocated,
    bounded_text,
    repository_identity,
    scope,
)


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


def principal_allows(principal, binding):
    if principal["role"] == "owner" and "repositories" not in principal:
        return True
    return bool(binding) and (
        principal.get("deployment_id") == binding["deployment_id"]
        and binding["repository"] in principal.get("repositories", ())
    )


def protect_legacy_call(store, principal, operation, payload):
    """Experimental/unscoped entry points cannot claim managed task ownership."""
    task = None
    with store.connect() as db:
        if "task_id" in payload:
            task = store._get(db, "task", payload["task_id"])
        for field, kind in (
            ("attempt_id", "attempt"),
            ("candidate_id", "candidate"),
            ("advice_id", "advice"),
        ):
            if field in payload:
                record = store._get(db, kind, payload[field])
                task = store._get(db, "task", record["task"])
    binding = task.get("managed_scope") if task else None
    if binding:
        if not principal_allows(principal, binding):
            raise web.HTTPForbidden()
        if principal["role"] not in {"owner", "reviewer", "conductor"}:
            raise web.HTTPForbidden()
    elif "repositories" in principal:
        raise web.HTTPForbidden()


def install_managed_routes(app, store, credentials, principal_key, config):
    config = validate_managed_config(config)
    for principal in credentials.values():
        validate_principal_scope(principal)

    def authorize(request, fields, roles):
        if config is None:
            raise web.HTTPServiceUnavailable(
                text="managed runtime policy is not configured"
            )
        principal = request[principal_key]
        binding = scope(fields["deployment_id"], fields["repository"])
        if principal["role"] not in roles or not principal_allows(principal, binding):
            raise web.HTTPForbidden()
        if binding["deployment_id"] != config["deployment_id"]:
            raise web.HTTPForbidden()
        policy = config["repositories"].get(binding["repository"])
        if policy is None:
            raise web.HTTPForbidden()
        return principal, binding, policy

    def query(request, extra=()):
        expected = {"deployment_id", "repository", *extra}
        if set(request.query) != expected or any(
            len(request.query.getall(key)) != 1 for key in expected
        ):
            raise ValueError("unsupported or missing query fields")
        return dict(request.query)

    async def body(request, extra):
        payload = _fields(
            await request.json(),
            {"schema_version", "deployment_id", "repository", *extra},
            "managed request",
        )
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise ValueError("unsupported managed request schema")
        return payload

    async def capabilities(request):
        fields = query(request, {"profile"})
        _, binding, policy = authorize(
            request, fields, {"runtime", "ingress", "owner", "reviewer"}
        )
        profile = policy["profiles"].get(fields["profile"])
        return web.json_response(
            {
                "schema_version": 1,
                **binding,
                "service": "keystone-polyphony",
                "protocol": "managed-runtime-v1",
                "profile": fields["profile"],
                "base": profile["base"] if profile else None,
                "profile_digest": digest(profile) if profile else None,
                "attempt_seconds": profile["attempt_seconds"] if profile else None,
                "capabilities": profile["capabilities"] if profile else [],
                "managed_dispatch": profile is not None,
                "external_root": profile is not None,
                "children": False,
                "child_grants": delegated_grant_policy(profile) is not None,
                "child_initial_outcome": OUTCOME_VERSION,
                "child_policy": delegated_grant_policy(profile),
                "profile_supported": profile is not None,
                "reason": "" if profile else "approved profile not found",
            }
        )

    async def snapshot(request):
        _, binding, _ = authorize(
            request,
            query(request),
            {"runtime", "ingress", "owner", "reviewer", "conductor"},
        )
        return web.json_response(store.scoped_snapshot(binding))

    async def artifact(request):
        _, binding, _ = authorize(
            request, query(request), {"runtime", "ingress", "owner", "reviewer"}
        )
        return web.Response(
            text=store.scoped_artifact(request.match_info["id"], binding)
        )

    async def enroll(request):
        fields = await body(request, {"source", "request_key", "title", "requirements"})
        _, binding, policy = authorize(request, fields, {"ingress"})
        task = store.managed_enroll(
            **binding,
            **{
                key: fields[key]
                for key in ("source", "request_key", "title", "requirements")
            },
            policy=policy,
        )
        return web.json_response({"schema_version": 1, **binding, "task": task})

    async def claim(request):
        fields = await body(
            request, {"profile", "worker_id", "task_id", "request_key", "base"}
        )
        principal, binding, policy = authorize(request, fields, {"runtime"})
        profile = policy["profiles"].get(fields["profile"])
        try:
            result = store.managed_claim(
                **binding,
                runtime_principal=principal["identity"],
                profile_spec=profile,
                **{
                    key: fields[key]
                    for key in (
                        "profile",
                        "worker_id",
                        "task_id",
                        "request_key",
                        "base",
                    )
                },
            )
        except ClaimNotAllocated:
            return web.json_response(
                {
                    "error": "canonical claim was not allocated",
                    "code": "claim_not_allocated",
                    "allocated": False,
                },
                status=409,
            )
        return web.json_response(result)

    async def permission(request):
        fields = await body(request, {"attempt_id"})
        principal, binding, policy = authorize(request, fields, {"runtime"})
        return web.json_response(
            store.managed_permission(
                **binding,
                runtime_principal=principal["identity"],
                attempt_id=fields["attempt_id"],
                profiles=policy["profiles"],
            )
        )

    async def capture(request):
        fields = await body(request, {"attempt_id", "kind", "content"})
        principal, binding, _ = authorize(request, fields, {"runtime"})
        return web.json_response(
            store.managed_capture(
                **binding,
                runtime_principal=principal["identity"],
                **{key: fields[key] for key in ("attempt_id", "kind", "content")},
            )
        )

    async def observe(request):
        fields = await body(
            request,
            {
                "attempt_id",
                "state",
                "termination_confirmed",
                "launch_fenced",
                "artifacts",
            },
        )
        principal, binding, _ = authorize(request, fields, {"runtime"})
        attempt = store.managed_observe(
            **binding,
            runtime_principal=principal["identity"],
            **{
                key: fields[key]
                for key in (
                    "attempt_id",
                    "state",
                    "termination_confirmed",
                    "launch_fenced",
                    "artifacts",
                )
            },
        )
        return web.json_response({"schema_version": 1, **binding, "attempt": attempt})

    async def candidate(request):
        fields = await body(request, {"attempt_id", "artifact_id"})
        principal, binding, _ = authorize(request, fields, {"runtime"})
        result = store.managed_candidate(
            **binding,
            runtime_principal=principal["identity"],
            attempt_id=fields["attempt_id"],
            artifact_id=fields["artifact_id"],
        )
        return web.json_response({"schema_version": 1, **binding, "candidate": result})

    install_delegated_grant_routes(app, store, authorize, body, query)
    for name, handler in (
        ("enroll", enroll),
        ("claim", claim),
        ("permission", permission),
        ("capture", capture),
        ("observe", observe),
        ("candidate", candidate),
    ):
        app.router.add_post("/managed/" + name, handler)
    app.router.add_get("/managed/capabilities", capabilities)
    app.router.add_get("/managed/snapshot", snapshot)
    app.router.add_get("/managed/artifacts/{id}", artifact)
