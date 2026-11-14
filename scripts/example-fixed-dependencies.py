#!/usr/bin/env python3
"""One fixed authenticated dependency example, with synthetic saved work only.

Creates a new, explicitly selected directory and a loopback-only native service.
No execution adapter, provider, existing deployment or Git repository is used.
All principals, including the reviewer, are scripted fixtures, not independent
human/model review. The database and report remain available after the example.
"""

import argparse
import asyncio
import json
from pathlib import Path
import secrets
import sys
import time

from aiohttp import ClientSession, ClientTimeout, web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.liminal_bridge.coordinator import Conflict, Coordinator, digest
from src.liminal_bridge.coordinator_service import create_app


async def example(directory):
    directory = Path(directory)
    directory.mkdir(mode=0o700, exist_ok=False)
    store = Coordinator(directory / "authority.db")
    roles = {
        "owner": "owner",
        "interface-author": "worker",
        "implementation-a": "worker",
        "implementation-b": "worker",
        "integrator": "worker",
        "unrelated-author": "worker",
        "fixture-reviewer": "reviewer",
        "conductor": "conductor",
    }
    tokens = {identity: secrets.token_hex(32) for identity in roles}
    credentials = {
        tokens[identity]: {"identity": identity, "role": role}
        for identity, role in roles.items()
    }
    runner = web.AppRunner(create_app(store, credentials))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    tasks, attempts, candidates, artifacts, admissions = {}, {}, {}, {}, {}
    refusals = []
    began = time.monotonic()
    try:
        await site.start()
        port = runner.addresses[0][1]
        url = f"http://127.0.0.1:{port}"
        async with ClientSession(
            timeout=ClientTimeout(total=5), trust_env=False
        ) as http:

            async def call(identity, operation, *, expected=200, **payload):
                async with http.post(
                    f"{url}/call/{operation}",
                    json=payload,
                    headers={"Authorization": f"Bearer {tokens[identity]}"},
                ) as response:
                    assert response.status == expected, (
                        identity,
                        operation,
                        response.status,
                    )
                    if expected != 200:
                        refusals.append({"operation": operation, "status": expected})
                        return None
                    return (await response.json())["result"]

            async def enroll(name, dependencies=()):
                task = await call(
                    "owner",
                    "enroll",
                    title=name,
                    requirements=f"Approved synthetic output for {name}; revision one.",
                    checks=["fixture-content"],
                    reviewers=["fixture-reviewer"],
                    dependencies=[tasks[dep]["id"] for dep in dependencies],
                    max_attempts=2,
                    budget_seconds=60,
                )
                tasks[name] = task

            async def start(name, author, expected=200):
                result = await call(
                    author,
                    "start_attempt",
                    expected=expected,
                    task_id=tasks[name]["id"],
                    capabilities=[],
                    seconds=30,
                    request_key=f"{name}-first-attempt",
                )
                if result is not None:
                    attempts[name] = result
                return result

            async def save(name, author):
                patch = f"Synthetic retained work for {name}. No code was executed.\n"
                artifact = await call(
                    author,
                    "save_work",
                    attempt_id=attempts[name]["id"],
                    content=patch,
                )
                artifacts[name] = {"sha256": artifact, "text": patch}
                candidates[name] = await call(
                    author,
                    "candidate",
                    attempt_id=attempts[name]["id"],
                    base="1" * 40,  # Explicit synthetic immutable-ID fixture.
                    artifact_id=artifact,
                )
                # No adapter exists and /execute was never called. This scripted
                # owner observes no launched process; it is not worker evidence.
                await call(
                    "owner",
                    "observe",
                    attempt_id=attempts[name]["id"],
                    state="exited",
                    termination_confirmed=True,
                )

            async def review(name, passed, key):
                return await call(
                    "fixture-reviewer",
                    "evidence",
                    candidate_id=candidates[name]["id"],
                    check="fixture-content",
                    passed=passed,
                    input_revision=1,
                    request_key=key,
                )

            async def accept(name):
                packet = await call(
                    "owner", "accept", candidate_id=candidates[name]["id"]
                )
                assert packet["release_authorized"] is False
                admissions[name] = packet

            await enroll("interface")
            await enroll("implementation-a", ["interface"])
            await enroll("implementation-b", ["interface"])
            await enroll("integration", ["implementation-a", "implementation-b"])
            await enroll("unrelated")
            before_proposal = store.snapshot()
            await call(
                "conductor",
                "enroll",
                expected=403,
                title="unapproved split",
                requirements="advice cannot create authoritative tasks",
                checks=["fixture-content"],
            )
            assert store.snapshot() == before_proposal
            await start("implementation-a", "implementation-a", 409)
            await start("implementation-b", "implementation-b", 409)
            assert all(task["attempts"] == 0 for task in store.snapshot()["task"])

            await start("interface", "interface-author")
            await save("interface", "interface-author")
            # Recorded completion and saved bytes still supply no accepted check.
            await call(
                "owner",
                "accept",
                expected=409,
                candidate_id=candidates["interface"]["id"],
            )
            await start("implementation-a", "implementation-a", 409)
            await start("implementation-b", "implementation-b", 409)
            await review("interface", True, "interface-content-v1")
            await accept("interface")

            first = await start("implementation-a", "implementation-a")
            second = await start("implementation-b", "implementation-b")
            assert first["id"] != second["id"]
            snapshot = store.snapshot()
            reserved = [
                a["id"] for a in snapshot["attempt"] if a["state"] == "reserved"
            ]
            assert set(reserved) == {first["id"], second["id"]}
            assert first["dependency_admissions"] == second["dependency_admissions"]
            await start("integration", "integrator", 409)

            await save("implementation-a", "implementation-a")
            await review("implementation-a", True, "implementation-a-content-v1")
            await accept("implementation-a")
            await save("implementation-b", "implementation-b")
            await review("implementation-b", False, "implementation-b-failed-v1")
            await call(
                "owner",
                "accept",
                expected=409,
                candidate_id=candidates["implementation-b"]["id"],
            )
            await start("integration", "integrator", 409)
            await review(
                "implementation-b", True, "implementation-b-corrected-observation-v1"
            )
            await accept("implementation-b")
            await start("integration", "integrator")
            await save("integration", "integrator")
            await review("integration", True, "integration-content-v1")
            await accept("integration")
            await start("unrelated", "unrelated-author")
            await save("unrelated", "unrelated-author")
            await review("unrelated", True, "unrelated-content-v1")
            await accept("unrelated")

            reopened = Coordinator(store.path)
            for name, packet in admissions.items():
                assert reopened.admission(candidates[name]["id"]) == packet
                assert (
                    reopened.artifact(artifacts[name]["sha256"])
                    == artifacts[name]["text"]
                )
            await call(
                "owner",
                "revise",
                task_id=tasks["interface"]["id"],
                expected_revision=1,
                requirements="Approved synthetic interface; revision two.",
                checks=["fixture-content"],
                reviewers=["fixture-reviewer"],
            )
            invalidated = [
                "interface",
                "implementation-a",
                "implementation-b",
                "integration",
            ]
            for name in invalidated:
                await call(
                    "owner",
                    "admission",
                    expected=409,
                    candidate_id=candidates[name]["id"],
                )
            assert (
                await call(
                    "owner", "admission", candidate_id=candidates["unrelated"]["id"]
                )
                == admissions["unrelated"]
            )
    finally:
        await runner.cleanup()

    # Reopening after the service closes preserves artifacts and invalidations.
    reopened = Coordinator(store.path)
    for name, artifact in artifacts.items():
        assert reopened.artifact(artifact["sha256"]) == artifact["text"]
        if name in invalidated:
            try:
                reopened.admission(candidates[name]["id"])
            except Conflict:
                pass
            else:
                raise AssertionError("Reopen revived a stale admission")
    assert reopened.admission(candidates["unrelated"]["id"]) == admissions["unrelated"]
    report = {
        "example": "fixed-dependencies-v1",
        "passed": True,
        "principal_kind": "All eight identities, including the reviewer, are scripted fixtures.",
        "execution": "No execution adapter, worker process, model, Git repository or release.",
        "observed_concurrently_reserved_attempts": reserved,
        "parallel_execution_measured": False,
        "refusals": refusals,
        "tasks": {name: task["id"] for name, task in tasks.items()},
        "artifacts": artifacts,
        "historical_admissions": admissions,
        "historical_receipt_sha256": {
            name: digest(packet) for name, packet in admissions.items()
        },
        "invalidated_after_interface_revision": invalidated,
        "current_admissions_after_reopen": ["unrelated"],
        "saved_artifacts_after_reopen": len(artifacts),
        "allocation": {
            "seconds_per_task": 30,
            "task_budget_seconds": 60,
            "sum_allocated_seconds": sum(
                task["allocated_seconds"] for task in reopened.snapshot()["task"]
            ),
            "plan_wide_enforcement": False,
        },
        "elapsed_seconds": round(time.monotonic() - began, 3),
        "fresh_attempts_and_reviews_required": invalidated,
        "release_authorized": False,
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        type=Path,
        required=True,
        help="New directory, never an existing deployment or previous run",
    )
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without -O so every example assertion is enforced")
    report = asyncio.run(asyncio.wait_for(example(args.directory), timeout=60))
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "example",
                    "passed",
                    "saved_artifacts_after_reopen",
                    "current_admissions_after_reopen",
                    "parallel_execution_measured",
                    "allocation",
                    "elapsed_seconds",
                    "release_authorized",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
