"""Read-only installation validation, separate from service construction."""

import hashlib
import json
from pathlib import Path

from .managed_configuration import (
    profile_capabilities,
    validate_credentials,
    validate_managed_config,
)

VALIDATION_CONTRACT = "managed-installation-v1"
CONTRACT_PATH = (
    Path(__file__).resolve().parents[2] / "contracts" / (VALIDATION_CONTRACT + ".json")
)


class InstallationInputError(ValueError):
    """A bounded public error, never an exception containing input values."""

    def __init__(self, code, path):
        super().__init__(code)
        self.code = code
        self.path = path


class ValidationContractUnavailable(ValueError):
    """The installed static artifact cannot establish its advertised contract."""


def _reject_constant(_value):
    raise ValueError("nonfinite JSON numbers are unsupported")


def _read_json(path, kind):
    pointer = "/" + kind
    try:
        selected = Path(path)
        if kind == "credentials" and selected.stat().st_mode & 0o077:
            raise InstallationInputError("credentials_permissions", pointer)
        content = selected.read_text(encoding="utf-8")
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        if isinstance(exc, InstallationInputError):
            raise
        raise InstallationInputError(kind + "_unreadable", pointer) from None
    try:
        return json.loads(content, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        raise InstallationInputError(kind + "_invalid_json", pointer) from None


def read_installation_inputs(credentials_path, managed_config_path=None):
    """Validate explicit input files before creating any application state.

    Startup uses this function without loading the static output contract.
    Existing owner-only credential modes, including 0400 and 0600, are kept.
    """
    credentials = _read_json(credentials_path, "credentials")
    try:
        credentials = validate_credentials(credentials)
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        RecursionError,
    ):
        raise InstallationInputError("credentials_invalid", "/credentials") from None
    managed = (
        _read_json(managed_config_path, "managed_config")
        if managed_config_path is not None
        else None
    )
    try:
        managed = validate_managed_config(managed)
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        RecursionError,
    ):
        raise InstallationInputError(
            "managed_config_invalid", "/managed_config"
        ) from None
    return credentials, managed


def validation_contract_digest():
    """Hash the static contract without redefining native profile digests."""
    try:
        document = json.loads(
            CONTRACT_PATH.read_text(encoding="utf-8"), parse_constant=_reject_constant
        )
        if (
            not isinstance(document, dict)
            or type(document.get("schema_version")) is not int
            or document["schema_version"] != 1
            or document.get("service") != "keystone-polyphony"
            or document.get("validation_contract") != VALIDATION_CONTRACT
        ):
            raise ValueError("unexpected contract identity")
        canonical = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        raise ValidationContractUnavailable("validation contract unavailable") from None


def installation_descriptor(managed):
    """Project validated native facts; this is neither a grant nor attestation."""
    profiles = []
    for repository, policy in sorted(managed["repositories"].items()):
        for name, profile in sorted(policy["profiles"].items()):
            profiles.append(
                {
                    "repository": repository,
                    "profile": name,
                    **profile_capabilities(profile),
                    "max_attempts": policy["max_attempts"],
                    "budget_seconds": policy["budget_seconds"],
                }
            )
    return {
        "protocol": "managed-runtime-v1",
        "deployment_id": managed["deployment_id"],
        "profiles": profiles,
    }


def validate_installation(
    credentials_path, managed_config_path=None, validation_contract=VALIDATION_CONTRACT
):
    """Return the closed nonsecret result for exactly the supplied input files."""
    result = {
        "schema_version": 1,
        "service": "keystone-polyphony",
        "validation_contract": VALIDATION_CONTRACT,
        "contract_digest": validation_contract_digest(),
        "valid": False,
        "managed_enabled": None,
        "descriptor": None,
        "errors": [],
    }
    try:
        if validation_contract != VALIDATION_CONTRACT:
            raise InstallationInputError("unsupported_contract", "/validation_contract")
        _, managed = read_installation_inputs(credentials_path, managed_config_path)
    except InstallationInputError as exc:
        result["errors"] = [{"code": exc.code, "path": exc.path}]
        return result
    result.update(
        valid=True,
        managed_enabled=managed is not None,
        descriptor=installation_descriptor(managed) if managed is not None else None,
    )
    return result
