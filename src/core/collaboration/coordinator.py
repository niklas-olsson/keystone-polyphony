"""Coordinator managing task lifecycle, assignments, submissions, and adapter orchestration."""

from typing import Optional, Dict, Any, Tuple, List
import time
from .models import (
    TaskObjective,
    TaskStatus,
    AssignmentClaim,
    ContributionSubmission,
    ReviewReceipt,
    ReviewVerdict,
    ResourceEnvelope,
)
from .ledger import FencedLedger
from .adapter import BaseExecutionAdapter, AttemptHandle, AttemptLimits
from .experimental_boundary import MANAGED_TASK_REFUSAL, reserved_task_id


class CollaborationCoordinator:
    def __init__(self, ledger: FencedLedger, execution_adapter: BaseExecutionAdapter):
        self.ledger = ledger
        self.adapter = execution_adapter
        self.active_handles: Dict[str, AttemptHandle] = {}

    def register_task(
        self,
        task_id: str,
        title: str,
        description: str,
        baseline_sha: str,
        acceptance_criteria: List[str],
        max_budget: float = 100.0,
        max_attempts: int = 5,
    ) -> TaskObjective:
        task = TaskObjective(
            task_id=task_id,
            title=title,
            description=description,
            baseline_sha=baseline_sha,
            acceptance_criteria=acceptance_criteria,
            envelope=ResourceEnvelope(max_budget=max_budget, max_attempts=max_attempts),
        )
        return self.ledger.create_task(task)

    def dispatch_attempt(
        self,
        task_id: str,
        actor_id: str,
        reserve_cost: float = 10.0,
        lease_duration: float = 300.0,
        limits: Optional[AttemptLimits] = None,
        now: Optional[float] = None,
    ) -> Tuple[bool, Optional[AttemptHandle], str]:
        success, claim, reason = self.ledger.acquire_assignment(
            task_id=task_id,
            actor_id=actor_id,
            reserve_cost=reserve_cost,
            lease_duration=lease_duration,
            now=now,
        )
        if not success or not claim:
            return False, None, reason

        task = self.ledger.get_task(task_id)
        assert task is not None

        try:
            handle = self.adapter.launch(
                task_id=task_id,
                attempt_id=claim.assignment_id,
                fence_token=claim.fence_token,
                actor_id=actor_id,
                baseline_ref=task.baseline_sha,
                limits=limits,
                metadata={"attempt_token": claim.attempt_token},
            )
            self.active_handles[claim.assignment_id] = handle
            return True, handle, "Attempt dispatched"
        except Exception as e:
            # If launch fails, cancel assignment immediately
            self.ledger.cancel_assignment(
                task_id, claim.assignment_id, settled_cost=0.0, now=now
            )
            return False, None, f"Execution adapter launch error: {e}"

    def submit_attempt(
        self,
        task_id: str,
        assignment_id: str,
        actor_id: str,
        fence_token: int,
        request_id: str,
        diff_text: str,
        actual_cost: float,
        now: Optional[float] = None,
    ) -> Tuple[bool, Optional[ContributionSubmission], str]:
        return self.ledger.submit_contribution(
            task_id=task_id,
            assignment_id=assignment_id,
            actor_id=actor_id,
            fence_token=fence_token,
            request_id=request_id,
            diff_text=diff_text,
            actual_cost=actual_cost,
            now=now,
        )

    def cancel_attempt(
        self,
        task_id: str,
        assignment_id: str,
        now: Optional[float] = None,
    ) -> Tuple[bool, bool, str]:
        """Returns (cancel_acknowledged, termination_confirmed, message)."""
        handle = self.active_handles.get(assignment_id)
        if reserved_task_id(task_id) or (
            handle is not None and reserved_task_id(handle.task_id)
        ):
            return False, False, MANAGED_TASK_REFUSAL
        if handle is not None and handle.task_id != task_id:
            return False, False, "Attempt handle belongs to another experimental task"
        termination_confirmed = False
        settled_cost = 0.0

        if handle:
            termination_confirmed, settled_cost = self.adapter.cancel(handle)
            del self.active_handles[assignment_id]

        success, reason = self.ledger.cancel_assignment(
            task_id=task_id,
            assignment_id=assignment_id,
            settled_cost=settled_cost,
            now=now,
        )
        return success, termination_confirmed, reason

    def record_review(
        self,
        candidate_id: str,
        task_id: str,
        reviewer_actor_id: str,
        verdict: ReviewVerdict,
        blocking_findings: int,
        notes: str = "",
        now: Optional[float] = None,
    ) -> Tuple[bool, Optional[ReviewReceipt], str]:
        return self.ledger.record_review(
            candidate_id=candidate_id,
            task_id=task_id,
            reviewer_actor_id=reviewer_actor_id,
            verdict=verdict,
            blocking_findings=blocking_findings,
            notes=notes,
            now=now,
        )
