#!/usr/bin/env python3
"""Experimental FencedLedger tasks and inspection receipts, not managed authority."""

import sys
import os
import argparse
import json
import uuid

# Setup python path so we can import liminal_bridge and core
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo_root)
sys.path.insert(0, os.path.join(repo_root, "src"))

from src.core.collaboration.ledger import FencedLedger
from src.core.collaboration.adapter import DeterministicExecutionAdapter
from src.core.collaboration.coordinator import CollaborationCoordinator
from src.core.collaboration.conductor import EnsembleConductor
from src.core.collaboration.admission import ExperimentalReceiptExporter
from src.core.collaboration.models import ReviewVerdict, TaskStatus
from src.core.collaboration.experimental_boundary import (
    ManagedTaskRefusal,
    require_experimental_task,
)


def get_coordinator(db_path: str = None) -> CollaborationCoordinator:
    db = db_path or os.getenv("LIMINAL_COLLAB_DB", "collab_ledger.db")
    ledger = FencedLedger(db)
    adapter = DeterministicExecutionAdapter()
    return CollaborationCoordinator(ledger, adapter)


def get_conductor(db_path: str = None) -> EnsembleConductor:
    db = db_path or os.getenv("LIMINAL_COLLAB_DB", "collab_ledger.db")
    ledger = FencedLedger(db)
    return EnsembleConductor(ledger)


def get_receipts(db_path: str = None) -> ExperimentalReceiptExporter:
    db = db_path or os.getenv("LIMINAL_COLLAB_DB", "collab_ledger.db")
    ledger = FencedLedger(db)
    return ExperimentalReceiptExporter(ledger)


def print_experimental(result):
    print(
        json.dumps(
            {
                **result,
                "managed_authority": False,
                "release_authorized": False,
                "identity_source": "caller-supplied",
            },
            indent=2,
        )
    )


def handle_enroll(args):
    require_experimental_task(args.task_id)
    coord = get_coordinator(args.db)
    task_id = args.task_id or f"experimental_{uuid.uuid4().hex[:8]}"
    criteria = [c.strip() for c in args.criteria.split(",") if c.strip()]
    task = coord.register_task(
        task_id=task_id,
        title=args.title,
        description=args.description or "",
        baseline_sha=args.baseline_sha,
        acceptance_criteria=criteria,
        max_budget=args.budget,
        max_attempts=args.max_attempts,
    )
    print_experimental(task.to_dict())


def handle_claim(args):
    require_experimental_task(args.task_id)
    coord = get_coordinator(args.db)
    conductor = get_conductor(args.db)
    ok, handle, reason = coord.dispatch_attempt(
        task_id=args.task_id,
        actor_id=args.actor_id,
        reserve_cost=args.reserve,
        lease_duration=args.lease,
    )
    if not ok or not handle:
        nudge = conductor.observe_contention_and_nudge(args.task_id, args.actor_id)
        out = {
            "success": False,
            "reason": reason,
            "nudge": nudge.to_dict() if nudge else None,
        }
        print_experimental(out)
        sys.exit(1)

    print_experimental(
        {
            "success": True,
            "assignment_id": handle.attempt_id,
            "fence_token": handle.fence_token,
            "actor_id": handle.actor_id,
            "baseline_ref": handle.baseline_ref,
        }
    )


def handle_submit(args):
    require_experimental_task(args.task_id)
    coord = get_coordinator(args.db)
    diff = sys.stdin.read() if args.diff_file == "-" else open(args.diff_file).read()
    request_id = args.request_id or f"req_{uuid.uuid4().hex[:8]}"
    ok, sub, reason = coord.submit_attempt(
        task_id=args.task_id,
        assignment_id=args.assignment_id,
        actor_id=args.actor_id,
        fence_token=args.fence,
        request_id=request_id,
        diff_text=diff,
        actual_cost=args.cost,
    )
    if not ok or not sub:
        print_experimental({"success": False, "reason": reason})
        sys.exit(1)

    print_experimental({"success": True, "submission": sub.to_dict()})


def handle_review(args):
    require_experimental_task(args.task_id)
    coord = get_coordinator(args.db)
    v = ReviewVerdict(args.verdict.upper())
    ok, rec, reason = coord.record_review(
        candidate_id=args.candidate_id,
        task_id=args.task_id,
        reviewer_actor_id=args.reviewer_id,
        verdict=v,
        blocking_findings=args.blocking_findings,
        notes=args.notes or "",
    )
    if not ok or not rec:
        print_experimental({"success": False, "reason": reason})
        sys.exit(1)

    print_experimental(
        {
            "success": True,
            "review_id": rec.review_id,
            "verdict": rec.verdict.value,
            "blocking_findings": rec.blocking_findings,
        }
    )


