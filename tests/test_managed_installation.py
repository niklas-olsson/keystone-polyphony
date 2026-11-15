"""Native installation validation and its startup/effect/privacy boundary."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from aiohttp.test_utils import TestClient, TestServer
import pytest

from src.core.collaboration.authority import Coordinator
from src.liminal_bridge import coordinator_service, managed_cli, managed_installation
from src.liminal_bridge.managed_configuration import validate_credentials

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads(
    (ROOT / "contracts/managed-installation-v1.fixtures.json").read_text()
)
REPOSITORY = "github:github.com/example/project"
TOKEN = "fixture-owner-token-0000000000000000"
MISSING = object()


def inputs(directory, credentials=MISSING, managed=MISSING, mode=0o600):
    credentials_path = directory / "private-credentials.json"
    credentials_path.write_text(
        json.dumps(FIXTURES["credentials"] if credentials is MISSING else credentials)
    )
    credentials_path.chmod(mode)
    managed_path = None
    if managed is not MISSING:
        managed_path = directory / "private-managed.json"
        managed_path.write_text(json.dumps(managed))
    return credentials_path, managed_path


def arguments(credentials_path, managed_path=None, contract="managed-installation-v1"):
    result = [
        "validate-installation",
        "--credentials",
        str(credentials_path),
        "--validation-contract",
        contract,
        "--json",
    ]
    if managed_path is not None:
        result.extend(["--managed-config", str(managed_path)])
    return result


def cli(
    directory, credentials_path, managed_path=None, contract="managed-installation-v1"
):
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "src.liminal_bridge.managed_cli",
            *arguments(credentials_path, managed_path, contract),
        ],
        cwd=directory,
        env=os.environ | {"PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def unchanged(path):
    metadata = path.stat()
    return path.read_bytes(), metadata.st_mode, metadata.st_mtime_ns


def assert_invalid(result, code, pointer):
    assert result == FIXTURES["disabled_result"] | {
        "valid": False,
        "managed_enabled": None,
        "errors": [{"code": code, "path": pointer}],
    }


def test_actual_cli_matches_golden_native_facts_without_input_or_state_mutation(
    tmp_path,
):
    paths = inputs(tmp_path, managed=FIXTURES["managed_config"])
    before = [unchanged(path) for path in paths]
    first = cli(tmp_path, *paths)
    second = cli(tmp_path, *paths)
    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    assert json.loads(first.stdout) == FIXTURES["disabled_result"] | {
        "managed_enabled": True,
        "descriptor": FIXTURES["expected_descriptor"],
    }
    assert [unchanged(path) for path in paths] == before
    assert set(tmp_path.iterdir()) == set(paths)
    # These expected digests are published golden values, not results from the
    # new validation/projection functions. Disabled profile encodings differ.
    assert {
        item["profile"]: item["profile_digest"]
        for item in json.loads(first.stdout)["descriptor"]["profiles"]
    } == {
        "root-only": "6595f86f8974feb376813ae2e942cd024239d8eb47a2e28ec6bc6067d61a701f",
        "child-enabled": "09e2c16ff2119c86eaf09de16480523ba149ec41fffb69fc2ac8edd9e3cad3d3",
        "child-disabled": "e6c0c13fd2e3358000f88b90da177478ed38a25a61902f3b8b9d8949157e5d98",
        "child-disabled-full": "d7ad2ce941475de9d1c4b534f8465d653af427e5ff855375520f4bae96bd98e2",
    }


@pytest.mark.parametrize("mode", [0o400, 0o600])
@pytest.mark.parametrize("managed", [MISSING, None])
def test_omitted_and_explicit_null_policy_are_disabled(tmp_path, mode, managed):
    result = cli(tmp_path, *inputs(tmp_path, managed=managed, mode=mode))
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == FIXTURES["disabled_result"]


@pytest.mark.parametrize(
    "credentials",
    [
        None,
        [],
        {},
        {"private-short-token": {"identity": "owner", "role": "owner"}},
        {TOKEN: []},
        {TOKEN: {"identity": True, "role": "owner"}},
        {TOKEN: {"identity": "", "role": "owner"}},
        {TOKEN: {"identity": "owner", "role": []}},
        {TOKEN: {"identity": "owner", "role": "private-unknown-role"}},
        {TOKEN: {"identity": "runtime", "role": "runtime"}},
        {TOKEN: {"identity": "ingress", "role": "ingress", "repositories": []}},
        {TOKEN: {"identity": "owner", "role": "owner", "repositories": [REPOSITORY]}},
        {TOKEN: {"identity": "private-\ud800", "role": "owner"}},
        {"\ud800" * 32: {"identity": "owner", "role": "owner"}},
    ],
)
def test_malformed_credentials_are_safe_and_never_partially_approved(
    tmp_path, credentials
):
    paths = inputs(
        tmp_path, credentials=credentials, managed=FIXTURES["managed_config"]
    )
    result = managed_installation.validate_installation(*paths)
    assert_invalid(result, "credentials_invalid", "/credentials")
    assert "private-" not in json.dumps(result)


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), True),
        (("schema_version",), 2),
        (("deployment_id",), "private-\ud800"),
        (("repositories",), []),
        (("repositories", REPOSITORY, "checks"), ["tests", "tests"]),
        (("repositories", REPOSITORY, "reviewers"), []),
        (("repositories", REPOSITORY, "max_attempts"), True),
        (("repositories", REPOSITORY, "max_attempts"), 1001),
        (("repositories", REPOSITORY, "budget_seconds"), True),
        (("repositories", REPOSITORY, "budget_seconds"), 10**400),
        (("repositories", REPOSITORY, "profiles", "root-only", "base"), "main"),
        (("repositories", REPOSITORY, "profiles", "root-only", "attempt_seconds"), 901),
        (
            ("repositories", REPOSITORY, "profiles", "root-only", "capabilities"),
            "python",
        ),
        (
            ("repositories", REPOSITORY, "profiles", "root-only", "command"),
            ["private-command"],
        ),
        (
            ("repositories", REPOSITORY, "profiles", "child-enabled", "max_children"),
            True,
        ),
        (("repositories", REPOSITORY, "profiles", "child-enabled", "max_children"), 65),
        (
            (
                "repositories",
                REPOSITORY,
                "profiles",
                "child-enabled",
                "max_child_seconds",
            ),
            301,
        ),
        (
            (
                "repositories",
                REPOSITORY,
                "profiles",
                "child-enabled",
                "max_child_turns",
            ),
            True,
        ),
        (
            (
                "repositories",
                REPOSITORY,
                "profiles",
                "child-enabled",
                "child_capabilities",
            ),
            ["enlarged"],
        ),
    ],
)
def test_native_policy_rejections_have_only_safe_error_codes(tmp_path, path, value):
    managed = copy.deepcopy(FIXTURES["managed_config"])
    target = managed
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result = managed_installation.validate_installation(
        *inputs(tmp_path, managed=managed)
    )
    assert_invalid(result, "managed_config_invalid", "/managed_config")


@pytest.mark.parametrize("kind", ["credentials", "managed_config"])
@pytest.mark.parametrize(
    "content", [b'{"private-token":', b'{"x": NaN}', b'{"x": Infinity}', b"\xff"]
)
def test_invalid_json_and_encoding_do_not_echo_inputs_or_paths(tmp_path, kind, content):
    credentials_path, managed_path = inputs(
        tmp_path, managed=FIXTURES["managed_config"]
    )
    selected = credentials_path if kind == "credentials" else managed_path
    selected.write_bytes(content)
    result = cli(tmp_path, credentials_path, managed_path)
    assert result.returncode == 1 and result.stderr == ""
    error = "_unreadable" if content == b"\xff" else "_invalid_json"
    assert_invalid(json.loads(result.stdout), kind + error, "/" + kind)
    assert str(selected) not in result.stdout and "private-token" not in result.stdout


def test_missing_files_bad_permissions_and_unknown_contract_are_safe(tmp_path):
    credentials_path, managed_path = inputs(
        tmp_path, managed=FIXTURES["managed_config"]
    )
    credentials_path.chmod(0o640)
    assert_invalid(
        managed_installation.validate_installation(credentials_path, managed_path),
        "credentials_permissions",
        "/credentials",
    )
    credentials_path.chmod(0o600)
    managed_path.unlink()
    assert_invalid(
        managed_installation.validate_installation(credentials_path, managed_path),
        "managed_config_unreadable",
        "/managed_config",
    )
    credentials_path.unlink()
    result = cli(
        tmp_path, credentials_path, managed_path, "private-unsupported-version"
    )
    assert result.returncode == 1 and result.stderr == ""
    assert_invalid(
        json.loads(result.stdout), "unsupported_contract", "/validation_contract"
    )
    assert "private-unsupported-version" not in result.stdout
    assert_invalid(
        managed_installation.validate_installation(credentials_path),
        "credentials_unreadable",
        "/credentials",
    )


def test_unscoped_roles_and_well_formed_unavailable_scopes_remain_valid(tmp_path):
    credentials = {
        ("fixture-" + role).ljust(32, "0"): {"identity": role, "role": role}
        for role in ("owner", "reviewer", "conductor", "worker")
    }
    credentials["fixture-foreign-runtime".ljust(32, "0")] = {
        "identity": "runtime",
        "role": "runtime",
        "deployment_id": "different-installation",
        "repositories": ["github:github.com/example/other"],
    }
    assert validate_credentials(credentials) == credentials
    result = managed_installation.validate_installation(
        *inputs(tmp_path, credentials, FIXTURES["managed_config"])
    )
    assert result["valid"] and result["descriptor"] == FIXTURES["expected_descriptor"]


def test_descriptor_does_not_inherit_a_consumers_lower_profile_limit(tmp_path):
    managed = copy.deepcopy(FIXTURES["managed_config"])
    policy = managed["repositories"][REPOSITORY]
    root_profile = policy["profiles"]["root-only"]
    policy["profiles"] = {f"profile-{n:03}": root_profile for n in range(100)}
    managed["repositories"]["github:github.com/example/other"] = policy | {
        "profiles": {"root-only": root_profile}
    }
    result = managed_installation.validate_installation(
        *inputs(tmp_path, managed=managed)
    )
    assert result["valid"] and len(result["descriptor"]["profiles"]) == 101


def test_native_64_character_base_remains_valid(tmp_path):
    managed = copy.deepcopy(FIXTURES["managed_config"])
    managed["repositories"][REPOSITORY]["profiles"]["root-only"]["base"] = "a" * 64
    result = managed_installation.validate_installation(
        *inputs(tmp_path, managed=managed)
    )
    assert result["valid"]
    root = next(
        row for row in result["descriptor"]["profiles"] if row["profile"] == "root-only"
    )
    assert root["base"] == "a" * 64


@pytest.mark.asyncio
async def test_actual_live_capabilities_equal_static_projection_and_scopes_still_refuse(
    tmp_path,
):
    credentials = copy.deepcopy(FIXTURES["credentials"])
    credentials["fixture-foreign-token".ljust(32, "0")] = {
        "identity": "foreign",
        "role": "runtime",
        "deployment_id": "foreign",
        "repositories": [REPOSITORY],
    }
    managed = FIXTURES["managed_config"]
    async with TestClient(
        TestServer(
            coordinator_service.create_app(
                Coordinator(tmp_path / "live.db"), credentials, managed=managed
            )
        )
    ) as client:
        query = {"deployment_id": managed["deployment_id"], "repository": REPOSITORY}
        for expected in FIXTURES["expected_descriptor"]["profiles"]:
            response = await client.get(
                "/managed/capabilities",
                params=query | {"profile": expected["profile"]},
                headers={"Authorization": "Bearer " + TOKEN},
            )
            assert response.status == 200
            live = await response.json()
            for key, value in expected.items():
                if key not in {"max_attempts", "budget_seconds"}:
                    assert live[key] == value
        response = await client.get(
            "/managed/snapshot",
            params=query,
            headers={
                "Authorization": "Bearer " + "fixture-foreign-token".ljust(32, "0")
            },
        )
        assert response.status == 403


@pytest.mark.parametrize("invalid", ["credentials", "permissions", "managed", "json"])
def test_startup_refuses_before_coordinator_adapter_or_listener(
    tmp_path, monkeypatch, capsys, invalid
):
    from src.liminal_bridge import worktree_adapter

    credentials_path, managed_path = inputs(
        tmp_path, managed=FIXTURES["managed_config"]
    )
    if invalid == "credentials":
        credentials_path.write_text(
            json.dumps({TOKEN: {"identity": True, "role": "owner"}})
        )
    elif invalid == "permissions":
        credentials_path.chmod(0o644)
    elif invalid == "managed":
        managed_path.write_text('{"schema_version": true}')
    else:
        managed_path.write_text("private-invalid-json")
    effects = []

    def unexpected(*args, **kwargs):
        effects.append("stateful constructor")
        raise AssertionError("startup crossed validation boundary")

    monkeypatch.setattr(coordinator_service, "Coordinator", unexpected)
    monkeypatch.setattr(worktree_adapter, "GitWorktreeAdapter", unexpected)
    monkeypatch.setattr(coordinator_service.web, "run_app", unexpected)
    with pytest.raises(SystemExit) as caught:
        coordinator_service.main(
            [
                "--db",
                str(tmp_path / "not-created" / "authority.db"),
                "--credentials",
                str(credentials_path),
                "--managed-config",
                str(managed_path),
                "--execution-config",
                str(tmp_path / "not-read-execution.json"),
            ]
        )
    assert caught.value.code == 2 and effects == []
    assert not (tmp_path / "not-created").exists()
    stderr = capsys.readouterr().err
    assert str(credentials_path) not in stderr and str(managed_path) not in stderr
    assert "private-invalid-json" not in stderr and TOKEN not in stderr


def test_startup_validation_does_not_depend_on_static_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(
        managed_installation, "CONTRACT_PATH", tmp_path / "absent-contract"
    )
    credentials, managed = managed_installation.read_installation_inputs(
        *inputs(tmp_path)
    )
    assert credentials == FIXTURES["credentials"] and managed is None
    with pytest.raises(
        managed_installation.ValidationContractUnavailable,
        match="^validation contract unavailable$",
    ):
        managed_installation.validate_installation(
            tmp_path / "private-credentials.json"
        )


def test_cold_actual_cli_has_no_application_effects_or_provider_imports(tmp_path):
    paths = inputs(tmp_path, managed=FIXTURES["managed_config"])
    audit_program = r"""
