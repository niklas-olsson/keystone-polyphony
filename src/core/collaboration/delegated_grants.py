"""One-level delegated grants in the canonical task and artifact journal.

Only a trusted runtime consumes these grants. Its broker must authenticate guest
contexts independently; a child identifier from guest JSON is never authority.
"""

import hashlib
import json
import math
import re
import secrets

from .errors import Conflict
from .child_outcomes import initial_outcome

DELEGATED_GRANT_POLICY_FIELDS = frozenset(
    {"max_children", "max_child_seconds", "max_child_turns", "child_capabilities"}
)
CAPTURES = frozenset({"patch", "log", "manifest"})


class DelegatedGrantNotAllocated(Conflict):
    """A new delegated grant was conclusively rejected before allocation."""


def delegated_grant_policy(profile):
    """Read defaults without inserting them into an approved profile digest."""
    if not profile or not profile.get("max_children", 0):
        return None
    return {key: profile[key] for key in DELEGATED_GRANT_POLICY_FIELDS}


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("child seconds must be positive and finite")


class DelegatedGrantAuthority:
    def _child_root(self, db, attempt_id, binding, runtime_principal):
        from .managed_runtime import bounded_text

        if runtime_principal is not None:
            return self._managed_attempt(db, attempt_id, binding, runtime_principal)
        # None is injected only by the service for an authenticated owner.
        root = self._get(db, "attempt", bounded_text(attempt_id, "attempt_id"))
        external = root.get("runtime_binding", {})
        if root.get("execution_mode") != "external" or any(
            external.get(key) != value for key, value in binding.items()
        ):
            raise Conflict("root is outside the managed scope")
        return root

    def _child(self, db, root, child_id):
        from .managed_runtime import bounded_text

        child = self._get(db, "child", bounded_text(child_id, "child_id"))
        if child["binding"]["root_attempt_id"] != root["id"]:
            raise Conflict("child is outside the authorized root lineage")
        return child

    def _children(self, db, root_id):
        return [
            child
            for child in self._all(db, "child")
            if child["binding"]["root_attempt_id"] == root_id
        ]

    def _child_permission(self, db, root, child, profile):
        current = self._managed_permission(db, root, profile)
        allowed, reason = current["launch_allowed"], current["reason"]
        if child["termination_confirmed"] or child.get("revoked"):
            allowed, reason = False, "child is terminated or revoked"
        elif (
            child["boot_id"] != self.boot_id
            or self.monotonic_clock() >= child["monotonic_deadline"]
        ):
            allowed, reason = False, "child permission expired"
        remaining = (
            max(
                0,
                min(root["monotonic_deadline"], child["monotonic_deadline"])
                - self.monotonic_clock(),
            )
            if allowed
            else 0
        )
        binding = child["binding"]
        return {
            "schema_version": 1,
            "deployment_id": binding["deployment_id"],
            "repository": binding["repository"],
            "root_attempt_id": root["id"],
            "child": child,
            "binding": binding,
            "instructions": child["instructions"],
            "launch_allowed": allowed,
            "remaining_seconds": remaining,
            "reason": reason,
        }

    def managed_child_grant(
        self,
        deployment_id,
        repository,
        runtime_principal,
        attempt_id,
        request_key,
        instructions,
        seconds,
        max_turns,
        capabilities,
        profiles,
    ):
        from .authority import digest
        from .managed_runtime import bounded_text, scope

        bounded_text(request_key, "request_key")
        bounded_text(instructions, "child instructions", 1_000_000)
        _seconds(seconds)
        if type(max_turns) is not int or max_turns < 1:
            raise ValueError("max_turns must be a positive integer")
        if (
            not isinstance(capabilities, list)
            or len(capabilities) > 100
            or any(not isinstance(value, str) for value in capabilities)
            or len(set(capabilities)) != len(capabilities)
        ):
            raise ValueError("child capabilities must be distinct identifiers")
        for value in capabilities:
            bounded_text(value, "capability")
        payload = {
            "instructions": instructions,
            "seconds": seconds,
            "max_turns": max_turns,
            "capabilities": sorted(capabilities),
        }
        binding = scope(deployment_id, repository)
        key = digest([runtime_principal, attempt_id, request_key])
        with self.transaction() as db:
            root = self._child_root(db, attempt_id, binding, runtime_principal)
            profile = profiles.get(root["runtime_binding"]["profile"])
            prior = db.execute(
                "SELECT body FROM records WHERE kind='child_start' AND id=?", (key,)
            ).fetchone()
            if prior:
                saved = json.loads(prior[0])
                if saved["payload"] != payload:
                    raise Conflict("child retry key payload changed")
                child = self._child(db, root, saved["child_id"])
                return self._child_permission(db, root, child, profile)
            policy = delegated_grant_policy(profile)
            permission = self._managed_permission(db, root, profile)
            if not permission["launch_allowed"]:
                raise DelegatedGrantNotAllocated(permission["reason"])
            if root.get("child_admission_closed") or policy is None:
                raise DelegatedGrantNotAllocated(
                    "child admission is closed or unsupported"
                )
            if len(self._children(db, attempt_id)) >= policy["max_children"]:
                raise DelegatedGrantNotAllocated(
                    "root lifetime child grant limit exhausted"
                )
            if (
                seconds > policy["max_child_seconds"]
                or max_turns > policy["max_child_turns"]
                or not set(capabilities) <= set(policy["child_capabilities"])
            ):
                raise DelegatedGrantNotAllocated(
                    "child request exceeds approved policy"
                )
            task = self._get(db, "task", root["task"])
            if task["allocated_seconds"] + seconds > task["budget_seconds"]:
                raise DelegatedGrantNotAllocated("task time allowance exhausted")
            child_id = "child_" + secrets.token_hex(16)
            immutable = {
                **binding,
                "child_id": child_id,
                "root_attempt_id": attempt_id,
                "parent_attempt_id": attempt_id,
                "task_id": root["task"],
                "runtime_principal": runtime_principal,
                "worker_id": root["runtime_binding"]["worker_id"],
                "profile": root["runtime_binding"]["profile"],
                "profile_digest": root["runtime_binding"]["profile_digest"],
                "requirements_revision": root["requirements_revision"],
                "base": root["runtime_binding"]["base"],
                "launch_key": "polyphony-child:" + child_id,
                "allocated_seconds": seconds,
                "deadline": min(root["deadline"], self.clock() + seconds),
                "max_turns": max_turns,
                "capabilities": sorted(capabilities),
                "instructions_digest": hashlib.sha256(
                    instructions.encode()
                ).hexdigest(),
            }
            immutable["grant_digest"] = digest(immutable)
            child = {
                "id": child_id,
                "binding": immutable,
                "instructions": instructions,
                "boot_id": self.boot_id,
                "monotonic_deadline": min(
                    root["monotonic_deadline"], self.monotonic_clock() + seconds
                ),
                "state": "unknown",
                "termination_confirmed": False,
                "turns_allocated": 0,
            }
            task["allocated_seconds"] += seconds
            self._save(db, "task", task["id"], task)
            self._save(db, "child", child_id, child)
            self._save(
                db, "child_start", key, {"payload": payload, "child_id": child_id}
            )
            return self._child_permission(db, root, child, profile)

    def managed_child_permission(
        self,
        deployment_id,
        repository,
        runtime_principal,
        attempt_id,
        child_id,
        profiles,
    ):
        from .managed_runtime import scope

        with self.transaction() as db:
            root = self._child_root(
                db, attempt_id, scope(deployment_id, repository), runtime_principal
            )
            child = self._child(db, root, child_id)
            profile = profiles.get(root["runtime_binding"]["profile"])
            return self._child_permission(db, root, child, profile)

    def managed_child_turn(
        self,
        deployment_id,
        repository,
        runtime_principal,
        attempt_id,
        child_id,
        request_key,
        request_digest,
        profiles,
    ):
        from .authority import digest
        from .managed_runtime import bounded_text, scope

        bounded_text(request_key, "request_key")
        if not isinstance(request_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", request_digest
        ):
            raise ValueError("request_digest must be a SHA-256 content hash")
        with self.transaction() as db:
            root = self._child_root(
                db, attempt_id, scope(deployment_id, repository), runtime_principal
            )
            child = self._child(db, root, child_id)
            profile = profiles.get(root["runtime_binding"]["profile"])
            key = digest([child_id, request_key])
            prior = db.execute(
                "SELECT body FROM records WHERE kind='child_turn' AND id=?", (key,)
            ).fetchone()
            current = self._child_permission(db, root, child, profile)
            if prior:
                turn = json.loads(prior[0])
                if turn["request_digest"] != request_digest:
                    raise Conflict("child turn retry payload changed")
                return current | {"turn": turn}
            if not current["launch_allowed"]:
                raise Conflict("child turn permission expired or revoked")
            if child["turns_allocated"] >= child["binding"]["max_turns"]:
                raise Conflict("child turn allowance exhausted")
            child["turns_allocated"] += 1
            self._save(db, "child", child_id, child)
            turn = {
                "turn_id": key,
                "child_id": child_id,
                "root_attempt_id": attempt_id,
                "number": child["turns_allocated"],
                "request_digest": request_digest,
            }
            self._save(db, "child_turn", key, turn)
            return self._child_permission(db, root, child, profile) | {"turn": turn}

    def managed_children_read(
        self,
        deployment_id,
        repository,
        runtime_principal,
        attempt_id,
        profiles,
        child_id=None,
    ):
        from .managed_runtime import scope

        with self.transaction() as db:
            root = self._child_root(
                db, attempt_id, scope(deployment_id, repository), runtime_principal
            )
            profile = profiles.get(root["runtime_binding"]["profile"])
            children = (
                [self._child(db, root, child_id)]
                if child_id is not None
                else self._children(db, attempt_id)
            )
            return {
                "schema_version": 1,
                **scope(deployment_id, repository),
                "root_attempt_id": attempt_id,
                "admission_closed": root.get("child_admission_closed", False),
                "children": [
                    self._child_permission(db, root, child, profile)
                    for child in children
                ],
            }

    def managed_child_control(
        self,
        deployment_id,
        repository,
        runtime_principal,
        attempt_id,
        action,
        reason,
        child_id=None,
    ):
        from .managed_runtime import bounded_text, scope

        bounded_text(reason, "reason", 2000)
        if action not in {"close", "cancel", "revoke"}:
            raise ValueError("invalid child control action")
        with self.transaction() as db:
            root = self._child_root(
                db, attempt_id, scope(deployment_id, repository), runtime_principal
            )
            if action == "revoke":
                children = [self._child(db, root, child_id)]
            else:
                children = self._children(db, attempt_id) if action == "cancel" else []
                if not root.get("child_admission_closed"):
                    root.update(
                        child_admission_closed=True, child_admission_reason=reason
                    )
                if action == "cancel" and not root["termination_confirmed"]:
                    root.update(cancel_requested=True, state="cancel_requested")
                self._save(db, "attempt", attempt_id, root)
            for child in children:
                if not child["termination_confirmed"] and not child.get("revoked"):
                    child.update(
                        revoked=True, revocation_reason=reason, state="cancel_requested"
                    )
                    self._save(db, "child", child["id"], child)
            return {
                "schema_version": 1,
                **scope(deployment_id, repository),
                "attempt": root,
                "action": action,
            }

    def managed_child_capture(
        self,
        deployment_id,
        repository,
        runtime_principal,
        attempt_id,
        child_id,
        kind,
        content,
    ):
        from .authority import digest
        from .managed_runtime import scope

        if kind not in CAPTURES or not isinstance(content, str):
            raise ValueError("child patch, log or manifest UTF-8 capture required")
        raw = content.encode()
        if len(raw) > 4_000_000:
            raise ValueError("child capture exceeds 4 MB limit")
        artifact = hashlib.sha256(raw).hexdigest()
        with self.transaction() as db:
            root = self._child_root(
                db, attempt_id, scope(deployment_id, repository), runtime_principal
            )
            child = self._child(db, root, child_id)
            if kind == "manifest":
                # Reject bad new input before it occupies this immutable slot.
                # Historical evidence delivery does not require launch permission.
                initial_outcome(content, child["binding"])
            key = digest([child_id, kind])
            prior = db.execute(
                "SELECT body FROM records WHERE kind='child_capture' AND id=?", (key,)
            ).fetchone()
            if prior:
                saved = json.loads(prior[0])
                if saved["artifact_id"] != artifact:
                    raise Conflict("final child capture already has different content")
                return saved
            if child["termination_confirmed"]:
                raise Conflict("terminated child capture cannot be changed")
            db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?)", (artifact, raw))
            return self._save(
                db,
                "child_capture",
                key,
                {
                    "schema_version": 1,
                    **scope(deployment_id, repository),
                    "root_attempt_id": attempt_id,
                    "child_id": child_id,
                    "kind": kind,
                    "artifact_id": artifact,
                    "runtime_principal": runtime_principal,
                },
            )

    def _verify_child_capture(self, db, child):
        from .authority import canonical, digest

        artifacts = child.get("captured_artifacts", {})
        if set(artifacts) != CAPTURES:
            raise Conflict("child terminal receipt requires every capture")
        manifest = None
        for kind, artifact in artifacts.items():
            saved = self._get(db, "child_capture", digest([child["id"], kind]))
            row = db.execute(
                "SELECT content FROM artifacts WHERE id=?", (artifact,)
            ).fetchone()
            if (
                saved["artifact_id"] != artifact
                or row is None
                or hashlib.sha256(bytes(row[0])).hexdigest() != artifact
            ):
                raise Conflict("child durable capture verification failed")
            if kind == "manifest":
                manifest = bytes(row[0])
        try:
            outcome = initial_outcome(manifest.decode("utf-8"), child["binding"])
        except (ValueError, TypeError, KeyError) as exc:
            raise Conflict("child durable outcome verification failed") from exc
        projection = (
            outcome
            | {
                "artifacts": artifacts,
                "canonical_committed": True,
                "release_authorized": False,
            }
            if outcome is not None
            else None
        )
        if child["termination_confirmed"]:
            try:
                matches = canonical(child.get("initial_outcome")) == canonical(
                    projection
                ) and not (projection is None and "initial_outcome" in child)
            except (ValueError, TypeError):
                matches = False
            if not matches:
                raise Conflict(
                    "child outcome projection does not match durable capture"
                )
        return projection

    def managed_child_observe(
        self,
        deployment_id,
        repository,
        runtime_principal,
        attempt_id,
        child_id,
        state,
        termination_confirmed,
        launch_fenced,
        artifacts,
    ):
        from .managed_runtime import scope

        if state not in {
            "running",
            "unknown",
            "cancel_requested",
            "exited",
            "timed_out",
        }:
            raise ValueError("invalid child execution state")
        if type(termination_confirmed) is not bool or type(launch_fenced) is not bool:
            raise ValueError("child receipt flags must be boolean")
        if not isinstance(artifacts, dict) or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in artifacts.values()
        ):
            raise ValueError("child artifact hash mapping required")
        if termination_confirmed:
            if (
                state not in {"exited", "timed_out"}
                or not launch_fenced
                or set(artifacts) != CAPTURES
            ):
                raise Conflict(
                    "child terminal receipt requires fence and every capture"
                )
        elif artifacts or launch_fenced:
            raise ValueError("nonterminal child cannot assert capture or launch fence")
        receipt = {
            "state": state,
            "termination_confirmed": termination_confirmed,
            "launch_fenced": launch_fenced,
            "artifacts": artifacts,
        }
        with self.transaction() as db:
            root = self._child_root(
                db, attempt_id, scope(deployment_id, repository), runtime_principal
            )
            child = self._child(db, root, child_id)
            if child["termination_confirmed"]:
                if child.get("runtime_receipt") != receipt:
                    raise Conflict("child terminal receipt changed")
                self._verify_child_capture(db, child)
                return child
            if termination_confirmed:
                child["captured_artifacts"] = artifacts
                outcome = self._verify_child_capture(db, child)
                if outcome is not None:
                    child["initial_outcome"] = outcome
                child.update(
                    termination_confirmed=True,
                    launch_fenced=True,
                    runtime_receipt=receipt,
                )
            if child.get("revoked") and not termination_confirmed:
                state = "cancel_requested"
            child["state"] = state
            if state == "cancel_requested":
                child["revoked"] = True
            return self._save(db, "child", child_id, child)

    def managed_child_artifact(
        self,
        deployment_id,
        repository,
        runtime_principal,
        attempt_id,
        child_id,
        artifact_id,
    ):
        from .managed_runtime import scope

        with self.connect() as db:
            root = self._child_root(
                db, attempt_id, scope(deployment_id, repository), runtime_principal
            )
            self._child(db, root, child_id)
            if not any(
                c["child_id"] == child_id and c["artifact_id"] == artifact_id
                for c in self._all(db, "child_capture")
            ):
                raise Conflict("artifact is outside the authorized child lineage")
            row = db.execute(
                "SELECT content FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
            if row is None or hashlib.sha256(bytes(row[0])).hexdigest() != artifact_id:
                raise Conflict("child durable artifact verification failed")
            return bytes(row[0]).decode()

    def _child_terminal_lineage(self, db, root_id):
        lineage = []
        for child in self._children(db, root_id):
            if not child["termination_confirmed"] or not child.get("launch_fenced"):
                raise Conflict("root has an unresolved child execution")
            self._verify_child_capture(db, child)
            lineage.append(
                {
                    "child_id": child["id"],
                    "grant_digest": child["binding"]["grant_digest"],
                    "artifacts": child["captured_artifacts"],
                }
            )
        return sorted(lineage, key=lambda row: row["child_id"])
