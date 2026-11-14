"""Trusted-runtime endpoints for bounded delegated grants under root attempts."""

from aiohttp import web

from src.core.collaboration.delegated_grants import DelegatedGrantNotAllocated


def install_delegated_grant_routes(app, store, authorize, body, query):
    def context(request, fields, roles):
        principal, binding, policy = authorize(request, fields, roles)
        return (
            {
                **binding,
                "runtime_principal": (
                    None if principal["role"] == "owner" else principal["identity"]
                ),
                "attempt_id": fields["attempt_id"],
            },
            policy["profiles"],
        )

    async def grant(request):
        extra = {
            "attempt_id",
            "request_key",
            "instructions",
            "seconds",
            "max_turns",
            "capabilities",
        }
        fields = await body(request, extra)
        common, profiles = context(request, fields, {"runtime"})
        try:
            result = store.managed_child_grant(
                **common,
                **{key: fields[key] for key in extra - {"attempt_id"}},
                profiles=profiles,
            )
        except DelegatedGrantNotAllocated as exc:
            return web.json_response(
                {"error": str(exc), "code": "child_not_allocated"}, status=409
            )
        return web.json_response(result)

    async def permission(request):
        fields = await body(request, {"attempt_id", "child_id"})
        common, profiles = context(request, fields, {"runtime"})
        return web.json_response(
            store.managed_child_permission(
                **common, child_id=fields["child_id"], profiles=profiles
            )
        )

    async def turn(request):
        extra = {"attempt_id", "child_id", "request_key", "request_digest"}
        fields = await body(request, extra)
        common, profiles = context(request, fields, {"runtime"})
        return web.json_response(
            store.managed_child_turn(
                **common,
                **{key: fields[key] for key in extra - {"attempt_id"}},
                profiles=profiles,
            )
        )

    async def read(request):
        fields = query(request, {"attempt_id"})
        common, profiles = context(request, fields, {"runtime", "owner"})
        return web.json_response(
            store.managed_children_read(
                **common, profiles=profiles, child_id=request.match_info.get("child_id")
            )
        )

    async def artifact(request):
        fields = query(request, {"attempt_id"})
        common, _ = context(request, fields, {"runtime", "owner"})
        return web.Response(
            text=store.managed_child_artifact(
                **common,
                child_id=request.match_info["child_id"],
                artifact_id=request.match_info["artifact_id"],
            )
        )

    async def capture(request):
        extra = {"attempt_id", "child_id", "kind", "content"}
        fields = await body(request, extra)
        common, _ = context(request, fields, {"runtime"})
        return web.json_response(
            store.managed_child_capture(
                **common, **{key: fields[key] for key in extra - {"attempt_id"}}
            )
        )

    async def observe(request):
        extra = {
            "attempt_id",
            "child_id",
            "state",
            "termination_confirmed",
            "launch_fenced",
            "artifacts",
        }
        fields = await body(request, extra)
        common, _ = context(request, fields, {"runtime"})
        child = store.managed_child_observe(
            **common, **{key: fields[key] for key in extra - {"attempt_id"}}
        )
        return web.json_response(
            {
                "schema_version": 1,
                "deployment_id": common["deployment_id"],
                "repository": common["repository"],
                "root_attempt_id": common["attempt_id"],
                "child": child,
            }
        )

    async def control(request):
        action = request.match_info.get("action", "cancel")
        extra = {"attempt_id", "reason"} | (
            {"child_id"} if action == "revoke" else set()
        )
        fields = await body(request, extra)
        common, _ = context(request, fields, {"runtime", "owner"})
        return web.json_response(
            store.managed_child_control(
                **common,
                action=action,
                reason=fields["reason"],
                child_id=fields.get("child_id"),
            )
        )

    for name, handler in (
        ("grant", grant),
        ("permission", permission),
        ("turn", turn),
        ("capture", capture),
        ("observe", observe),
    ):
        app.router.add_post("/managed/children/" + name, handler)
    app.router.add_post("/managed/children/{action:close|revoke}", control)
    app.router.add_post("/managed/cancel", control)
    app.router.add_get("/managed/children", read)
    app.router.add_get("/managed/children/{child_id}", read)
    app.router.add_get("/managed/children/{child_id}/artifacts/{artifact_id}", artifact)