import runpy
import sys

def audit(event, args):
    forbidden = {"socket.__new__", "socket.connect", "socket.bind", "socket.getaddrinfo",
                 "subprocess.Popen", "os.system", "os.posix_spawn", "sqlite3.connect"}
    if event in forbidden:
        raise AssertionError("unexpected application effect: " + event)
    if event == "import" and args[0].split(".")[0] in {"openai", "anthropic", "ollama"}:
        raise AssertionError("unexpected provider import")
    if event == "open" and isinstance(args[2], int) and args[2] & 3:
        raise AssertionError("unexpected file write")

sys.addaudithook(audit)
sys.argv = ["managed_cli", *sys.argv[1:]]
runpy.run_module("src.liminal_bridge.managed_cli", run_name="__main__")
"""
    before = [unchanged(path) for path in paths]
    result = subprocess.run(
        [sys.executable, "-B", "-c", audit_program, *arguments(*paths)],
        cwd=tmp_path,
        env=os.environ
        | {"PYTHONPATH": str(ROOT), "POLYPHONY_INGRESS_TOKEN": "private-ambient-token"},
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["descriptor"] == FIXTURES["expected_descriptor"]
    assert [unchanged(path) for path in paths] == before
    assert set(tmp_path.iterdir()) == set(paths)


def test_static_contract_digest_and_declared_result_keys_match():
    artifact = json.loads(managed_installation.CONTRACT_PATH.read_text())
    canonical = json.dumps(
        artifact,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    assert (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        == FIXTURES["contract_digest"]
    )
    assert set(artifact["result"]["required"]) == set(FIXTURES["disabled_result"])
    assert set(artifact["result"]["$defs"]["profile"]["required"]) == set(
        FIXTURES["expected_descriptor"]["profiles"][0]
    )


@pytest.mark.parametrize("command_first", [True, False])
def test_existing_enrollment_cli_ordering_remains_supported(
    tmp_path, monkeypatch, command_first
):
    async def submit(_url, token, payload):
        assert token == "fixture-token".ljust(32, "0")
        assert payload["request_key"] == "original-request"
        return {"task": {"id": "task_v2_fixture"}}

    monkeypatch.setattr(managed_cli, "submit", submit)
    monkeypatch.setenv("POLYPHONY_INGRESS_TOKEN", "fixture-token".ljust(32, "0"))
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("synthetic requirements")
    args = [
        "--deployment-id",
        "fixture",
        "--repository",
        REPOSITORY,
        "--source",
        "fixture-source",
        "--request-key",
        "original-request",
        "--title",
        "fixture title",
        "--requirements-file",
        str(requirements),
    ]
    args.insert(0 if command_first else len(args), "enroll")
    assert managed_cli.main(args) == 0
