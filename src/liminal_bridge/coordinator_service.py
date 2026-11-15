"""Authenticated local service. Run: python -m src.liminal_bridge.coordinator_service.

Credential file is owner-provisioned JSON: bearer -> {identity, role}. Identities
are stable principals, not worker labels. Different tokens for one principal do
not establish reviewer independence. All principals share the trusted host.
"""

import argparse
import asyncio
import hmac
import json
import os
from pathlib import Path

from aiohttp import ClientSession, web

from .coordinator import Conflict, Coordinator
from .worktree_adapter import run_attempt
from .approved_inputs import PublicRequirements
from .managed_service import install_managed_routes, protect_legacy_call
from .managed_configuration import (
    ROLE_OPERATIONS,
    validate_credentials,
    validate_managed_config,
)
from .managed_installation import InstallationInputError, read_installation_inputs

PRINCIPAL = web.RequestKey("principal", dict)


# Reserved for server-injected identity. Never accept these from remote payloads.
IDENTITY_FIELD = {
    "start_attempt": "worker",
    "acquire": "worker",
    "write_resource": "worker",
    "save_work": "worker",
    "candidate": "worker",
    "problem": "worker",
    "respond": "worker",
    "evidence": "reviewer",
    "advise": "source",
}


def create_app(
    store, credentials, adapter=None, execution=None, input_provider=None, managed=None
):
    credentials = validate_credentials(credentials)
    managed = validate_managed_config(managed)
    input_provider = input_provider or PublicRequirements()
    if execution is not None:
        if adapter is None:
            raise ValueError("execution template requires an adapter")
        # Reject unsupported hosts before workers allocate task-wide budgets.
        # The authority/read/recovery service can still run without execution.
        adapter.validate_execution()

    @web.middleware
    async def auth(request, handler):
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
        principal = next(
            (
                p
                for key, p in credentials.items()
                if hmac.compare_digest(bearer.encode(), key.encode())
            ),
            None,
        )
        if principal is None:
            raise web.HTTPUnauthorized()
        request[PRINCIPAL] = principal
        if principal["role"] in {"runtime", "ingress"} and not request.path.startswith(
            "/managed/"
        ):
            raise web.HTTPForbidden()
        if "repositories" in principal and request.path in {
            "/snapshot",
            "/events",
            "/execute",
            "/cancel",
        }:
            raise web.HTTPForbidden()
        try:
            return await handler(request)
        except Conflict as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    app = web.Application(middlewares=[auth], client_max_size=5_000_000)

    executions = {}

    def capture_unresolved(active):
        return active is not None and (
            not active.done()
            or active.cancelled()
            or active.exception() is not None
            or active.result() is None
        )

    async def snapshot(request):
        if adapter:
            for attempt in store.snapshot()["attempt"]:
                if attempt.get("execution_mode") == "external":
                    continue
                active = executions.get(attempt["id"])
                if capture_unresolved(active):
                    if (
                        active.done()
                        and not attempt["termination_confirmed"]
                        and attempt["state"] not in {"unknown", "cancel_requested"}
                    ):
                        # Reconcile a failed capture after storage recovers,
                        # without confirming death or blocking independent work.
                        store.observe(attempt["id"], "unknown")
                    continue  # Preserve work before releasing its reservation.
                if (
                    not attempt["termination_confirmed"]
                    and attempt["state"] != "reserved"
                ):
                    state = await adapter.observe(attempt["id"])
                    if state["state"] != attempt["state"]:
                        store.observe(
                            attempt["id"],
                            state["state"],
                            state["termination_confirmed"],
                        )
        return web.json_response(
            store.snapshot()
            if request[PRINCIPAL]["role"] == "owner"
            else store.scoped_snapshot()
        )

    async def execute(request):
        principal = request[PRINCIPAL]
        if principal["role"] != "worker" or adapter is None or execution is None:
            raise web.HTTPForbidden()
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("object payload required")
        attempts = store.snapshot()["attempt"]
        attempt = next(
            (a for a in attempts if a["id"] == payload.get("attempt_id")), None
        )
        if not attempt or attempt["worker"] != principal["identity"]:
            raise web.HTTPForbidden()
        aid = attempt["id"]
        # Launch intent is persisted before launching. A lost launch response may
        # only observe/reconcile; a restart must never duplicate an unknown launch.
        if aid not in executions:
            attempt, requirements = store.authorize_launch(aid, principal["identity"])

            async def supervise():
                try:
                    approved_env = execution.get("env")
                    if execution.get("isolated_home"):
                        home = adapter.root / f"{aid}.home"
                        home.mkdir(mode=0o700, exist_ok=True)
                        approved_env = dict(approved_env or {}) | {"HOME": str(home)}
                        approved_env.update(
                            XDG_CONFIG_HOME=str(home / "config"),
                            XDG_DATA_HOME=str(home / "data"),
                            XDG_CACHE_HOME=str(home / "cache"),
                            XDG_STATE_HOME=str(home / "state"),
                        )
                        approved_env.pop("OPENCODE_DB", None)
                    return await run_attempt(
                        store,
                        adapter,
                        attempt,
                        execution["base"],
                        execution["command"],
                        env=approved_env,
                        input_text=input_provider.worker_input(
                            attempt["task"], requirements
                        ),
                    )
                except Exception:
                    state = adapter.results.get(aid)
                    unlaunched = (
                        aid not in adapter.watchers
                        and aid not in adapter.running
                        and (state is None or state.get("not_started") is True)
                    )
                    if unlaunched:
                        # This service uniquely claimed a fresh reserved launch.
                        # Input/permission preparation failed before any spawn.
                        confirmed = store.observe(aid, "exited", True)
                    else:
                        store.observe(aid, "unknown")
                    store.problem(
                        aid,
                        attempt["worker"],
                        "execution/capture failed; preserve work before verified owner recovery",
                    )
                    return (
                        {"execution": confirmed, "not_started": True}
                        if unlaunched
                        else None
                    )

            executions[aid] = asyncio.create_task(supervise())
        return web.json_response({"attempt": aid, "state": "submitted"})

    async def cancel(request):
        principal = request[PRINCIPAL]
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("object payload required")
        attempt = next(
            (
                a
                for a in store.snapshot()["attempt"]
                if a["id"] == payload.get("attempt_id")
            ),
            None,
        )
        if (
            not attempt
            or principal["role"] != "worker"
            or attempt["worker"] != principal["identity"]
            or attempt.get("execution_mode") == "external"
            or adapter is None
        ):
            raise web.HTTPForbidden()
        cancelled = store.request_cancel(attempt["id"], principal["identity"])
        if cancelled["termination_confirmed"]:
            return web.json_response(cancelled)
        result = await adapter.cancel(attempt["id"])
        active = executions.get(attempt["id"])
        if active is not None:
            try:
                await asyncio.shield(active)
            except asyncio.CancelledError:
                if not active.cancelled():
                    raise  # The HTTP caller cancelled, not the execution task.
            except Exception:
                pass  # Failed capture remains reserved for verified recovery.
            if capture_unresolved(active):
                raise Conflict(
                    "work capture unresolved; preserve work before owner recovery"
                )
            result = await adapter.observe(attempt["id"])
        confirmed = store.observe(
            attempt["id"], result["state"], result["termination_confirmed"]
        )
        return web.json_response(
            confirmed
            if confirmed["termination_confirmed"]
            and not result["termination_confirmed"]
            else result
        )

    async def cleanup(app):
        # Stop serving protected requests while already approved independent
        # actions finish under their original adapter execution limits.
        await asyncio.gather(*executions.values(), return_exceptions=True)

    app.on_cleanup.append(cleanup)

    async def events(request):
        cursor = int(request.query.get("after", 0))
        timeout = min(30, max(0, float(request.query.get("timeout", 30))))
        deadline = asyncio.get_running_loop().time() + timeout
        # Only storage checks while waiting; no inference. Durable sequence means
        # a restart/notification lost between snapshot and wait cannot lose a wake.
        while store.cursor() <= cursor and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(
                min(0.1, max(0, deadline - asyncio.get_running_loop().time()))
            )
        return web.json_response({"cursor": store.cursor()})

    async def artifact(request):
        principal = request[PRINCIPAL]
        if "repositories" in principal:
            raise web.HTTPForbidden()
        reader = (
            store.artifact if principal["role"] == "owner" else store.scoped_artifact
        )
        return web.Response(text=reader(request.match_info["id"]))

    async def call(request):
        op = request.match_info["operation"]
        principal = request[PRINCIPAL]
        if op not in ROLE_OPERATIONS[principal["role"]]:
            raise web.HTTPForbidden()
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("object payload required")
            protect_legacy_call(store, principal, op, payload)
            # Server policy, never conductor-controlled throttling.
            if op == "advise" and any(
                k in payload for k in ("minimum_interval", "ttl")
            ):
                raise ValueError("advice limits are fixed by service policy")
            field = IDENTITY_FIELD.get(op)
            if field:
                if field in payload:
                    raise ValueError("caller identity must not be supplied")
                payload[field] = principal["identity"]
            result = getattr(store, op)(**payload)
            return web.json_response({"result": result})
        except Conflict as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    install_managed_routes(app, store, credentials, PRINCIPAL, managed)
    app.router.add_post("/execute", execute)
    app.router.add_post("/cancel", cancel)
    app.router.add_get("/snapshot", snapshot)
    app.router.add_get("/events", events)
    app.router.add_get("/artifacts/{id}", artifact)
    app.router.add_post("/call/{operation}", call)
    return app


