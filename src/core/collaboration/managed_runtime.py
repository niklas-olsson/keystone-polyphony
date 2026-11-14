"""External runtime operations on the canonical authority's existing journal.

Only the authenticated service calls these methods. The runtime is a trusted
supervisor, never the guest coding process. A receipt is not independent review.
"""

import hashlib
import json
import re
import secrets

from .errors import Conflict
from .delegated_grants import DelegatedGrantAuthority

CAPTURE_KINDS = frozenset({"patch", "log", "manifest"})


class ClaimNotAllocated(Conflict):
    """The atomic claim rejected before reserving this delivery identity."""


def bounded_text(value, name, limit=512):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > limit
        or "\x00" in value
    ):
        raise ValueError(f"invalid {name}")
    return value


def repository_identity(value):
    bounded_text(value, "repository")
    if value != value.lower() or not re.fullmatch(
        r"github:github\.com/[a-z0-9_.-]+/[a-z0-9_.-]+", value
    ):
        raise ValueError("canonical GitHub repository identity required")
    return value


def scope(deployment_id, repository):
    return {
        "deployment_id": bounded_text(deployment_id, "deployment_id"),
        "repository": repository_identity(repository),
    }


class ManagedRuntimeAuthority(DelegatedGrantAuthority):
    """Mixin sharing Coordinator transactions, budgets, artifacts and review."""

    def managed_enroll(
        self,
        deployment_id,
        repository,
        source,
        request_key,
        title,
        requirements,
        policy,
    ):
        from .authority import Conflict, digest

        binding = scope(deployment_id, repository)
        bounded_text(source, "source")
        bounded_text(request_key, "request_key")
        bounded_text(title, "title", 1000)
        bounded_text(requirements, "requirements", 1_000_000)
        payload = {
            **binding,
            "source": source,
            "request_key": request_key,
            "title": title,
            "requirements": requirements,
            "policy": {
                key: policy[key]
                for key in ("checks", "reviewers", "max_attempts", "budget_seconds")
            },
        }
        key = digest([binding, source, request_key])
        with self.transaction() as db:
            prior = db.execute(
                "SELECT body FROM records WHERE kind='managed_source' AND id=?",
                (key,),
            ).fetchone()
            if prior:
                saved = json.loads(prior[0])
                if saved["payload"] != payload:
                    raise Conflict("source retry payload or enrollment policy changed")
                return self._get(db, "task", saved["task"])
            task_id = "task_v2_" + secrets.token_hex(12)
            task = {
                "id": task_id,
                "schema_version": 2,
                "title": title,
                "requirements": requirements,
                "checks": list(policy["checks"]),
                "reviewers": sorted(set(policy["reviewers"])),
                "required": [],
                "dependencies": [],
                "resource": None,
                "max_attempts": policy["max_attempts"],
                "budget_seconds": policy["budget_seconds"],
                "allocated_seconds": 0,
                "attempts": 0,
                "priority": 1,
                "created": self.clock(),
                "status": "todo",
                "revision": 1,
                "managed_scope": binding,
                "source": source,
                "execution_request_key": request_key,
            }
            self._save(db, "task", task_id, task)
            self._save(db, "managed_source", key, {"payload": payload, "task": task_id})
            return task

    def managed_claim(
        self,
        deployment_id,
        repository,
        runtime_principal,
        worker_id,
        profile,
        profile_spec,
        task_id,
        request_key,
        base,
    ):
        from .authority import Conflict, digest

        binding = scope(deployment_id, repository)
        for value, name in (
            (runtime_principal, "runtime principal"),
            (worker_id, "worker_id"),
            (profile, "profile"),
            (task_id, "task_id"),
            (request_key, "request_key"),
        ):
            bounded_text(value, name)
        requested = {
            **binding,
            "runtime_principal": runtime_principal,
            "worker_id": worker_id,
            "profile": profile,
            "base": base,
        }
        actor = "runtime:" + digest([runtime_principal, worker_id])
        retry = digest(["external", binding, worker_id, request_key])
        with self.transaction() as db:
            prior = db.execute(
                "SELECT body FROM records WHERE kind='start' AND id=?",
                (digest([actor, retry]),),
            ).fetchone()
            if prior:
                saved = json.loads(prior[0])
                original = saved["payload"].get("external", {})
                if saved["payload"]["task"] != task_id or any(
                    original.get(key) != value for key, value in requested.items()
                ):
                    raise Conflict("retry key payload changed")
                attempt = self._managed_attempt(
                    db, saved["attempt"], binding, runtime_principal
                )
                # A revoked profile cannot erase an already allocated attempt
                # or prevent recovering a reply lost before its identity arrived.
                return self._managed_permission(db, attempt, profile_spec)
            if profile_spec is None or base != profile_spec["base"]:
                raise ClaimNotAllocated("approved profile or base is unavailable")
            external = requested | {"profile_digest": digest(profile_spec)}
            task = self._get(db, "task", task_id)
            if task.get("managed_scope") != binding:
                raise ClaimNotAllocated("task is outside the managed repository scope")
            # Reuse the existing task-wide budget and unresolved-root invariant.
            try:
                attempt = self._start_attempt(
                    db,
                    task_id,
                    actor,
                    profile_spec["capabilities"],
                    profile_spec["attempt_seconds"],
                    retry,
                    external=external,
                )
            except Conflict as exc:
                raise ClaimNotAllocated(str(exc)) from None
            return self._managed_permission(db, attempt, profile_spec)

    def _managed_attempt(self, db, attempt_id, binding, runtime_principal):
        from .authority import Conflict

        attempt = self._get(db, "attempt", bounded_text(attempt_id, "attempt_id"))
        external = attempt.get("runtime_binding", {})
        if (
            attempt.get("execution_mode") != "external"
            or external.get("runtime_principal") != runtime_principal
            or any(external.get(key) != value for key, value in binding.items())
        ):
            raise Conflict("attempt is not bound to this runtime and scope")
        return attempt

    def _managed_permission(self, db, attempt, profile_spec):
        from .authority import Conflict, digest

        binding = attempt["runtime_binding"]
        allowed, remaining, reason = False, 0, "attempt permission expired"
        try:
            if not isinstance(attempt.get("approved_requirements"), str):
                raise Conflict("approved external input is missing")
            if (
                profile_spec is None
                or digest(profile_spec) != binding["profile_digest"]
            ):
                raise Conflict("approved runtime profile changed or was removed")
            self._permission(db, attempt["id"], attempt["worker"])
            allowed = True
            remaining = max(0, attempt["monotonic_deadline"] - self.monotonic_clock())
            reason = ""
        except Conflict as exc:
            reason = str(exc)
        return {
            "schema_version": 1,
            "deployment_id": binding["deployment_id"],
            "repository": binding["repository"],
            "attempt": attempt,
            "binding": binding,
            "requirements": attempt.get("approved_requirements", ""),
            "launch_allowed": allowed,
            "remaining_seconds": remaining,
            "reason": reason,
        }

    def managed_permission(
        self, deployment_id, repository, runtime_principal, attempt_id, profiles
    ):
        with self.transaction() as db:
            attempt = self._managed_attempt(
                db, attempt_id, scope(deployment_id, repository), runtime_principal
            )
            profile = profiles.get(attempt["runtime_binding"]["profile"])
            return self._managed_permission(db, attempt, profile)

    def managed_capture(
        self, deployment_id, repository, runtime_principal, attempt_id, kind, content
    ):
        from .authority import Conflict, digest

        if kind not in CAPTURE_KINDS or not isinstance(content, str):
            raise ValueError("patch, log or manifest UTF-8 capture required")
        raw = content.encode()
        if len(raw) > 4_000_000:
            raise ValueError("capture exceeds 4 MB limit")
        if kind == "manifest":
            manifest = json.loads(content)
            if not isinstance(manifest, dict):
                raise ValueError("capture manifest must be a JSON object")
        artifact = hashlib.sha256(raw).hexdigest()
        binding = scope(deployment_id, repository)
        with self.transaction() as db:
            attempt = self._managed_attempt(db, attempt_id, binding, runtime_principal)
            key = digest([attempt_id, kind])
            prior = db.execute(
                "SELECT body FROM records WHERE kind='runtime_capture' AND id=?", (key,)
            ).fetchone()
            if prior:
                saved = json.loads(prior[0])
                if saved["artifact_id"] != artifact:
                    raise Conflict("final capture kind already has different content")
                return saved
            if attempt["termination_confirmed"]:
                raise Conflict("terminated capture cannot be changed")
            db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?)", (artifact, raw))
            if kind == "patch":
                self._save(
                    db,
                    "work",
                    digest([attempt_id, artifact]),
                    {
                        "attempt": attempt_id,
                        "artifact": artifact,
                        "saved": self.clock(),
                    },
                )
            return self._save(
                db,
                "runtime_capture",
                key,
                {
                    "schema_version": 1,
                    **binding,
                    "attempt_id": attempt_id,
                    "kind": kind,
                    "artifact_id": artifact,
                    "runtime_principal": runtime_principal,
                },
            )

    def managed_observe(
        self,
        deployment_id,
        repository,
        runtime_principal,
        attempt_id,
        state,
        termination_confirmed,
        launch_fenced,
        artifacts,
    ):
        from .authority import Conflict, digest

        if state not in {
            "running",
            "unknown",
            "cancel_requested",
            "exited",
            "timed_out",
        }:
            raise ValueError("invalid execution state")
        if type(termination_confirmed) is not bool or type(launch_fenced) is not bool:
            raise ValueError("receipt flags must be boolean")
        if not isinstance(artifacts, dict):
            raise ValueError("artifact hash mapping required")
        if any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in artifacts.values()
        ):
            raise ValueError("artifact references must be SHA-256 content hashes")
        if termination_confirmed:
            if state not in {"exited", "timed_out"} or not launch_fenced:
                raise Conflict("terminal receipt requires termination and launch fence")
            if set(artifacts) != CAPTURE_KINDS:
                raise Conflict("terminal receipt requires patch, log and manifest")
        elif artifacts or launch_fenced:
            raise ValueError("nonterminal receipt cannot assert final capture or fence")
        binding = scope(deployment_id, repository)
        receipt = {
            "state": state,
            "termination_confirmed": termination_confirmed,
            "launch_fenced": launch_fenced,
            "artifacts": artifacts,
        }
        with self.transaction() as db:
            attempt = self._managed_attempt(db, attempt_id, binding, runtime_principal)
            if attempt["termination_confirmed"]:
                if attempt.get("runtime_receipt") != receipt:
                    raise Conflict("terminal receipt changed")
                return attempt
            if termination_confirmed:
                lineage = self._child_terminal_lineage(db, attempt_id)
                for kind, artifact in artifacts.items():
                    saved = self._get(db, "runtime_capture", digest([attempt_id, kind]))
                    if saved["artifact_id"] != artifact:
                        raise Conflict("receipt does not match durable runtime capture")
                    row = db.execute(
                        "SELECT content FROM artifacts WHERE id=?", (artifact,)
                    ).fetchone()
                    if (
                        row is None
                        or hashlib.sha256(bytes(row[0])).hexdigest() != artifact
                    ):
                        raise Conflict("durable artifact verification failed")
                attempt.update(
                    termination_confirmed=True,
                    launch_fenced=True,
                    captured_artifacts=artifacts,
                    runtime_receipt=receipt,
                )
                if lineage:
                    attempt["child_lineage"] = lineage
            if attempt.get("cancel_requested") and not termination_confirmed:
                state = "cancel_requested"
            attempt["state"] = state
            if state == "cancel_requested":
                attempt["cancel_requested"] = True
            return self._save(db, "attempt", attempt_id, attempt)

    def managed_candidate(
        self, deployment_id, repository, runtime_principal, attempt_id, artifact_id
    ):
        with self.connect() as db:
            attempt = self._managed_attempt(
                db, attempt_id, scope(deployment_id, repository), runtime_principal
            )
        return self.candidate(
            attempt_id,
            attempt["worker"],
            attempt["runtime_binding"]["base"],
            artifact_id,
        )

    def scoped_snapshot(self, binding=None):
        """Return one managed scope, or legacy-only state when binding is None."""
        result = self.snapshot()
        tasks = {t["id"] for t in result["task"] if t.get("managed_scope") == binding}
        attempts = {a["id"] for a in result["attempt"] if a["task"] in tasks}
        candidates = {c["id"] for c in result["candidate"] if c["task"] in tasks}
        advice = {a["id"] for a in result["advice"] if a["task"] in tasks}
        resources = {
            t["resource"] for t in result["task"] if t["id"] in tasks and t["resource"]
        }
        filters = {
            "task": lambda row: row["id"] in tasks,
            "attempt": lambda row: row["id"] in attempts,
            "candidate": lambda row: row["id"] in candidates,
            "advice": lambda row: row["id"] in advice,
            "work": lambda row: row["attempt"] in attempts,
            "problem": lambda row: row["task"] in tasks,
            "evidence": lambda row: row["candidate"] in candidates,
            "response": lambda row: row["advice"] in advice,
            "resource": lambda row: row["id"] in resources,
            "destination": lambda row: row.get("resource") in resources,
        }
        for kind, predicate in filters.items():
            result[kind] = [row for row in result[kind] if predicate(row)]
        # Scope-specific sequence, without exposing another repository's events.
        if binding:
            visible = tasks | attempts | candidates | advice | resources
            with self.connect() as db:
                rows = db.execute("SELECT seq,record_id FROM events ORDER BY seq")
                result["cursor"] = sum(1 for _, identity in rows if identity in visible)
            result.update(schema_version=1, **binding)
        return result

    def scoped_artifact(self, artifact_id, binding=None):
        from .authority import Conflict

        snapshot = self.scoped_snapshot(binding)
        attempts = {a["id"] for a in snapshot["attempt"]}
        with self.connect() as db:
            visible = any(
                w["artifact"] == artifact_id and w["attempt"] in attempts
                for w in self._all(db, "work")
            ) or any(
                c["artifact_id"] == artifact_id and c["attempt_id"] in attempts
                for c in self._all(db, "runtime_capture")
            )
        if not visible:
            raise Conflict("artifact is outside the authorized scope")
        return self.artifact(artifact_id)
