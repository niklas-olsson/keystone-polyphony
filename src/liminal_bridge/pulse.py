import json
import time
from typing import Any

try:
    from src.core.collaboration.experimental_boundary import ManagedTaskRefusal
except ImportError:
    from core.collaboration.experimental_boundary import ManagedTaskRefusal

try:
    from .architect import Architect
    from .decisions import DecisionAdvisor
except ImportError:
    from architect import Architect
    from decisions import DecisionAdvisor


def _serialize_for_json(obj):
    """Convert objects to JSON-serializable format."""
    if isinstance(obj, set):
        return list(obj)
    elif isinstance(obj, dict):
        return {k: _serialize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_serialize_for_json(item) for item in obj]
    else:
        return obj


class Pulse:
    def __init__(self, mesh: Any, architect: Architect, decision_advisor=None):
        self.mesh = mesh
        self.architect = architect
        self.decision_advisor = (
            decision_advisor if decision_advisor is not None else DecisionAdvisor()
        )
        self.last_decision = None
        self.last_consultation = 0.0
        self.cooldown = 300.0  # 5 minutes between automatic consultations

    async def trigger(self, context: str = "routine_check"):
        """
        Manually triggers a consultation if cooldown has passed.
        """
        now = time.time()
        if now - self.last_consultation < self.cooldown and context != "force":
            return

        print(f"PULSE: Triggering Architect consultation ({context})...")

        # Gather state - serialize to ensure JSON compatibility
        raw_state = {
            "thoughts": self.mesh.thoughts,
            "batons": self.mesh.batons,
            "kv": self.mesh.get_all_kv(),
            "context": context,
        }
        swarm_state = _serialize_for_json(raw_state)

        # Consult only at the coordination boundary, never during CRDT merges.
        # Send a bounded summary rather than shared thoughts or arbitrary KV values.
        self.last_decision = None
        if context != "force":
            has_master_plan = bool(self.mesh.get_kv("master_plan"))
            trigger = context.split(":", 1)[0]
            if trigger not in {
                "routine_check",
                "seed_heartbeat",
                "manual",
                "baton_released",
            }:
                trigger = "other"
            tasks = []
            backlog = self.mesh.get_kv("swarm_backlog")
            if not isinstance(backlog, (list, set, tuple)):
                backlog = []
            for item in backlog:
                try:
                    task = json.loads(item)
                    if isinstance(task, dict):
                        tasks.append(
                            {
                                "status": task.get("status"),
                                "priority": task.get("priority"),
                            }
                        )
                except (TypeError, json.JSONDecodeError):
                    continue
            self.last_decision = await self.decision_advisor.evaluate(
                {
                    "context": trigger,
                    "thought_count": len(self.mesh.thoughts),
                    "active_baton_count": len(self.mesh.batons),
                    "has_master_plan": has_master_plan,
                    "tasks": tasks[:100],
                    "task_count": len(tasks),
                }
            )
            if self.last_decision.get("action") == "continue" and has_master_plan:
                self.last_consultation = now
                print("PULSE: Decision advisor recommends continuing the current plan.")
                return

        # Consult
        new_plan_raw = await self.architect.consult(swarm_state)

        try:
            new_plan = json.loads(new_plan_raw)
        except json.JSONDecodeError:
            # Fallback if it's just a string or malformed
            new_plan = {"backlog": new_plan_raw}

        # Broadcast the new plan to KV
        await self.mesh.update_kv("master_plan", new_plan)

        # Execute commands if any
        if isinstance(new_plan, dict) and "commands" in new_plan:
            for cmd_req in new_plan["commands"]:
                target = cmd_req.get("target")
                command = cmd_req.get("command")
                caps = cmd_req.get("capabilities", [])
                if command:
                    print(
                        f"PULSE: Issuing command to {target or 'capable agents'}: {command}"
                    )
                    await self.mesh.broadcast_command(
                        {"type": "architect_execute", "payload": command},
                        target=target,
                        capabilities=caps,
                    )

        self.last_consultation = now
        print("PULSE: Plan updated and commands issued.")

    async def check_baton_health(self):
        """
        Iterates over active batons. If the owner's last thought is older than 5 minutes (300s),
        release the baton by broadcasting baton_release.
        """
        now = time.time()
        for resource, owner in list(self.mesh.batons.items()):
            thought = self.mesh.thoughts.get(owner)
            if not thought:
                continue

            last_activity = thought.get("timestamp", 0)
            if now - last_activity > 300:
                try:
                    self.mesh.require_advisory_resource(resource)
                except ManagedTaskRefusal:
                    continue
                print(
                    f"PULSE: Baton {resource} held by {owner} appears stale. Broadcasting release."
                )
                # We can originate the release from our own node_id, we will update mesh.py to allow it
                await self.mesh.broadcast(
                    {
                        "type": "baton_release",
                        "resource": resource,
                        "force": True,  # Add a force flag for mesh to recognize
                    }
                )

    async def on_baton_release(self, resource: str, agent_id: str):
        """
        Callback when a baton is released.
        If it was a critical resource (e.g. core file), trigger a pulse.
        """
        # Heuristic: If 'main' or 'core' is in the resource name, it's critical
        if "main" in resource or "core" in resource or "api" in resource:
            await self.trigger(context=f"baton_released:{resource} by {agent_id}")
