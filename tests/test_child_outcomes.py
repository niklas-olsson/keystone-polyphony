"""Initial child semantic reports share the existing private capture barrier."""

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
import pytest

from src.core.collaboration.authority import Coordinator, Conflict, canonical, digest
from src.core.collaboration.child_outcomes import initial_outcome
from src.liminal_bridge.coordinator_service import create_app
from src.liminal_bridge.managed_service import validate_managed_config
from test_child_grants import (
    REPOSITORY,
    SCOPE,
    capture_root,
    child,
    config as grant_config,
    end_child,
    end_root,
    profiles,
    root_attempt,
)

config = grant_config


def report(binding, value="completed", summary="Private child outcome λ"):
    payload = {"request_key": "report:one", "outcome": value, "summary": summary}
    return {
        "schema_version": 1,
        "work_turn_id": digest(
            ["polyphony-child-work-v1", binding["child_id"], binding["grant_digest"], 1]
        ),
        "ordinal": 1,
        **{
            key: binding[key]
            for key in (
                "child_id",
                "root_attempt_id",
                "parent_attempt_id",
                "task_id",
                "requirements_revision",
                "grant_digest",
                "instructions_digest",
            )
        },
        "source": (
            "child_report_via_bound_runtime" if value is not None else "unreported"
        ),
        "report": (
            payload | {"payload_digest": digest(payload)} if value is not None else None
        ),
    }


def capture(store, root, grant, manifest):
    return {
        kind: store.managed_child_capture(
            **SCOPE,
            runtime_principal="runtime",
            attempt_id=root["id"],
            child_id=grant["child_id"],
            kind=kind,
            content=content,
        )["artifact_id"]
        for kind, content in {
            "patch": "partial patch remains even when declined",
            "log": "Private child log",
            "manifest": manifest,
        }.items()
    }


@pytest.fixture
def running(tmp_path, config):
    store = Coordinator(tmp_path / "authority.db", clock=lambda: 100, boot_id="boot")
    task, root = root_attempt(store, config)
    binding = child(store, config, root)["binding"]
    return store, task, root, binding


def read_child(store, root, binding):
    return store.managed_children_read(
        **SCOPE,
        runtime_principal="runtime",
        attempt_id=root["id"],
        child_id=binding["child_id"],
        profiles={},
    )["children"][0]["child"]


def test_frozen_unicode_interoperability_vector():
    vector = json.loads(
        (Path(__file__).parent / "fixtures/child-initial-outcome-v1.json").read_text()
    )
    expected = vector["manifest"]["work_outcome"]
    assert (
        report(
            vector["binding"],
            "needs_clarification",
            "Which behavior should λ preserve?",
        )
        == expected
    )
    assert initial_outcome(canonical(vector["manifest"]), vector["binding"]) == expected


@pytest.mark.parametrize(
    "semantic", ["completed", "declined", "needs_clarification", "failed", None]
)
def test_final_report_recovers_without_reallocating_or_releasing_root(
    running, semantic
):
    store, _, root, binding = running
    outcome = report(binding, semantic)
    manifest = canonical({"work_outcome": outcome})
    hashes = capture(store, root, binding, manifest)
    cid = binding["child_id"]
    before = store.snapshot()["task"][0]
    assert "initial_outcome" not in read_child(store, root, binding)
    with pytest.raises(Conflict, match="unresolved child"):
        end_root(store, root, capture_root(store, root))
    final = end_child(store, root, cid, hashes)
    assert final["initial_outcome"] == outcome | {
        "artifacts": hashes,
        "canonical_committed": True,
        "release_authorized": False,
    }
    reopened = Coordinator(store.path, clock=lambda: 100, boot_id="boot")
    assert end_child(reopened, root, cid, hashes) == final
    assert read_child(reopened, root, binding) == final
    assert capture(reopened, root, binding, manifest) == hashes
    assert reopened.snapshot()["task"][0] == before
    assert not reopened.snapshot()["attempt"][0]["termination_confirmed"]
    recovered = reopened.managed_child_artifact(
        **SCOPE,
        runtime_principal="runtime",
        attempt_id=root["id"],
        child_id=cid,
        artifact_id=hashes["manifest"],
    )
    assert recovered == manifest
    assert final["turns_allocated"] == 0


