"""Validate an installation or submit one explicit managed execution request."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from src.core.collaboration.managed_runtime import repository_identity


def service_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "service URL must be an HTTP(S) origin or base path without credentials"
        )
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("remote managed service requires HTTPS")
    return value.rstrip("/")


async def submit(url, token, payload):
    """Bounded request, no ambient proxy/cookies or credential-bearing redirects."""
    from aiohttp import ClientSession, ClientTimeout

    async with ClientSession(
        timeout=ClientTimeout(total=15),
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
    ) as session:
        async with session.post(
            service_url(url) + "/managed/enroll", json=payload, allow_redirects=False
        ) as response:
            if response.status != 200:
                raise ValueError(f"managed enrollment returned HTTP {response.status}")
            result = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                result.extend(chunk)
                if len(result) > 2_000_000:
                    raise ValueError("managed enrollment response exceeds limit")
            document = json.loads(result)
            if (
                not isinstance(document, dict)
                or document.get("schema_version") != 1
                or document.get("deployment_id") != payload["deployment_id"]
                or document.get("repository") != payload["repository"]
                or not isinstance(document.get("task"), dict)
                or not str(document["task"].get("id", "")).startswith("task_v2_")
            ):
                raise ValueError(
                    "managed enrollment returned an invalid scoped receipt"
                )
            return document


def _enrollment_arguments(parser):
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--token-env", default="POLYPHONY_INGRESS_TOKEN")
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument(
        "--repository", required=True, help="canonical github:github.com/owner/repo"
    )
    parser.add_argument(
        "--source", required=True, help="stable source identity, e.g. github:issue:19"
    )
    parser.add_argument(
        "--request-key",
        required=True,
        help="explicit execution request, reused on retry",
    )
    parser.add_argument("--title", required=True)
    parser.add_argument("--requirements-file", required=True)


def _validate_installation(argv):
    from .managed_installation import (
        ValidationContractUnavailable,
        validate_installation,
    )

    validation = argparse.ArgumentParser(
        description="Validate explicit inputs without service effects."
    )
    validation.add_argument("--credentials", required=True)
    validation.add_argument("--managed-config")
    validation.add_argument("--validation-contract", required=True)
    validation.add_argument("--json", action="store_true", required=True)
    args = validation.parse_args(argv)
    try:
        result = validate_installation(
            args.credentials, args.managed_config, args.validation_contract
        )
    except ValidationContractUnavailable:
        print("validation contract unavailable", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["valid"] else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "validate-installation":
        return _validate_installation(argv[1:])

    # Retain the enrollment parser, including options before its command.
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="For read-only validation use: validate-installation --help",
    )
    parser.add_argument("command", choices=["enroll"])
    _enrollment_arguments(parser)
    args = parser.parse_args(argv)
    from aiohttp import ClientError

    try:
        repository_identity(args.repository)
        token = os.environ.get(args.token_env, "")
        if len(token) < 32:
            raise ValueError(
                "configured ingress token environment variable is absent or too short"
            )
        path = Path(args.requirements_file)
        if path.stat().st_size > 1_000_000:
            raise ValueError("requirements file exceeds 1 MB")
        payload = {
            "schema_version": 1,
            "deployment_id": args.deployment_id,
            "repository": args.repository,
            "source": args.source,
            "request_key": args.request_key,
            "title": args.title,
            "requirements": path.read_text(),
        }
        print(json.dumps(asyncio.run(submit(args.url, token, payload)), indent=2))
        return 0
    except (ClientError, TimeoutError):
        print(
            "managed enrollment transport failed; outcome is uncertain, retry the same source and request key",
            file=sys.stderr,
        )
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
