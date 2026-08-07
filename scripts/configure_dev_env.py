#!/usr/bin/env python3
"""Create a secret, MinIO-backed local-development ``.env``.

The production template deliberately contains no usable credentials. This
helper is the single copy-paste path for a local Compose evaluation: it creates
the target atomically, never overwrites it, and never prints generated secrets.
"""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = REPO_ROOT / ".env.example"
DEFAULT_OUTPUT = REPO_ROOT / ".env"


def _neo4j_password() -> str:
    """Return a strong password that Neo4j cannot parse as a CLI option."""

    return f"hm_{secrets.token_urlsafe(32)}"


def _updates() -> dict[str, str]:
    return {
        "ADMIN_BOOTSTRAP_KEY": secrets.token_urlsafe(48),
        "S3_ENDPOINT_URL": "http://minio:9000",
        "S3_ACCESS_KEY_ID": "hivemind-dev",
        "S3_SECRET_ACCESS_KEY": secrets.token_urlsafe(32),
        "S3_BUCKET_NAME": "hivemind",
        "S3_REGION_NAME": "us-east-1",
        "S3_SIGNATURE_MODE": "sigv4",
        "HIVEMIND_MESH_ENABLED": "false",
        "HIVEMIND_MESH_PUBLIC_URL": "",
        "HIVEMIND_MESH_PRIVATE_KEY": "",
        "HIVEMIND_MESH_DISPLAY_NAME": "",
        "NEO4J_PASSWORD": _neo4j_password(),
        "SITE_ADDRESS": ":8080",
        "WAF_PORT": "8080",
    }


def render(template: str, updates: dict[str, str]) -> str:
    """Return the template with every expected assignment replaced once."""
    remaining = set(updates)
    rendered: list[str] = []
    for line in template.splitlines(keepends=True):
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            rendered.append(f"{key}={updates[key]}\n")
            remaining.remove(key)
        else:
            rendered.append(line)
    if remaining:
        missing = ", ".join(sorted(remaining))
        raise ValueError(f"template is missing required assignments: {missing}")
    return "".join(rendered)


def create_dev_env(template_path: Path, output_path: Path) -> None:
    template = template_path.read_text(encoding="utf-8")
    content = render(template, _updates())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a mode-0600 local .env for the Compose dev profile."
    )
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    try:
        create_dev_env(args.template.resolve(), args.output.resolve())
    except FileExistsError:
        parser.error(f"refusing to overwrite existing file: {args.output}")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    print(f"Created {args.output} with local MinIO and Mesh disabled.")
    print(
        "Before mid/long, configure LLMAAS_API_URL, LLMAAS_API_KEY, "
        "LLMAAS_MODEL, LLMAAS_EMBEDDING_MODEL, and "
        "LLMAAS_EMBEDDING_DIMENSIONS."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