@pytest.mark.parametrize("content", ['{ "legacy": 1 }\n', '{"legacy":1,"legacy":2}'])
def test_legacy_absence_is_distinct_from_explicit_unreported(running, content):
    store, _, root, binding = running
    hashes = capture(store, root, binding, content)
    final = end_child(store, root, binding["child_id"], hashes)
    assert "initial_outcome" not in final
    assert (
        store.managed_child_artifact(
            **SCOPE,
            runtime_principal="runtime",
            attempt_id=root["id"],
            child_id=binding["child_id"],
            artifact_id=hashes["manifest"],
        )
        == content
    )
    end_root(store, root, capture_root(store, root))


INVALID_FIELDS = [
    ("schema_version", True),
    ("schema_version", 1.0),
    ("schema_version", 2),
    ("ordinal", True),
    ("ordinal", 2),
    ("requirements_revision", True),
    ("requirements_revision", 1.0),
    ("requirements_revision", 2),
    ("child_id", "other-child"),
    ("root_attempt_id", "other-root"),
    ("parent_attempt_id", "other-parent"),
    ("task_id", "other-task"),
    ("grant_digest", "a" * 64),
    ("instructions_digest", "a" * 64),
    ("work_turn_id", "a" * 64),
    ("source", "unreported"),
    ("source", "parent"),
    ("report", {}),
    ("report", []),
    ("report.request_key", ""),
    ("report.request_key", "key with spaces"),
    ("report.request_key", "k" * 129),
    ("report.request_key", 1),
    ("report.outcome", "accepted"),
    ("report.outcome", {}),
    ("report.summary", "λ" * 4097),
    ("report.summary", "embedded\x00NUL"),
    ("report.summary", 1),
    ("report.payload_digest", "0" * 64),
    ("release_authorized", True),
    ("report.release_authorized", True),
]


@pytest.mark.parametrize("path,value", INVALID_FIELDS)
def test_malformed_report_cannot_poison_immutable_manifest_slot(running, path, value):
    store, _, root, binding = running
    valid = report(binding)
    invalid = copy.deepcopy(valid)
    target = invalid
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    with pytest.raises(ValueError):
        capture(store, root, binding, canonical({"work_outcome": invalid}))
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT 1 FROM records WHERE kind='child_capture' AND id=?",
                (digest([binding["child_id"], "manifest"]),),
            ).fetchone()
            is None
        )
    assert not read_child(store, root, binding)["termination_confirmed"]
    hashes = capture(store, root, binding, canonical({"work_outcome": valid}))
    assert (
        end_child(store, root, binding["child_id"], hashes)["initial_outcome"]["report"]
        == valid["report"]
    )


@pytest.mark.parametrize("replacement", [None, {}, [], {"schema_version": 1}])
def test_present_extension_cannot_silently_downgrade(running, replacement):
    store, _, root, binding = running
    with pytest.raises(ValueError):
        capture(store, root, binding, canonical({"work_outcome": replacement}))


def test_null_report_requires_exact_unreported_source_and_no_ambiguous_json(running):
    store, _, root, binding = running
    outcome = report(binding, None)
    outcome["source"] = "child_report_via_bound_runtime"
    with pytest.raises(ValueError, match="unreported"):
        capture(store, root, binding, canonical({"work_outcome": outcome}))
    valid = canonical({"work_outcome": report(binding)})
    ambiguous = valid.replace('"ordinal":1', '"ordinal":2,"ordinal":1')
    with pytest.raises(ValueError, match="duplicate"):
        capture(store, root, binding, ambiguous)
    with pytest.raises(ValueError, match="duplicate"):
        capture(store, root, binding, '{"work_outcome":null,' + valid[1:])


@pytest.mark.parametrize("summary", ["", "λ" * 4096])
def test_summary_bound_is_utf8_bytes_and_accepts_exact_limit(running, summary):
    store, _, root, binding = running
    outcome = report(binding, summary=summary)
    hashes = capture(store, root, binding, canonical({"work_outcome": outcome}))
    assert (
        end_child(store, root, binding["child_id"], hashes)["initial_outcome"][
            "report"
        ]["summary"]
        == summary
    )


def test_report_slot_is_immutable_even_under_a_new_request_key(running):
    store, _, root, binding = running
    outcome = report(binding)
    hashes = capture(store, root, binding, canonical({"work_outcome": outcome}))
    changed = report(binding, "declined")
    changed["report"]["request_key"] = "another-key"
    changed["report"]["payload_digest"] = digest(
        {key: changed["report"][key] for key in ("request_key", "outcome", "summary")}
    )
    with pytest.raises(Conflict, match="different content"):
        capture(store, root, binding, canonical({"work_outcome": changed}))
    final = end_child(store, root, binding["child_id"], hashes)
    assert final["initial_outcome"]["report"] == outcome["report"]


