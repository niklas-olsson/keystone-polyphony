"""Durable SQLite WAL ledger implementing atomic CAS mutations, leases, and fencing."""

import sqlite3
import json
import time
import os
import hashlib
from typing import Optional, List, Dict, Any, Tuple
from .experimental_boundary import (
    MANAGED_TASK_REFUSAL,
    experimental_host_targets,
    require_experimental_task,
    reserved_task_id,
    stored_experimental_host_targets,
)
from .models import (
    TaskObjective,
    TaskStatus,
    AssignmentClaim,
    AttemptStatus,
    ContributionSubmission,
    CandidateReceipt,
    ReviewReceipt,
    ReviewVerdict,
    ResourceEnvelope,
    HostSignalType,
    HostSignalStatus,
    ConductorLeaseStatus,
    ConductorLease,
    HostTelemetrySignal,
)


class FencedLedger:
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._mem_conn = None
        if self.db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", timeout=10.0)
            self._mem_conn.row_factory = sqlite3.Row
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # Enforce WAL mode and busy timeout for strict concurrent serialization
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)

        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    baseline_sha TEXT NOT NULL,
                    acceptance_criteria TEXT NOT NULL,
                    spec_revision INTEGER NOT NULL DEFAULT 1,
                    schema_version INTEGER NOT NULL DEFAULT 2,
                    status TEXT NOT NULL,
                    envelope TEXT NOT NULL,
                    current_fence_token INTEGER NOT NULL DEFAULT 0,
                    current_assignment_id TEXT,
                    active_attempt_id TEXT,
                    accepted_candidate_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS assignments (
                    assignment_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    actor_id TEXT NOT NULL,
                    fence_token INTEGER NOT NULL,
                    attempt_token TEXT NOT NULL,
                    reserved_cost REAL NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS submissions (
                    receipt_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    assignment_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    fence_token INTEGER NOT NULL,
                    request_id TEXT NOT NULL,
                    diff_text TEXT NOT NULL,
                    candidate_sha TEXT NOT NULL,
                    actual_cost REAL NOT NULL,
                    spec_revision INTEGER NOT NULL,
                    baseline_sha TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(task_id, request_id)
                );

                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    baseline_sha TEXT NOT NULL,
                    patch_sha TEXT NOT NULL,
                    spec_revision INTEGER NOT NULL,
                    author_actor_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reviews (
                    review_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    reviewer_actor_id TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    blocking_findings INTEGER NOT NULL,
                    spec_revision INTEGER NOT NULL,
                    baseline_sha TEXT NOT NULL,
                    notes TEXT,
                    is_valid INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS advisory_nudges (
                    nudge_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    target_actor_id TEXT,
                    suggested_action TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conductor_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    lease_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    fence_token INTEGER NOT NULL,
                    active_worker_count INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS host_signal_outbox (
                    signal_id TEXT PRIMARY KEY,
                    emitted_by_actor TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    requested_worker_delta INTEGER NOT NULL,
                    target_task_ids TEXT NOT NULL,
                    unsettled_cost_snapshot REAL NOT NULL,
                    rationale TEXT NOT NULL,
                    host_status TEXT NOT NULL,
                    host_response_note TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """)

    def create_task(self, task: TaskObjective) -> TaskObjective:
        require_experimental_task(task.task_id)
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, title, description, baseline_sha, acceptance_criteria,
                    spec_revision, schema_version, status, envelope, current_fence_token,
                    current_assignment_id, active_attempt_id, accepted_candidate_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.title,
                    task.description,
                    task.baseline_sha,
                    json.dumps(task.acceptance_criteria),
                    task.spec_revision,
                    task.schema_version,
                    task.status.value,
                    json.dumps(task.envelope.to_dict()),
                    task.current_fence_token,
                    task.current_assignment_id,
                    task.active_attempt_id,
                    task.accepted_candidate_id,
                    task.created_at,
                    task.updated_at,
                ),
            )
            conn.commit()
        return task

    def get_task(self, task_id: str) -> Optional[TaskObjective]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_task(row)

    def list_tasks(self, status: Optional[TaskStatus] = None) -> List[TaskObjective]:
        with self._get_connection() as conn:
            if status:
                cursor = conn.execute(
                    "SELECT * FROM tasks WHERE status = ?", (status.value,)
                )
            else:
                cursor = conn.execute("SELECT * FROM tasks")
            return [self._row_to_task(row) for row in cursor.fetchall()]

    def _row_to_task(self, row: sqlite3.Row) -> TaskObjective:
        return TaskObjective(
            task_id=row["task_id"],
            title=row["title"],
            description=row["description"],
            baseline_sha=row["baseline_sha"],
            acceptance_criteria=json.loads(row["acceptance_criteria"]),
            spec_revision=row["spec_revision"],
            schema_version=row["schema_version"],
            status=TaskStatus(row["status"]),
            envelope=ResourceEnvelope.from_dict(json.loads(row["envelope"])),
            current_fence_token=row["current_fence_token"],
            current_assignment_id=row["current_assignment_id"],
            active_attempt_id=row["active_attempt_id"],
            accepted_candidate_id=row["accepted_candidate_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def acquire_assignment(
        self,
        task_id: str,
        actor_id: str,
        reserve_cost: float = 10.0,
        lease_duration: float = 300.0,
        now: Optional[float] = None,
    ) -> Tuple[bool, Optional[AssignmentClaim], str]:
        """Atomically checks task eligibility, increments fence, reserves budget, and persists assignment."""
        if reserved_task_id(task_id):
            return False, None, MANAGED_TASK_REFUSAL
        current_time = now if now is not None else time.time()

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return False, None, "Task not found"

            task = self._row_to_task(row)

            if task.status not in (
                TaskStatus.OPEN,
                TaskStatus.IN_PROGRESS,
                TaskStatus.INTEGRATING,
                TaskStatus.HELD,
            ):
                conn.rollback()
                return (
                    False,
                    None,
                    f"Task is in non-assignable state: {task.status.value}",
                )

            # Check if active assignment exists and is not expired
            if task.current_assignment_id:
                asgn_cursor = conn.execute(
                    "SELECT * FROM assignments WHERE assignment_id = ?",
                    (task.current_assignment_id,),
                )
                asgn_row = asgn_cursor.fetchone()
                if asgn_row:
                    asgn = AssignmentClaim.from_dict(dict(asgn_row))
                    if asgn.status in (AttemptStatus.ASSIGNED, AttemptStatus.RUNNING):
                        if not asgn.is_expired(current_time):
                            conn.rollback()
                            return (
                                False,
                                None,
                                f"Contested baton: active claim by {asgn.actor_id}",
                            )
                        else:
                            # Expired lease: mark previous as EXPIRED
                            conn.execute(
                                "UPDATE assignments SET status = ? WHERE assignment_id = ?",
                                (AttemptStatus.EXPIRED.value, asgn.assignment_id),
                            )

            # Check budget envelope
            if not task.envelope.can_reserve(reserve_cost):
                conn.rollback()
                return False, None, "Budget envelope exceeded; cannot reserve capacity"

            if task.envelope.attempt_count >= task.envelope.max_attempts:
                conn.rollback()
                return False, None, "Max attempt count reached for this task"

            # Increment fence token monotonically
            new_fence = task.current_fence_token + 1
            assignment_id = f"asgn_{task_id}_{new_fence}"
            attempt_token = hashlib.sha256(
                f"{task_id}:{assignment_id}:{new_fence}:{actor_id}:{current_time}".encode()
            ).hexdigest()

            # Reserve budget & increment attempt count
            task.envelope.reserve(reserve_cost)
            task.envelope.attempt_count += 1
            expires_at = current_time + lease_duration

            claim = AssignmentClaim(
                assignment_id=assignment_id,
                task_id=task_id,
                actor_id=actor_id,
                fence_token=new_fence,
                attempt_token=attempt_token,
                reserved_cost=reserve_cost,
                lease_expires_at=expires_at,
                status=AttemptStatus.ASSIGNED,
                created_at=current_time,
            )

            conn.execute(
                """
                INSERT INTO assignments (
                    assignment_id, task_id, actor_id, fence_token, attempt_token,
                    reserved_cost, lease_expires_at, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim.assignment_id,
                    claim.task_id,
                    claim.actor_id,
                    claim.fence_token,
                    claim.attempt_token,
                    claim.reserved_cost,
                    claim.lease_expires_at,
                    claim.status.value,
                    claim.created_at,
                ),
            )

            conn.execute(
                """
                UPDATE tasks SET
                    current_fence_token = ?,
                    current_assignment_id = ?,
                    active_attempt_id = ?,
                    status = ?,
                    envelope = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    new_fence,
                    assignment_id,
                    assignment_id,
                    TaskStatus.IN_PROGRESS.value,
                    json.dumps(task.envelope.to_dict()),
                    current_time,
                    task_id,
                ),
            )

            conn.commit()
            return True, claim, "Assignment acquired"

    def submit_contribution(
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
        """Atomically validates current fence, idempotency, settles budget, and commits submission."""
        if reserved_task_id(task_id):
            return False, None, MANAGED_TASK_REFUSAL
        current_time = now if now is not None else time.time()

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")

            # Check if this exact request_id has already been committed (Idempotency)
            sub_cursor = conn.execute(
                "SELECT * FROM submissions WHERE task_id = ? AND request_id = ?",
                (task_id, request_id),
            )
            existing_sub = sub_cursor.fetchone()
            if existing_sub:
                existing = ContributionSubmission.from_dict(dict(existing_sub))
                # Check if payload matches
                if existing.diff_text == diff_text and existing.actor_id == actor_id:
                    conn.rollback()
                    return True, existing, "Idempotent retry: existing receipt returned"
                else:
                    conn.rollback()
                    return (
                        False,
                        None,
                        "Reused request_id with conflicting payload rejected",
                    )

            # Check task state
            task_cursor = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            )
            task_row = task_cursor.fetchone()
            if not task_row:
                conn.rollback()
                return False, None, "Task not found"

            task = self._row_to_task(task_row)

            # Atomic CAS: Check fence token validity
            if (
                fence_token != task.current_fence_token
                or assignment_id != task.current_assignment_id
            ):
                conn.rollback()
                return (
                    False,
                    None,
                    f"Superseded fence: attempt fence {fence_token} vs current {task.current_fence_token}",
                )

            # Check assignment row
            asgn_cursor = conn.execute(
                "SELECT * FROM assignments WHERE assignment_id = ?", (assignment_id,)
            )
            asgn_row = asgn_cursor.fetchone()
            if not asgn_row:
                conn.rollback()
                return False, None, "Assignment claim not found"

            asgn = AssignmentClaim.from_dict(dict(asgn_row))
            if asgn.actor_id != actor_id:
                conn.rollback()
                return False, None, "Actor mismatch on assignment"

            if asgn.status in (AttemptStatus.CANCELLED, AttemptStatus.SUPERSEDED):
                conn.rollback()
                return False, None, f"Assignment already {asgn.status.value}"

            # Calculate candidate patch SHA and candidate SHA
            patch_sha = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
            candidate_id = CandidateReceipt.calculate_id(
                task.baseline_sha, patch_sha, task.spec_revision
            )
            receipt_id = f"rcpt_{hashlib.sha256(f'{task_id}:{request_id}:{candidate_id}'.encode()).hexdigest()[:16]}"

            submission = ContributionSubmission(
                task_id=task_id,
                assignment_id=assignment_id,
                actor_id=actor_id,
                fence_token=fence_token,
                request_id=request_id,
                diff_text=diff_text,
                candidate_sha=candidate_id,
                actual_cost=actual_cost,
                spec_revision=task.spec_revision,
                baseline_sha=task.baseline_sha,
                receipt_id=receipt_id,
                created_at=current_time,
            )

            # Settle budget: reconcile reserved vs actual
            task.envelope.settle(asgn.reserved_cost, actual_cost)

            # Insert submission
            conn.execute(
                """
                INSERT INTO submissions (
                    receipt_id, task_id, assignment_id, actor_id, fence_token,
                    request_id, diff_text, candidate_sha, actual_cost,
                    spec_revision, baseline_sha, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission.receipt_id,
                    submission.task_id,
                    submission.assignment_id,
                    submission.actor_id,
                    submission.fence_token,
                    submission.request_id,
                    submission.diff_text,
                    submission.candidate_sha,
                    submission.actual_cost,
                    submission.spec_revision,
                    submission.baseline_sha,
                    submission.created_at,
                ),
            )

            # Upsert candidate record
            conn.execute(
                """
                INSERT OR REPLACE INTO candidates (
                    candidate_id, task_id, baseline_sha, patch_sha, spec_revision, author_actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    task_id,
                    task.baseline_sha,
                    patch_sha,
                    task.spec_revision,
                    actor_id,
                    current_time,
                ),
            )

            # Update assignment status
            conn.execute(
                "UPDATE assignments SET status = ? WHERE assignment_id = ?",
                (AttemptStatus.SUBMITTED.value, assignment_id),
            )

            # Update task status and envelope
            conn.execute(
                """
                UPDATE tasks SET
                    status = ?,
                    envelope = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    TaskStatus.INTEGRATING.value,
                    json.dumps(task.envelope.to_dict()),
                    current_time,
                    task_id,
                ),
            )

            conn.commit()
            return True, submission, "Submission committed successfully"

    def cancel_assignment(
        self,
        task_id: str,
        assignment_id: str,
        settled_cost: float = 0.0,
        now: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Cancels an assignment and settles its budget without silent budget reset."""
        if reserved_task_id(task_id):
            return False, MANAGED_TASK_REFUSAL
        current_time = now if now is not None else time.time()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            asgn_cursor = conn.execute(
                "SELECT * FROM assignments WHERE assignment_id = ? AND task_id = ?",
                (assignment_id, task_id),
            )
            asgn_row = asgn_cursor.fetchone()
            if not asgn_row:
                conn.rollback()
                return False, "Assignment not found"

            asgn = AssignmentClaim.from_dict(dict(asgn_row))
            if asgn.status in (AttemptStatus.SUBMITTED, AttemptStatus.CANCELLED):
                conn.rollback()
                return (
                    False,
                    f"Assignment already in terminal state: {asgn.status.value}",
                )

            task_cursor = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            )
            task = self._row_to_task(task_cursor.fetchone())

            # Reconcile budget
            task.envelope.settle(asgn.reserved_cost, settled_cost)

            conn.execute(
                "UPDATE assignments SET status = ? WHERE assignment_id = ?",
                (AttemptStatus.CANCELLED.value, assignment_id),
            )

            conn.execute(
                """
                UPDATE tasks SET
                    current_assignment_id = NULL,
                    active_attempt_id = NULL,
                    status = ?,
                    envelope = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    TaskStatus.OPEN.value,
                    json.dumps(task.envelope.to_dict()),
                    current_time,
                    task_id,
                ),
            )

            conn.commit()
            return True, "Assignment cancelled"

    def bump_spec_revision(
        self,
        task_id: str,
        new_acceptance_criteria: Optional[List[str]] = None,
        new_baseline_sha: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Tuple[bool, Optional[TaskObjective], str]:
        """Bumps spec revision or baseline, invalidating all prior review receipts."""
        if reserved_task_id(task_id):
            return False, None, MANAGED_TASK_REFUSAL
        current_time = now if now is not None else time.time()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task_cursor = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            )
            row = task_cursor.fetchone()
            if not row:
                conn.rollback()
                return False, None, "Task not found"

            task = self._row_to_task(row)
            task.spec_revision += 1
            if new_acceptance_criteria is not None:
                task.acceptance_criteria = new_acceptance_criteria
            if new_baseline_sha is not None:
                task.baseline_sha = new_baseline_sha

            # Invalidate all prior reviews for this task
            conn.execute(
                "UPDATE reviews SET is_valid = 0 WHERE task_id = ?", (task_id,)
            )

            # If task was accepted or in review, move to HELD
            task.status = TaskStatus.HELD
            task.accepted_candidate_id = None
            task.updated_at = current_time

            conn.execute(
                """
                UPDATE tasks SET
                    spec_revision = ?,
                    baseline_sha = ?,
                    acceptance_criteria = ?,
                    status = ?,
                    accepted_candidate_id = NULL,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    task.spec_revision,
                    task.baseline_sha,
                    json.dumps(task.acceptance_criteria),
                    task.status.value,
                    task.updated_at,
                    task_id,
                ),
            )
            conn.commit()
            return (
                True,
                task,
                f"Spec revision bumped to {task.spec_revision}; prior reviews invalidated",
            )

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
        """Enforces author != reviewer and verifies spec_revision / baseline_sha matching."""
        if reserved_task_id(task_id):
            return False, None, MANAGED_TASK_REFUSAL
        current_time = now if now is not None else time.time()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")

            # Check candidate
            cand_cursor = conn.execute(
                "SELECT * FROM candidates WHERE candidate_id = ? AND task_id = ?",
                (candidate_id, task_id),
            )
            cand_row = cand_cursor.fetchone()
            if not cand_row:
                conn.rollback()
                return False, None, "Candidate not found for task"

            # Check author != reviewer invariant
            if cand_row["author_actor_id"] == reviewer_actor_id:
                conn.rollback()
                return (
                    False,
                    None,
                    "Reviewer independence violation: author cannot review their own candidate",
                )

            # Check task current revision
            task_cursor = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            )
            task = self._row_to_task(task_cursor.fetchone())

            # Candidate must match current spec revision and baseline
            if (
                cand_row["spec_revision"] != task.spec_revision
                or cand_row["baseline_sha"] != task.baseline_sha
            ):
                conn.rollback()
                return (
                    False,
                    None,
                    "Candidate is stale (spec revision or baseline SHA changed)",
                )

            review_id = f"rev_{hashlib.sha256(f'{candidate_id}:{reviewer_actor_id}:{current_time}'.encode()).hexdigest()[:16]}"
            receipt = ReviewReceipt(
                review_id=review_id,
                candidate_id=candidate_id,
                task_id=task_id,
                reviewer_actor_id=reviewer_actor_id,
                verdict=verdict,
                blocking_findings=blocking_findings,
                spec_revision=task.spec_revision,
                baseline_sha=task.baseline_sha,
                notes=notes,
                is_valid=True,
                created_at=current_time,
            )

            conn.execute(
                """
                INSERT INTO reviews (
                    review_id, candidate_id, task_id, reviewer_actor_id,
                    verdict, blocking_findings, spec_revision, baseline_sha,
                    notes, is_valid, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.review_id,
                    receipt.candidate_id,
                    receipt.task_id,
                    receipt.reviewer_actor_id,
                    receipt.verdict.value,
                    receipt.blocking_findings,
                    receipt.spec_revision,
                    receipt.baseline_sha,
                    receipt.notes,
                    1,
                    receipt.created_at,
                ),
            )

            # If verdict is PASS with 0 blocking findings, mark candidate accepted
            if verdict == ReviewVerdict.PASS and blocking_findings == 0:
                conn.execute(
                    """
                    UPDATE tasks SET
                        status = ?,
                        accepted_candidate_id = ?,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (TaskStatus.ACCEPTED.value, candidate_id, current_time, task_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE tasks SET
                        status = ?,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (TaskStatus.HELD.value, current_time, task_id),
                )

            conn.commit()
            return True, receipt, "Review recorded"

    def get_candidate(self, candidate_id: str) -> Optional[CandidateReceipt]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return CandidateReceipt(
                candidate_id=row["candidate_id"],
                task_id=row["task_id"],
                baseline_sha=row["baseline_sha"],
                patch_sha=row["patch_sha"],
                spec_revision=row["spec_revision"],
                author_actor_id=row["author_actor_id"],
                created_at=row["created_at"],
            )

    def get_valid_reviews_for_candidate(self, candidate_id: str) -> List[ReviewReceipt]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM reviews WHERE candidate_id = ? AND is_valid = 1",
                (candidate_id,),
            )
            return [
                ReviewReceipt(
                    review_id=r["review_id"],
                    candidate_id=r["candidate_id"],
                    task_id=r["task_id"],
                    reviewer_actor_id=r["reviewer_actor_id"],
                    verdict=ReviewVerdict(r["verdict"]),
                    blocking_findings=r["blocking_findings"],
                    spec_revision=r["spec_revision"],
                    baseline_sha=r["baseline_sha"],
                    notes=r["notes"],
                    is_valid=bool(r["is_valid"]),
                    created_at=r["created_at"],
                )
                for r in cursor.fetchall()
            ]

    # --- Emergent Conductor Leases & Host Signal Outbox ---

    def acquire_conductor_lease(
        self,
        actor_id: str,
        min_active_workers_for_conductor: int = 3,
        lease_ttl: float = 90.0,
        force_for_solo_deadlock: bool = False,
        now: Optional[float] = None,
    ) -> Tuple[bool, Optional[ConductorLease], str]:
        """Atomically acquires the singleton Conductor lease."""
        current_time = now if now is not None else time.time()

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")

            # 1. Bar claimant if actively holding a coding assignment
            asgn_cursor = conn.execute(
                """
                SELECT 1 FROM assignments
                WHERE actor_id = ?
                  AND status IN (?, ?)
                  AND lease_expires_at > ?
                LIMIT 1
                """,
                (
                    actor_id,
                    AttemptStatus.ASSIGNED.value,
                    AttemptStatus.RUNNING.value,
                    current_time,
                ),
            )
            if asgn_cursor.fetchone():
                conn.rollback()
                return (
                    False,
                    None,
                    f"Agent {actor_id} holds an active coding assignment and cannot claim Conductor lease",
                )

            # 2. Check current conductor state
            cond_cursor = conn.execute(
                "SELECT * FROM conductor_state WHERE singleton_id = 1"
            )
            cond_row = cond_cursor.fetchone()

            current_fence = 0
            if cond_row:
                current_fence = cond_row["fence_token"]
                lease_status = cond_row["status"]
                lease_expires = cond_row["expires_at"]

                # If lease is active and not expired
                if (
                    lease_status == ConductorLeaseStatus.ACTIVE.value
                    and lease_expires > current_time
                ):
                    if cond_row["actor_id"] == actor_id:
                        # Re-entrant renewal by active conductor
                        new_expires = current_time + lease_ttl
                        conn.execute(
                            """
                            UPDATE conductor_state
                            SET expires_at = ?
                            WHERE singleton_id = 1
                            """,
                            (new_expires,),
                        )
                        conn.commit()
                        lease = ConductorLease(
                            lease_id=cond_row["lease_id"],
                            actor_id=cond_row["actor_id"],
                            fence_token=cond_row["fence_token"],
                            active_worker_count=cond_row["active_worker_count"],
                            expires_at=new_expires,
                            status=ConductorLeaseStatus.ACTIVE,
                            created_at=cond_row["created_at"],
                        )
                        return True, lease, "Conductor lease renewed"
                    else:
                        conn.rollback()
                        return (
                            False,
                            None,
                            f"Conductor lease currently held by {cond_row['actor_id']}",
                        )
                elif (
                    lease_status == ConductorLeaseStatus.ACTIVE.value
                    and lease_expires <= current_time
                ):
                    # Mark expired
                    conn.execute(
                        "UPDATE conductor_state SET status = ? WHERE singleton_id = 1",
                        (ConductorLeaseStatus.EXPIRED.value,),
                    )

            # 3. Calculate active workers count directly from assignments table
            workers_cursor = conn.execute(
                """
                SELECT COUNT(DISTINCT actor_id) AS cnt
                FROM assignments
                WHERE status IN (?, ?) AND lease_expires_at > ?
                """,
                (
                    AttemptStatus.ASSIGNED.value,
                    AttemptStatus.RUNNING.value,
                    current_time,
                ),
            )
            active_worker_count = workers_cursor.fetchone()["cnt"]

            # 4. Check tasks in INTEGRATING or HELD
            stuck_cursor = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM tasks
                WHERE status IN (?, ?)
                """,
                (TaskStatus.INTEGRATING.value, TaskStatus.HELD.value),
            )
            stuck_task_count = stuck_cursor.fetchone()["cnt"]

            # Eligibility condition: active_workers >= min_active_workers_for_conductor OR stuck_tasks > 0 OR force_for_solo_deadlock
            eligible = (
                (active_worker_count >= min_active_workers_for_conductor)
                or (stuck_task_count > 0)
                or force_for_solo_deadlock
            )

            if not eligible:
                conn.rollback()
                return (
                    False,
                    None,
                    f"Chamber mode: insufficient active workers ({active_worker_count} < {min_active_workers_for_conductor}) and no integrating/held tasks",
                )

            # 5. Acquire lease with incremented fence_token
            new_fence = current_fence + 1
            lease_id = f"cond_lease_{new_fence}"
            expires_at = current_time + lease_ttl

            conn.execute(
                """
                INSERT INTO conductor_state (
                    singleton_id, lease_id, actor_id, fence_token,
                    active_worker_count, expires_at, status, created_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    lease_id = excluded.lease_id,
                    actor_id = excluded.actor_id,
                    fence_token = excluded.fence_token,
                    active_worker_count = excluded.active_worker_count,
                    expires_at = excluded.expires_at,
                    status = excluded.status,
                    created_at = excluded.created_at
                """,
                (
                    lease_id,
                    actor_id,
                    new_fence,
                    active_worker_count,
                    expires_at,
                    ConductorLeaseStatus.ACTIVE.value,
                    current_time,
                ),
            )
            conn.commit()

            lease = ConductorLease(
                lease_id=lease_id,
                actor_id=actor_id,
                fence_token=new_fence,
                active_worker_count=active_worker_count,
                expires_at=expires_at,
                status=ConductorLeaseStatus.ACTIVE,
                created_at=current_time,
            )
            return True, lease, "Conductor lease acquired"

    def release_conductor_lease(
        self,
        actor_id: str,
        fence_token: int,
    ) -> Tuple[bool, str]:
        """Releases the Conductor lease so the actor can step down to coding/reviewing."""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cond_cursor = conn.execute(
                "SELECT * FROM conductor_state WHERE singleton_id = 1"
            )
            cond_row = cond_cursor.fetchone()
            if not cond_row:
                conn.rollback()
                return False, "No conductor lease exists"

            if cond_row["actor_id"] != actor_id:
                conn.rollback()
                return False, f"Actor {actor_id} is not the current conductor"

            if cond_row["fence_token"] != fence_token:
                conn.rollback()
                return (
                    False,
                    f"Fence token mismatch: provided {fence_token} vs current {cond_row['fence_token']}",
                )

            if cond_row["status"] != ConductorLeaseStatus.ACTIVE.value:
                conn.rollback()
                return False, f"Conductor lease is already {cond_row['status']}"

            conn.execute(
                "UPDATE conductor_state SET status = ? WHERE singleton_id = 1",
                (ConductorLeaseStatus.RELEASED.value,),
            )
            conn.commit()
            return True, "Conductor lease stepped down"

    def get_conductor_lease(
        self, now: Optional[float] = None
    ) -> Optional[ConductorLease]:
        """Gets current conductor lease, lazily marking expired leases."""
        current_time = now if now is not None else time.time()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "SELECT * FROM conductor_state WHERE singleton_id = 1"
            )
            row = cursor.fetchone()
            if not row:
                conn.commit()
                return None

            status = row["status"]
            if (
                status == ConductorLeaseStatus.ACTIVE.value
                and row["expires_at"] <= current_time
            ):
                conn.execute(
                    "UPDATE conductor_state SET status = ? WHERE singleton_id = 1",
                    (ConductorLeaseStatus.EXPIRED.value,),
                )
                conn.commit()
                return None
            conn.commit()

            if status != ConductorLeaseStatus.ACTIVE.value:
                return None

            return ConductorLease(
                lease_id=row["lease_id"],
                actor_id=row["actor_id"],
                fence_token=row["fence_token"],
                active_worker_count=row["active_worker_count"],
                expires_at=row["expires_at"],
                status=ConductorLeaseStatus(row["status"]),
                created_at=row["created_at"],
            )

    def emit_host_signal(
        self,
        actor_id: str,
        signal_type: HostSignalType,
        requested_worker_delta: int,
        target_task_ids: List[str],
        rationale: str,
        now: Optional[float] = None,
    ) -> Tuple[bool, Optional[HostTelemetrySignal], str]:
        """Atomically emits a host telemetry scaling signal with deduplication and cost snapshotting."""
        if not experimental_host_targets(target_task_ids):
            return False, None, "Host signals require nonreserved experimental task IDs"
        current_time = now if now is not None else time.time()
        sorted_targets = sorted(list(target_task_ids))

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")

            # 1. Authorize emission
            # Active Conductor can emit SCALE_UP, IDLE_DRAIN, ESCALATE_TRIAGE
            # Any worker can emit SPAWN_REVIEWER if they authored a candidate stuck in 'integrating' for at least one target_task_id
            cond_cursor = conn.execute(
                "SELECT * FROM conductor_state WHERE singleton_id = 1"
            )
            cond_row = cond_cursor.fetchone()
            is_active_conductor = (
                cond_row is not None
                and cond_row["actor_id"] == actor_id
                and cond_row["status"] == ConductorLeaseStatus.ACTIVE.value
                and cond_row["expires_at"] > current_time
            )

            if signal_type in (
                HostSignalType.SCALE_UP,
                HostSignalType.IDLE_DRAIN,
                HostSignalType.ESCALATE_TRIAGE,
            ):
                if not is_active_conductor:
                    conn.rollback()
                    return (
                        False,
                        None,
                        f"Only active Conductor can emit {signal_type.value}",
                    )
            elif signal_type == HostSignalType.SPAWN_REVIEWER:
                if not is_active_conductor:
                    # Verify actor authored a candidate in integrating state for at least one target_task_id
                    auth_cand = False
                    for tid in sorted_targets:
                        c_cursor = conn.execute(
                            """
                            SELECT 1 FROM tasks t
                            JOIN candidates c ON t.task_id = c.task_id
                            WHERE t.task_id = ?
                              AND t.status = ?
                              AND c.author_actor_id = ?
                            LIMIT 1
                            """,
                            (tid, TaskStatus.INTEGRATING.value, actor_id),
                        )
                        if c_cursor.fetchone():
                            auth_cand = True
                            break

                    if not auth_cand:
                        conn.rollback()
                        return (
                            False,
                            None,
                            f"Actor {actor_id} is not active Conductor and has no candidate awaiting review in target tasks",
                        )
            else:
                conn.rollback()
                return False, None, f"Unknown signal type {signal_type}"

            # 2. Check deduplication for identical PENDING signals with same signal_type and sorted target_task_ids
            existing_cursor = conn.execute(
                """
                SELECT * FROM host_signal_outbox
                WHERE host_status = ? AND signal_type = ?
                """,
                (HostSignalStatus.PENDING.value, signal_type.value),
            )
            for row in existing_cursor.fetchall():
                if not stored_experimental_host_targets(row["target_task_ids"]):
                    continue
                row_targets = json.loads(row["target_task_ids"])
                if sorted(row_targets) == sorted_targets:
                    conn.rollback()
                    existing_signal = HostTelemetrySignal.from_dict(dict(row))
                    return (
                        True,
                        existing_signal,
                        "Deduplicated: identical pending host signal returned",
                    )

            # 3. Snapshot total unsettled cost globally across all tasks
            cost_cursor = conn.execute("SELECT envelope FROM tasks")
            total_unsettled_cost = 0.0
            for r in cost_cursor.fetchall():
                try:
                    env_data = json.loads(r["envelope"])
                    env = ResourceEnvelope.from_dict(env_data)
                    total_unsettled_cost += (
                        env.settled_budget + env.in_flight_reserved_budget
                    )
                except Exception:
                    continue

            signal_id = f"sig_{hashlib.sha256(f'{signal_type.value}:{sorted_targets}:{current_time}'.encode()).hexdigest()[:16]}"
            new_signal = HostTelemetrySignal(
                signal_id=signal_id,
                emitted_by_actor=actor_id,
                signal_type=signal_type,
                requested_worker_delta=requested_worker_delta,
                target_task_ids=sorted_targets,
                unsettled_cost_snapshot=total_unsettled_cost,
                rationale=rationale,
                host_status=HostSignalStatus.PENDING,
                host_response_note="",
                created_at=current_time,
                updated_at=current_time,
            )

            conn.execute(
                """
                INSERT INTO host_signal_outbox (
                    signal_id, emitted_by_actor, signal_type, requested_worker_delta,
                    target_task_ids, unsettled_cost_snapshot, rationale,
                    host_status, host_response_note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_signal.signal_id,
                    new_signal.emitted_by_actor,
                    new_signal.signal_type.value,
                    new_signal.requested_worker_delta,
                    json.dumps(new_signal.target_task_ids),
                    new_signal.unsettled_cost_snapshot,
                    new_signal.rationale,
                    new_signal.host_status.value,
                    new_signal.host_response_note,
                    new_signal.created_at,
                    new_signal.updated_at,
                ),
            )
            conn.commit()
            return True, new_signal, "Host signal emitted"

    def poll_host_signals(
        self,
        status: HostSignalStatus = HostSignalStatus.PENDING,
        *,
        executable_only: bool = False,
    ) -> List[HostTelemetrySignal]:
        """Inspect history by default; execution hosts must request the filtered view."""
        status_val = (
            status.value if isinstance(status, HostSignalStatus) else str(status)
        )
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM host_signal_outbox WHERE host_status = ? ORDER BY created_at ASC",
                (status_val,),
            )
            return [
                HostTelemetrySignal.from_dict(dict(row))
                for row in cursor.fetchall()
                if not executable_only
                or stored_experimental_host_targets(row["target_task_ids"])
            ]

    def acknowledge_host_signal(
        self,
        signal_id: str,
        new_status: HostSignalStatus,
        response_note: str = "",
        now: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Updates host signal status and response note."""
        current_time = now if now is not None else time.time()
        new_status_val = (
            new_status.value
            if isinstance(new_status, HostSignalStatus)
            else str(new_status)
        )

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "SELECT * FROM host_signal_outbox WHERE signal_id = ?",
                (signal_id,),
            )
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return False, f"Signal {signal_id} not found"

            if not stored_experimental_host_targets(row["target_task_ids"]):
                conn.rollback()
                return (
                    False,
                    "Historical managed or malformed host signals are inspection-only",
                )

            conn.execute(
                """
                UPDATE host_signal_outbox
                SET host_status = ?, host_response_note = ?, updated_at = ?
                WHERE signal_id = ?
                """,
                (new_status_val, response_note, current_time, signal_id),
            )
            conn.commit()
            return True, f"Signal {signal_id} updated to {new_status_val}"

    def prune_host_signals(
        self,
        older_than_seconds: float = 86400.0,
        statuses: Optional[List[HostSignalStatus]] = None,
        now: Optional[float] = None,
    ) -> int:
        """Prunes terminal host signals older than the retention threshold to compact the ledger."""
        current_time = now if now is not None else time.time()
        cutoff_time = current_time - older_than_seconds
        target_statuses = statuses or [
            HostSignalStatus.FULFILLED,
            HostSignalStatus.REJECTED,
        ]
        status_vals = [
            s.value if isinstance(s, HostSignalStatus) else str(s)
            for s in target_statuses
        ]

        if not status_vals:
            return 0

        placeholders = ",".join("?" for _ in status_vals)
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                f"""
                DELETE FROM host_signal_outbox
                WHERE host_status IN ({placeholders})
                  AND updated_at < ?
                """,
                (*status_vals, cutoff_time),
            )
            deleted_count = cursor.rowcount
            conn.commit()
            return deleted_count