def handle_local_receipt(args):
    try:
        if args.pr is not None:
            raise ValueError(
                "Experimental records cannot certify GitHub PR or workflow provenance; "
                "omit --pr to inspect a local receipt"
            )
        if args.action == "evidence":
            print(
                "Deprecated: evidence exports an experimental local receipt only; "
                "use local-receipt. It cannot authorize managed work or a release.",
                file=sys.stderr,
            )
        receipt = get_receipts(args.db).emit_local_receipt(args.task_id)
        print(json.dumps(receipt, indent=2))
    except ValueError as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


def handle_list(args):
    db = args.db or os.getenv("LIMINAL_COLLAB_DB", "collab_ledger.db")
    ledger = FencedLedger(db)
    tasks = ledger.list_tasks()
    print(json.dumps([t.to_dict() for t in tasks], indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Inspect experimental FencedLedger tasks. Managed tasks use the authenticated coordinator service."
    )
    parser.add_argument(
        "--db", type=str, default=None, help="Path to collaboration SQLite ledger"
    )
    subparsers = parser.add_subparsers(dest="action")

    # list
    p_list = subparsers.add_parser("list", help="Inspect experimental task history")
    p_list.set_defaults(func=handle_list)

    # enroll
    p_enroll = subparsers.add_parser(
        "enroll", help="Create an experimental local task, never a managed task"
    )
    p_enroll.add_argument("--task-id", type=str, default=None)
    p_enroll.add_argument("--title", required=True)
    p_enroll.add_argument("--description", default="")
    p_enroll.add_argument("--baseline-sha", required=True)
    p_enroll.add_argument("--criteria", required=True, help="Comma-separated criteria")
    p_enroll.add_argument("--budget", type=float, default=100.0)
    p_enroll.add_argument("--max-attempts", type=int, default=5)
    p_enroll.set_defaults(func=handle_enroll)

    # claim
    p_claim = subparsers.add_parser(
        "claim", help="Claim an experimental assignment using a local actor label"
    )
    p_claim.add_argument("--task-id", required=True)
    p_claim.add_argument("--actor-id", required=True)
    p_claim.add_argument("--reserve", type=float, default=10.0)
    p_claim.add_argument("--lease", type=float, default=300.0)
    p_claim.set_defaults(func=handle_claim)

    # submit
    p_submit = subparsers.add_parser(
        "submit", help="Submit an experimental local contribution"
    )
    p_submit.add_argument("--task-id", required=True)
    p_submit.add_argument("--assignment-id", required=True)
    p_submit.add_argument("--actor-id", required=True)
    p_submit.add_argument("--fence", type=int, required=True)
    p_submit.add_argument(
        "--diff-file", default="-", help="Path to diff or '-' for stdin"
    )
    p_submit.add_argument("--cost", type=float, default=2.0)
    p_submit.add_argument("--request-id", default=None)
    p_submit.set_defaults(func=handle_submit)

    # review
    p_review = subparsers.add_parser(
        "review", help="Record an experimental review using a local reviewer label"
    )
    p_review.add_argument("--task-id", required=True)
    p_review.add_argument("--candidate-id", required=True)
    p_review.add_argument("--reviewer-id", required=True)
    p_review.add_argument("--verdict", choices=["PASS", "FAIL", "HOLD"], default="PASS")
    p_review.add_argument("--blocking-findings", type=int, default=0)
    p_review.add_argument("--notes", default="")
    p_review.set_defaults(func=handle_review)

    # Local inspection only; the deprecated name cannot emit forge evidence.
    p_ev = subparsers.add_parser(
        "local-receipt",
        aliases=["evidence"],
        help="Inspect experimental local acceptance; never forge review evidence",
    )
    p_ev.add_argument("--task-id", required=True)
    p_ev.add_argument("--pr", type=int, default=None, help=argparse.SUPPRESS)
    p_ev.set_defaults(func=handle_local_receipt)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    try:
        args.func(args)
    except ManagedTaskRefusal as error:
        print_experimental({"success": False, "reason": str(error)})
        sys.exit(1)


if __name__ == "__main__":
    main()