def test_concurrent_reports_use_one_existing_capture_slot(running):
    store, _, root, binding = running

    def submit(semantic):
        local = Coordinator(store.path, clock=lambda: 100, boot_id="boot")
        try:
            return capture(
                local,
                root,
                binding,
                canonical({"work_outcome": report(binding, semantic)}),
            )
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ["completed", "declined"]))
    winners = [hashes for hashes in results if hashes is not None]
    assert len(winners) == 1
    final = end_child(store, root, binding["child_id"], winners[0])
    assert final["initial_outcome"]["report"]["outcome"] in {"completed", "declined"}


def test_historical_report_delivery_does_not_require_current_permission(running):
    store, task, root, binding = running
    store.revise(task["id"], 1, "changed requirements", ["tests"], ["reviewer"])
    store.managed_child_control(
        **SCOPE,
        runtime_principal="runtime",
        attempt_id=root["id"],
        action="revoke",
        child_id=binding["child_id"],
        reason="stop new effects",
    )
    reopened = Coordinator(store.path, clock=lambda: 1000, boot_id="other-boot")
    outcome = report(binding, "failed")
    hashes = capture(reopened, root, binding, canonical({"work_outcome": outcome}))
    final = end_child(reopened, root, binding["child_id"], hashes)
    assert final["initial_outcome"]["requirements_revision"] == 1
    assert read_child(reopened, root, binding) == final
    with pytest.raises(Conflict, match="permission"):
        reopened.managed_child_turn(
            **SCOPE,
            runtime_principal="runtime",
            attempt_id=root["id"],
            child_id=binding["child_id"],
            request_key="cannot-retry-work",
            request_digest="f" * 64,
            profiles={},
        )


def replace_saved_manifest(store, binding, hashes, content, *, terminal=False):
    """Model imported/corrupted durable evidence with a valid content hash."""
    artifact = hashlib.sha256(content.encode()).hexdigest()
    changed = hashes | {"manifest": artifact}
    with store.transaction() as db:
        db.execute("INSERT INTO artifacts VALUES(?,?)", (artifact, content.encode()))
        key = digest([binding["child_id"], "manifest"])
        saved = store._get(db, "child_capture", key)
        saved["artifact_id"] = artifact
        store._save(db, "child_capture", key, saved)
        if terminal:
            record = store._get(db, "child", binding["child_id"])
            record["captured_artifacts"] = changed
            record["runtime_receipt"]["artifacts"] = changed
            record["initial_outcome"]["artifacts"] = changed
            store._save(db, "child", record["id"], record)
    return changed


def test_observation_revalidates_legacy_stored_extension_before_termination(running):
    store, _, root, binding = running
    outcome = report(binding)
    hashes = capture(store, root, binding, canonical({"work_outcome": outcome}))
    outcome["instructions_digest"] = "0" * 64
    changed = replace_saved_manifest(
        store, binding, hashes, canonical({"work_outcome": outcome})
    )
    with pytest.raises(Conflict, match="durable outcome verification"):
        end_child(store, root, binding["child_id"], changed)
    assert not read_child(store, root, binding)["termination_confirmed"]
    with pytest.raises(Conflict, match="unresolved child"):
        end_root(store, root, capture_root(store, root))
    # Rejection leaves the damaged historical record inspectable, not rewritten.
    with store.connect() as db:
        assert (
            store._get(db, "child_capture", digest([binding["child_id"], "manifest"]))[
                "artifact_id"
            ]
            == changed["manifest"]
        )


@pytest.mark.parametrize("consumer", ["root_terminal", "candidate"])
def test_root_use_revalidates_child_outcome_after_its_terminal_receipt(
    running, consumer
):
    store, _, root, binding = running
    outcome = report(binding)
    hashes = capture(store, root, binding, canonical({"work_outcome": outcome}))
    end_child(store, root, binding["child_id"], hashes)
    root_hashes = capture_root(store, root)
    if consumer == "candidate":
        end_root(store, root, root_hashes)
    outcome["root_attempt_id"] = "different-root"
    replace_saved_manifest(
        store, binding, hashes, canonical({"work_outcome": outcome}), terminal=True
    )
    with pytest.raises(Conflict, match="durable outcome verification"):
        if consumer == "root_terminal":
            end_root(store, root, root_hashes)
        else:
            store.managed_candidate(
                **SCOPE,
                runtime_principal="runtime",
                attempt_id=root["id"],
                artifact_id=root_hashes["patch"],
            )