class CoordinatorClient:
    """Remote workers use this interface, never a network-mounted SQLite DB."""

    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    async def request(self, method, path, payload=None):
        async with ClientSession(headers=self.headers) as session:
            async with session.request(
                method, self.url + path, json=payload
            ) as response:
                if response.status == 409:
                    raise Conflict((await response.json())["error"])
                response.raise_for_status()
                return await response.json()

    async def call(self, operation, **payload):
        return (await self.request("POST", f"/call/{operation}", payload))["result"]

    async def snapshot(self):
        return await self.request("GET", "/snapshot")

    async def wait(self, cursor, timeout=30):
        return await self.request(
            "GET", f"/events?after={int(cursor)}&timeout={min(30, timeout)}"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=".liminal/coordinator.db")
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--managed-config", help="owner-only managed repository/profile policy JSON"
    )
    parser.add_argument(
        "--execution-config",
        help="owner-only JSON: repository, root, base, command argv",
    )
    args = parser.parse_args(argv)
    try:
        credentials, managed = read_installation_inputs(
            args.credentials, args.managed_config
        )
    except InstallationInputError as exc:
        parser.error(f"{exc.code} at {exc.path}")
    adapter = execution = None
    if args.execution_config:
        from .worktree_adapter import GitWorktreeAdapter

        execution = json.loads(Path(args.execution_config).read_text())
        adapter = GitWorktreeAdapter(execution["repository"], execution["root"])
        execution["env"] = {
            key: os.environ[key]
            for key in execution.get("inherit_env", ["PATH"])
            if key in os.environ
        } | execution.get("env", {})
    web.run_app(
        create_app(
            Coordinator(args.db),
            credentials,
            adapter,
            execution,
            managed=managed,
        ),
        host="127.0.0.1",
        port=args.port,
    )


if __name__ == "__main__":
    main()
