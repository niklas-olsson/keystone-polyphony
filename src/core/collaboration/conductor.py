"""Self-organizing Conductor agent and advisory nudges.

Strictly non-coding and non-blocking: observes contention and telemetry,
emits deduplicated advisory nudges with cooldowns/expiries, and guides workers to uncontested tasks.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set
import time
import hashlib
from .models import (
    TaskObjective,
    TaskStatus,
    HostSignalType,
    HostTelemetrySignal,
    ConductorLease,
)
from .ledger import FencedLedger
from .experimental_boundary import require_experimental_task, reserved_task_id


@dataclass
class AdvisoryNudge:
    nudge_id: str
    task_id: str
    target_actor_id: Optional[str]
    suggested_action: str
    rationale: str
    suggested_alternative_task_ids: List[str] = field(default_factory=list)
    expires_at: float = field(default_factory=lambda: time.time() + 300.0)
    created_at: float = field(default_factory=time.time)

    def is_expired(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        return t > self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nudge_id": self.nudge_id,
            "task_id": self.task_id,
            "target_actor_id": self.target_actor_id,
            "suggested_action": self.suggested_action,
            "rationale": self.rationale,
            "suggested_alternative_task_ids": self.suggested_alternative_task_ids,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
        }


class EnsembleConductor:
    """
    Cognitive Conductor: observes coordinator telemetry and task contention.
    Does NOT write code or edit files. Emits advisory nudges to route workers.
    """

    def __init__(self, ledger: FencedLedger, nudge_cooldown_seconds: float = 60.0):
        self.ledger = ledger
        self.nudge_cooldown = nudge_cooldown_seconds
        self.emitted_nudges: Dict[str, float] = {}  # nudge_fingerprint -> timestamp
        self.active_nudges: List[AdvisoryNudge] = []
        self.contention_counts: Dict[str, int] = {}  # task_id -> count
        self.decomposition_proposals: Dict[str, Dict[str, Any]] = {}

    def observe_contention_and_nudge(
        self,
        contested_task_id: str,
        blocked_actor_id: str,
        now: Optional[float] = None,
    ) -> Optional[AdvisoryNudge]:
        """
        Invoked when a worker encounters 409 Conflict on a task.
        Identifies alternative eligible tasks and emits a deduplicated advisory nudge.
        """
        if reserved_task_id(contested_task_id):
            return None
        current_time = now if now is not None else time.time()

        # Find uncontested eligible tasks
        all_tasks = self.ledger.list_tasks()
        uncontested = [
            t.task_id
            for t in all_tasks
            if t.task_id != contested_task_id
            and not reserved_task_id(t.task_id)
            and t.status in (TaskStatus.OPEN, TaskStatus.INTEGRATING)
            and t.current_assignment_id is None
        ]

        # Fingerprint for deduplication and cooldown
        fingerprint = hashlib.sha256(
            f"{contested_task_id}:{blocked_actor_id}".encode()
        ).hexdigest()

        last_emitted = self.emitted_nudges.get(fingerprint, 0.0)
        if (current_time - last_emitted) < self.nudge_cooldown:
            # Under cooldown; do not spam nudges
            return None

        nudge_id = f"nudge_{fingerprint[:12]}"
        # Track contention count
        self.contention_counts[contested_task_id] = (
            self.contention_counts.get(contested_task_id, 0) + 1
        )

        action = (
            f"Pivot to alternative uncontested tasks: {uncontested[:3]}"
            if uncontested
            else "Wait with exponential backoff or assist in integration review"
        )
        rationale = f"Task {contested_task_id} is currently claimed and contested (count: {self.contention_counts[contested_task_id]})."

        # Check if contention crosses threshold for dynamic decomposition (e.g. >= 2)
        if (
            self.contention_counts[contested_task_id] >= 2
            and contested_task_id not in self.decomposition_proposals
        ):
            proposal = self.propose_task_decomposition(contested_task_id)
            if proposal:
                action += f" | Proposed decomposition: {proposal['subtask_ids']}"

        nudge = AdvisoryNudge(
            nudge_id=nudge_id,
            task_id=contested_task_id,
            target_actor_id=blocked_actor_id,
            suggested_action=action,
            rationale=rationale,
            suggested_alternative_task_ids=uncontested[:5],
            expires_at=current_time + 180.0,
            created_at=current_time,
        )

        self.emitted_nudges[fingerprint] = current_time
        self.active_nudges.append(nudge)
        return nudge

    def propose_task_decomposition(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Dynamically proposes decomposing a contested task into an abstract interface/stub
        and parallel implementation leaves to unlock uncontested execution.
        """
        if reserved_task_id(task_id):
            return None
        task = self.ledger.get_task(task_id)
        if not task:
            return None

        interface_task_id = f"{task_id}_protocol"
        impl1_task_id = f"{task_id}_impl_core"
        impl2_task_id = f"{task_id}_impl_extended"

        proposal = {
            "original_task_id": task_id,
            "strategy": "interface_first_decomposition",
            "protocol_task": {
                "task_id": interface_task_id,
                "title": f"[Protocol/Stub] {task.title}",
                "description": f"Define interface contracts, types, and stubs for {task_id}.",
            },
            "subtask_ids": [interface_task_id, impl1_task_id, impl2_task_id],
            "reason": f"High contention on {task_id} (count {self.contention_counts.get(task_id, 0)}). Split into protocol and parallel leaves.",
        }
        self.decomposition_proposals[task_id] = proposal
        return proposal

    def apply_decomposition(self, task_id: str) -> List[TaskObjective]:
        """Creates the proposed subtasks in the ledger and marks original task HELD."""
        require_experimental_task(task_id)
        proposal = self.decomposition_proposals.get(task_id)
        if not proposal:
            proposal = self.propose_task_decomposition(task_id)
            if not proposal:
                return []

        require_experimental_task(proposal["original_task_id"])
        for child_id in proposal["subtask_ids"]:
            require_experimental_task(child_id)

        orig = self.ledger.get_task(task_id)
        if not orig:
            return []

        subtasks = []
        for st_id in proposal["subtask_ids"]:
            st = TaskObjective(
                task_id=st_id,
                title=f"Subtask {st_id} for {orig.title}",
                description=f"Decomposed component from contested task {task_id}",
                baseline_sha=orig.baseline_sha,
                acceptance_criteria=orig.acceptance_criteria,
            )
            created = self.ledger.create_task(st)
            subtasks.append(created)

        # Update original task to HELD
        with self.ledger._get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET status = ? WHERE task_id = ?",
                (TaskStatus.HELD.value, task_id),
            )

        return subtasks

    def get_active_nudges_for_actor(
        self, actor_id: str, now: Optional[float] = None
    ) -> List[AdvisoryNudge]:
        current_time = now if now is not None else time.time()
        # Filter unexpired nudges for this actor or broadcast (target None)
        valid = [
            n
            for n in self.active_nudges
            if not n.is_expired(current_time)
            and (n.target_actor_id is None or n.target_actor_id == actor_id)
        ]
        return valid

    def evaluate_and_act(
        self,
        actor_id: str,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Evaluates current swarm health, dynamically claims the conductor lease if eligible,
        and emits host signals (SCALE_UP, SPAWN_REVIEWER, IDLE_DRAIN) if anomalies/bottlenecks are detected.
        """
        current_time = now if now is not None else time.time()
        result = {
            "claimed_lease": False,
            "fence_token": None,
            "emitted_signals": [],
        }

        # 1. Check if actor already holds Conductor lease or can claim it
        current_lease = self.ledger.get_conductor_lease(now=current_time)
        if current_lease and current_lease.actor_id == actor_id:
            result["fence_token"] = current_lease.fence_token
        else:
            # Attempt to claim lease
            ok_claim, lease, _ = self.ledger.acquire_conductor_lease(
                actor_id=actor_id,
                lease_ttl=90.0,
                now=current_time,
            )
            if ok_claim and lease:
                result["claimed_lease"] = True
                result["fence_token"] = lease.fence_token
                current_lease = lease

        all_tasks = [
            task
            for task in self.ledger.list_tasks()
            if not reserved_task_id(task.task_id)
        ]
        integrating_tasks = [t for t in all_tasks if t.status == TaskStatus.INTEGRATING]
        open_tasks = [t for t in all_tasks if t.status == TaskStatus.OPEN]

        # 2. Check for review deadlocks (integrating tasks stuck)
        for t in integrating_tasks:
            # If conductor is held, or if this actor authored a candidate on an integrating task
            ok_sig, sig, _ = self.ledger.emit_host_signal(
                actor_id=actor_id,
                signal_type=HostSignalType.SPAWN_REVIEWER,
                requested_worker_delta=1,
                target_task_ids=[t.task_id],
                rationale=f"Task {t.task_id} is awaiting independent review verification",
                now=current_time,
            )
            if ok_sig and sig:
                result["emitted_signals"].append(sig.to_dict())

        # 3. If actor is active Conductor, check for high backlog needing SCALE_UP
        if current_lease and current_lease.actor_id == actor_id:
            if len(open_tasks) >= 3:
                target_ids = [t.task_id for t in open_tasks[:5]]
                ok_scale, scale_sig, _ = self.ledger.emit_host_signal(
                    actor_id=actor_id,
                    signal_type=HostSignalType.SCALE_UP,
                    requested_worker_delta=min(len(open_tasks), 3),
                    target_task_ids=target_ids,
                    rationale=f"Backlog pressure: {len(open_tasks)} open tasks pending execution",
                    now=current_time,
                )
                if ok_scale and scale_sig:
                    result["emitted_signals"].append(scale_sig.to_dict())

        return result


class WorkerFlowManager:
    """
    Manages client/worker loop invariants:
    - Never spin or block indefinitely on a contested baton.
    - If 409 Conflict is received, immediately pivot to an uncontested eligible task or review.
    """

    def __init__(self, actor_id: str, conductor: EnsembleConductor):
        self.actor_id = actor_id
        self.conductor = conductor

    def handle_contention(
        self,
        task_id: str,
        now: Optional[float] = None,
    ) -> Optional[str]:
        """
        Receives 409 Conflict. Requests a conductor nudge and returns the next task ID to pivot to.
        """
        nudge = self.conductor.observe_contention_and_nudge(
            contested_task_id=task_id,
            blocked_actor_id=self.actor_id,
            now=now,
        )
        if nudge and nudge.suggested_alternative_task_ids:
            return nudge.suggested_alternative_task_ids[0]
        return None
