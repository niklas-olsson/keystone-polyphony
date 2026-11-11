"""Inspection receipts for the experimental FencedLedger, without forge authority."""

from typing import Dict, Any, Optional
import json
from pathlib import Path
import warnings

from .models import ReviewVerdict, TaskStatus
from .ledger import FencedLedger


class ExperimentalReceiptExporter:
    """Export local ledger observations, never authenticated admission evidence.

    FencedLedger actor labels are caller supplied. Its candidate identifiers are
    local content identities, not Git commit SHAs. Neither establishes managed
    task authority, an independent PR review, or permission to release changes.
    """

    def __init__(
        self, ledger: FencedLedger, repository: str = "niklas-olsson/keystone-polyphony"
    ):
        self.ledger = ledger
        self.repository = repository

    def emit_local_receipt(self, task_id: str) -> Dict[str, Any]:
        """Inspect an accepted local candidate and preserve its review records."""
        task = self.ledger.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        if not task.accepted_candidate_id:
            raise ValueError(f"Task {task_id} has no accepted candidate")
        if task.status != TaskStatus.ACCEPTED:
            raise ValueError(f"Task {task_id} is not locally accepted")

        candidate = self.ledger.get_candidate(task.accepted_candidate_id)
        if not candidate:
            raise ValueError(f"Candidate {task.accepted_candidate_id} not found")
        if (
            candidate.task_id != task_id
            or candidate.spec_revision != task.spec_revision
            or candidate.baseline_sha != task.baseline_sha
        ):
            raise ValueError("Local candidate does not match the task revision")

        reviews = self.ledger.get_valid_reviews_for_candidate(candidate.candidate_id)
        if not any(
            r.verdict == ReviewVerdict.PASS and r.blocking_findings == 0
            for r in reviews
        ):
            raise ValueError(
                f"No passing review with 0 blocking findings for candidate {candidate.candidate_id}"
            )

        return {
            "schema_version": 1,
            "receipt_type": "polyphony-experimental-local",
            "ledger": "FencedLedger",
            "managed_authority": False,
            "release_authorized": False,
            "identity_source": "caller-supplied",
            "repository_label": self.repository,
            "task_id": task_id,
            "local_task_status": task.status.value,
            "candidate_id": candidate.candidate_id,
            "baseline_reference": candidate.baseline_sha,
            "patch_digest": candidate.patch_sha,
            "author_actor_id": candidate.author_actor_id,
            "spec_revision": candidate.spec_revision,
            "local_reviews": [
                {
                    "review_id": review.review_id,
                    "reviewer_actor_id": review.reviewer_actor_id,
                    "verdict": review.verdict.value,
                    "blocking_findings": review.blocking_findings,
                    "notes": review.notes,
                    "created_at": review.created_at,
                }
                for review in sorted(reviews, key=lambda r: (r.created_at, r.review_id))
            ],
        }

    @staticmethod
    def _write_receipt(receipt: Dict[str, Any], output_path: str) -> str:
        if Path(output_path).name == "review-evidence.json":
            raise ValueError(
                "review-evidence.json is reserved for authenticated forge evidence; "
                "use experimental-receipt.json"
            )
        with open(output_path, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2)
        return output_path

    def write_local_receipt(
        self, task_id: str, output_path: str = "experimental-receipt.json"
    ) -> str:
        """Write an inspection receipt without changing the historical ledger."""
        return self._write_receipt(self.emit_local_receipt(task_id), output_path)

    def emit_review_evidence(
        self,
        task_id: str,
        workflow_path: Optional[str] = None,
        run_id: Optional[int] = None,
        run_attempt: Optional[int] = None,
        pr_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Deprecated local-receipt alias. Forge metadata is explicitly rejected."""
        if any(
            value is not None
            for value in (workflow_path, run_id, run_attempt, pr_number)
        ):
            raise ValueError(
                "Experimental records cannot certify GitHub PR or workflow provenance"
            )
        warnings.warn(
            "emit_review_evidence now returns an experimental local receipt; "
            "use emit_local_receipt",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.emit_local_receipt(task_id)

    def write_evidence_file(
        self,
        task_id: str,
        output_path: str = "experimental-receipt.json",
        workflow_path: Optional[str] = None,
        run_id: Optional[int] = None,
        run_attempt: Optional[int] = None,
        pr_number: Optional[int] = None,
    ) -> str:
        """Deprecated alias, with safe local semantics and filename."""
        receipt = self.emit_review_evidence(
            task_id, workflow_path, run_id, run_attempt, pr_number
        )
        return self._write_receipt(receipt, output_path)


# Keep imports working for inspection clients without restoring forge semantics.
AdmissionHandoffEmitter = ExperimentalReceiptExporter
