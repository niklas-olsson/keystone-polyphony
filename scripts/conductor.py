#!/usr/bin/env python3
"""Experimental conductor utilities and historical host-signal inspection."""

import sys
import os
import argparse
import json

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo_root)
sys.path.insert(0, os.path.join(repo_root, "src"))

from src.core.collaboration.ledger import FencedLedger
from src.core.collaboration.models import (
    HostSignalType,
    HostSignalStatus,
    ConductorLeaseStatus,
)


def get_ledger(db_path: str = None) -> FencedLedger:
    db = db_path or os.getenv("LIMINAL_COLLAB_DB", "collab_ledger.db")
    return FencedLedger(db)


def handle_claim(args):
    ledger = get_ledger(args.db)
    ok, lease, reason = ledger.acquire_conductor_lease(
        actor_id=args.actor_id,
        min_active_workers_for_conductor=args.min_workers,
        lease_ttl=args.ttl,
        force_for_solo_deadlock=args.force,
    )
    if not ok or not lease:
        print(json.dumps({"success": False, "reason": reason}, indent=2))
        sys.exit(1)

    print(
        json.dumps(
            {
                "success": True,
                "message": reason,
                "data": lease.to_dict(),
            },
            indent=2,
        )
    )


def handle_step_down(args):
    ledger = get_ledger(args.db)
    ok, reason = ledger.release_conductor_lease(
        actor_id=args.actor_id,
        fence_token=args.fence,
    )
    if not ok:
        print(json.dumps({"success": False, "reason": reason}, indent=2))
        sys.exit(1)

    print(
        json.dumps(
            {
                "success": True,
                "message": reason,
            },
            indent=2,
        )
    )


def handle_status(args):
    ledger = get_ledger(args.db)
    lease = ledger.get_conductor_lease()
    if not lease:
        print(json.dumps({"status": "NO_ACTIVE_CONDUCTOR"}, indent=2))
    else:
        print(json.dumps(lease.to_dict(), indent=2))


def handle_emit(args):
    ledger = get_ledger(args.db)
    sig_type = HostSignalType(args.signal_type.upper())
    target_tasks = [t.strip() for t in args.targets.split(",") if t.strip()]
    ok, sig, reason = ledger.emit_host_signal(
        actor_id=args.actor_id,
        signal_type=sig_type,
        requested_worker_delta=args.delta,
        target_task_ids=target_tasks,
        rationale=args.rationale or "",
    )
    if not ok or not sig:
        print(json.dumps({"success": False, "reason": reason}, indent=2))
        sys.exit(1)

    print(
        json.dumps(
            {
                "success": True,
                "message": reason,
                "data": sig.to_dict(),
            },
            indent=2,
        )
    )


def handle_poll(args):
    ledger = get_ledger(args.db)
    status_filter = HostSignalStatus(args.status.upper())
    signals = ledger.poll_host_signals(status=status_filter)
    print(json.dumps([s.to_dict() for s in signals], indent=2))


def handle_ack(args):
    ledger = get_ledger(args.db)
    new_status = HostSignalStatus(args.new_status.upper())
    ok, reason = ledger.acknowledge_host_signal(
        signal_id=args.signal_id,
        new_status=new_status,
        response_note=args.note or "",
    )
    if not ok:
        print(json.dumps({"success": False, "reason": reason}, indent=2))
        sys.exit(1)

    print(json.dumps({"success": True, "message": reason}, indent=2))


def handle_prune(args):
    ledger = get_ledger(args.db)
    deleted = ledger.prune_host_signals(older_than_seconds=args.older_than)
    print(json.dumps({"success": True, "pruned_count": deleted}, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Experimental local conductor and host-signal history; never managed authority."
    )
    parser.add_argument(
        "--db", type=str, default=None, help="Path to collaboration SQLite ledger"
    )
    subparsers = parser.add_subparsers(dest="subcommand")

    # Conductor commands
    p_claim = subparsers.add_parser("claim", help="Claim singleton conductor lease")
    p_claim.add_argument("--actor-id", required=True)
    p_claim.add_argument("--ttl", type=float, default=90.0)
    p_claim.add_argument("--min-workers", type=int, default=3)
    p_claim.add_argument(
        "--force", action="store_true", help="Force claim for solo deadlock"
    )
    p_claim.set_defaults(func=handle_claim)

    p_step = subparsers.add_parser("step-down", help="Step down from conductor lease")
    p_step.add_argument("--actor-id", required=True)
    p_step.add_argument("--fence", type=int, required=True)
    p_step.set_defaults(func=handle_step_down)

    p_status = subparsers.add_parser(
        "status", help="Show current conductor lease status"
    )
    p_status.set_defaults(func=handle_status)

    # Signal outbox commands
    p_emit = subparsers.add_parser("emit-signal", help="Emit host scaling signal")
    p_emit.add_argument("--actor-id", required=True)
    p_emit.add_argument(
        "--signal-type",
        choices=["SCALE_UP", "SPAWN_REVIEWER", "IDLE_DRAIN", "ESCALATE_TRIAGE"],
        required=True,
    )
    p_emit.add_argument("--delta", type=int, default=1)
    p_emit.add_argument(
        "--targets", required=True, help="Comma-separated target task IDs"
    )
    p_emit.add_argument("--rationale", default="")
    p_emit.set_defaults(func=handle_emit)

    p_poll = subparsers.add_parser(
        "poll-signals",
        help="Inspect historical experimental signals, not executable authority",
    )
    p_poll.add_argument(
        "--status",
        choices=["PENDING", "ACKNOWLEDGED", "FULFILLED", "REJECTED"],
        default="PENDING",
    )
    p_poll.set_defaults(func=handle_poll)

    p_ack = subparsers.add_parser(
        "ack-signal", help="Acknowledge or fulfill host signal"
    )
    p_ack.add_argument("--signal-id", required=True)
    p_ack.add_argument(
        "--new-status",
        choices=["ACKNOWLEDGED", "FULFILLED", "REJECTED"],
        required=True,
    )
    p_ack.add_argument("--note", default="")
    p_ack.set_defaults(func=handle_ack)

    p_prune = subparsers.add_parser("prune-signals", help="Prune terminal host signals")
    p_prune.add_argument(
        "--older-than",
        type=float,
        default=86400.0,
        help="Prune signals updated older than N seconds (default: 86400)",
    )
    p_prune.set_defaults(func=handle_prune)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