def test_durable_projection_cannot_diverge_from_its_manifest(running):
    store, _, root, binding = running
    hashes = capture(store, root, binding, canonical({"work_outcome": report(binding)}))
    end_child(store, root, binding["child_id"], hashes)
    with store.transaction() as db:
        saved = store._get(db, "child", binding["child_id"])
        # Python dictionary equality would incorrectly accept an integer bool alias.
        saved["initial_outcome"]["canonical_committed"] = 1
        store._save(db, "child", saved["id"], saved)
    with pytest.raises(Conflict, match="projection"):
        end_child(store, root, binding["child_id"], hashes)
    with pytest.raises(Conflict, match="projection"):
        end_root(store, root, capture_root(store, root))


@pytest.mark.asyncio
async def test_http_capability_final_recovery_and_private_scope(running, config):
    store, _, root, binding = running
    cid = binding["child_id"]
    _, other_root = root_attempt(store, config, "different")
    credentials = {
        character
        * 40: {
            "identity": identity,
            "role": role,
            **(
                {"deployment_id": SCOPE["deployment_id"], "repositories": [REPOSITORY]}
                if role in {"runtime", "ingress"}
                else {}
            ),
        }
        for character, identity, role in (
            ("r", "runtime", "runtime"),
            ("f", "foreign", "runtime"),
            ("i", "ingress", "ingress"),
            ("v", "reviewer", "reviewer"),
            ("o", "owner", "owner"),
        )
    }
    original_config = copy.deepcopy(config)
    outcome = report(binding, "needs_clarification")
    manifest = canonical({"work_outcome": outcome})

    def headers(token):
        return {"Authorization": "Bearer " + token * 40}

    async with TestClient(
        TestServer(create_app(store, credentials, managed=config))
    ) as client:

        async def post(operation, fields, status=200, token="r"):
            response = await client.post(
                "/managed/children/" + operation,
                json={
                    "schema_version": 1,
                    **SCOPE,
                    "attempt_id": root["id"],
                    "child_id": cid,
                    **fields,
                },
                headers=headers(token),
            )
            assert response.status == status, await response.text()
            return (
                await response.json()
                if response.content_type == "application/json"
                else None
            )

        cap = await client.get(
            "/managed/capabilities",
            params=SCOPE | {"profile": "approved"},
            headers=headers("r"),
        )
        capabilities = await cap.json()
        assert type(capabilities["child_initial_outcome"]) is int
        assert capabilities["child_initial_outcome"] == 1
        assert capabilities["children"] is False
        assert capabilities["profile_digest"] == digest(profiles(config)["approved"])
        assert validate_managed_config(config) == original_config
        bad = copy.deepcopy(outcome)
        bad["parent_attempt_id"] = other_root["id"]
        await post(
            "capture",
            {"kind": "manifest", "content": canonical({"work_outcome": bad})},
            400,
        )
        hashes = {}
        for kind, content in {
            "patch": "Private partial work",
            "log": "Private log",
            "manifest": manifest,
        }.items():
            saved = await post("capture", {"kind": kind, "content": content})
            hashes[kind] = saved["artifact_id"]
        terminal = {
            "state": "exited",
            "termination_confirmed": True,
            "launch_fenced": True,
            "artifacts": hashes,
        }
        saved = await post("observe", terminal)
        assert saved["child"]["initial_outcome"]["report"] == outcome["report"]
        assert await post("observe", terminal) == saved
        query = SCOPE | {"attempt_id": root["id"]}
        for token, expected in (
            ("r", 200),
            ("o", 200),
            ("f", 409),
            ("v", 403),
            ("i", 403),
        ):
            response = await client.get(
                "/managed/children/" + cid, params=query, headers=headers(token)
            )
            assert response.status == expected
            if expected == 200:
                assert (await response.json())["children"][0]["child"] == saved["child"]
            response = await client.get(
                f"/managed/children/{cid}/artifacts/{hashes['manifest']}",
                params=query,
                headers=headers(token),
            )
            assert response.status == expected
        response = await client.get(
            "/managed/children/" + cid,
            params=query | {"attempt_id": other_root["id"]},
            headers=headers("r"),
        )
        assert response.status == 409
        for path, token, params in (
            ("/snapshot", "v", {}),
            ("/managed/snapshot", "r", SCOPE),
        ):
            response = await client.get(path, params=params, headers=headers(token))
            assert response.status == 200
            text = await response.text()
            assert "Private child outcome" not in text
            assert "initial_outcome" not in text and "work_outcome" not in text
            assert binding["instructions_digest"] not in text
        response = await client.get(
            "/artifacts/" + hashes["manifest"], headers=headers("v")
        )
        assert response.status == 409
