"""Narrow authenticated external-runtime HTTP surface for the authority."""

from aiohttp import web

from .delegated_grant_service import install_delegated_grant_routes
from .managed_configuration import (
    _fields,
    profile_capabilities,
    validate_credentials,
    validate_managed_config,
)
from src.core.collaboration.managed_runtime import ClaimNotAllocated, scope


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
    credentials = validate_credentials(credentials)

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
                **profile_capabilities(profile),
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
