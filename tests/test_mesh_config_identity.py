# -*- coding: utf-8 -*-
"""P10-2 security contract for Mesh configuration and local identity."""

from __future__ import annotations

import base64
import copy
import dataclasses
import fnmatch
import hashlib
import json
import logging
import os
import pickle
import shutil
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from pydantic import BaseModel, ConfigDict

from live_mem.config import Settings
from live_mem.mesh import cli as mesh_cli
from live_mem.mesh.config import (
    DEFAULT_MESH_BOOTSTRAP_MAX_BYTES,
    DEFAULT_MESH_BOOTSTRAP_MAX_OBJECTS,
    DEFAULT_MESH_CONTROL_MAX_BYTES,
    DEFAULT_MESH_INVITATION_TTL_SECONDS,
    MESH_BOOTSTRAP_MAX_BYTES_ENV,
    MESH_BOOTSTRAP_MAX_OBJECTS_ENV,
    MESH_CONTROL_MAX_BYTES_ENV,
    MESH_DISPLAY_NAME_ENV,
    MESH_ENABLED_ENV,
    MESH_INVITATION_TTL_ENV,
    MESH_PRIVATE_KEY_ENV,
    MESH_PUBLIC_URL_ENV,
    MeshConfigError,
    MeshEnabledConfig,
    is_mesh_enabled,
    load_mesh_config,
    load_mesh_environment,
)
from live_mem.mesh.identity import (
    LEGACY_MEMBERSHIP_PUBLIC_KEY_PREFIX,
    MESH_FINGERPRINT_PREFIX,
    MESH_PRIVATE_KEY_PREFIX,
    MESH_PUBLIC_KEY_PREFIX,
    MeshIdentityError,
    decode_mesh_public_key,
    decode_membership_public_key,
    generate_mesh_identity,
    mesh_identity_fingerprint,
    mesh_public_key_from_private,
    parse_mesh_private_key,
    parse_mesh_public_key,
)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _encoded_private(raw: bytes) -> str:
    return MESH_PRIVATE_KEY_PREFIX + _b64(raw)


def _valid_environment() -> dict[str, str]:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return {
        MESH_ENABLED_ENV: "true",
        MESH_PUBLIC_URL_ENV: "https://mesh.example.test",
        MESH_PRIVATE_KEY_ENV: _encoded_private(raw),
        MESH_DISPLAY_NAME_ENV: "Amsterdam peer",
    }


def _python_environment(**updates: str) -> dict[str, str]:
    environment = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment["PYTHONPATH"] = os.fspath(source_root)
    environment.update(updates)
    return environment


_MESH_ENV_NAMES = (
    MESH_ENABLED_ENV,
    MESH_PUBLIC_URL_ENV,
    MESH_PRIVATE_KEY_ENV,
    MESH_DISPLAY_NAME_ENV,
    MESH_INVITATION_TTL_ENV,
    MESH_CONTROL_MAX_BYTES_ENV,
    MESH_BOOTSTRAP_MAX_BYTES_ENV,
    MESH_BOOTSTRAP_MAX_OBJECTS_ENV,
)

_NON_LINUX_PROCESS_LOCK_TEST_SEAM = (
    "import sys; import live_mem.mesh.replay as mesh_replay; "
    "setattr(mesh_replay, '_require_process_lock_filesystem', lambda _fd: None) "
    "if sys.platform != 'linux' else None; "
)


def _python_environment_without_mesh(**updates: str) -> dict[str, str]:
    environment = _python_environment()
    for name in _MESH_ENV_NAMES:
        environment.pop(name, None)
    environment.update(updates)
    return environment


def _compose_service_blocks(compose_text: str) -> dict[str, str]:
    """Return top-level service blocks with a strict stdlib-only scan."""

    blocks: dict[str, list[str]] = {}
    current: str | None = None
    in_services = False
    for line in compose_text.splitlines():
        stripped = line.rstrip()
        if not in_services:
            if stripped == "services:":
                in_services = True
            continue
        if stripped and not line.startswith(" ") and not stripped.startswith("#"):
            break
        if line.startswith("  ") and not line.startswith("   "):
            candidate = line[2:].rstrip()
            if candidate and not candidate.startswith("#") and candidate.endswith(":"):
                current = candidate[:-1].strip()
                if not current or current in blocks:
                    raise AssertionError("invalid or duplicate Compose service")
                blocks[current] = [line]
                continue
        if current is not None:
            blocks[current].append(line)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


def _assert_mesh_private_key_compose_source_isolated(compose_text: str) -> None:
    blocks = _compose_service_blocks(compose_text)
    assert "hivemind" in blocks and "graph-memory" in blocks
    inherited = {
        name for name, block in blocks.items() if "    env_file: .env" in block
    }
    assert "hivemind" in inherited
    assert "HIVEMIND_MESH_PRIVATE_KEY=" not in blocks["hivemind"]
    for service in inherited - {"hivemind"}:
        assert (
            "      - HIVEMIND_MESH_PRIVATE_KEY=" in blocks[service]
        ), f"{service} inherits .env without masking the Mesh private key"


def _assert_mesh_secret_build_context_isolated(dockerignore_text: str) -> None:
    active_lines = [
        line.strip()
        for line in dockerignore_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not any(line.startswith("!") for line in active_lines), (
        "Dockerignore negation can re-include secrets or worktrees and is "
        "forbidden at the repository-root build boundary"
    )
    patterns = set(active_lines)
    required = {
        ".git",
        ".git/**",
        ".claude",
        ".claude/**",
        ".codex",
        ".codex/**",
        ".env",
        ".env.*",
        "**/.env",
        "**/.env.*",
        "*.key",
        "**/*.key",
        "*.pem",
        "**/*.pem",
        ".venv",
        "**/.venv",
        ".venv-tests",
        "**/.venv-tests",
        "venv",
        "**/venv",
        "env",
        "**/env",
        ".tox",
        "**/.tox",
        "__pycache__",
        "**/__pycache__",
        "**/*.py[cod]",
        ".pytest_cache",
        "**/.pytest_cache",
        ".mypy_cache",
        "**/.mypy_cache",
        ".ruff_cache",
        "**/.ruff_cache",
        ".coverage",
        ".coverage.*",
        "**/.coverage",
        "**/.coverage.*",
        "htmlcov",
        "**/htmlcov",
        "build",
        "**/build",
        "dist",
        "**/dist",
        "*.egg-info",
        "**/*.egg-info",
        "proof-artifacts",
        "**/proof-artifacts",
        "chantier",
        "**/chantier",
        "memory-bank",
        "**/memory-bank",
        ".mcp.json",
        "**/.mcp.json",
    }
    assert required.issubset(patterns)

    def excluded(path: str) -> bool:
        # Docker excludes descendants when a parent directory matches. Checking
        # every ancestor as well as the leaf models that rule for these canaries.
        pure = PurePosixPath(path)
        candidates = [path]
        candidates.extend(
            parent.as_posix()
            for parent in pure.parents
            if parent.as_posix() != "."
        )
        return any(
            fnmatch.fnmatchcase(candidate, pattern)
            for candidate in candidates
            for pattern in patterns
        )

    for secret_path in (
        ".env",
        ".env.production",
        "deploy/private/.env",
        "deploy/private/.env.production",
        "mesh-identity.key",
        "deploy/private/mesh-identity.key",
        "mesh-identity.pem",
        "deploy/private/mesh-identity.pem",
        ".git/config",
        ".claude/worktrees/review/private.txt",
        ".codex/cache/session.json",
        ".venv/lib/python/site.pyc",
        "services/graph-memory/.venv/lib/python/site.pyc",
        ".venv-tests/lib/python/site.pyc",
        "services/graph-memory/.venv-tests/lib/python/site.pyc",
        "venv/lib/python/site.pyc",
        "tools/venv/lib/python/site.pyc",
        "env/lib/python/site.pyc",
        "tools/env/lib/python/site.pyc",
        ".tox/py314/lib/python/site.pyc",
        "services/graph-memory/.tox/py314/lib/python/site.pyc",
        "__pycache__/config.cpython-314.pyc",
        "src/live_mem/__pycache__/server.cpython-314.pyc",
        ".pytest_cache/v/cache/nodeids",
        "tests/.pytest_cache/v/cache/nodeids",
        ".mypy_cache/3.14/cache.json",
        "src/.mypy_cache/3.14/cache.json",
        ".ruff_cache/content",
        "services/.ruff_cache/content",
        ".coverage",
        ".coverage.worker-1",
        "tests/.coverage",
        "tests/.coverage.worker-1",
        "htmlcov/index.html",
        "tests/htmlcov/index.html",
        "build/lib/live_mem/server.py",
        "services/graph-memory/build/lib/mcp_memory/server.py",
        "dist/hivemind.whl",
        "services/graph-memory/dist/graph-memory.whl",
        "src/hivemind.egg-info/PKG-INFO",
        "services/graph-memory/src/graph_memory.egg-info/PKG-INFO",
        "proof-artifacts/admin-console.png",
        "tests/proof-artifacts/proof-report.json",
        "chantier/strategie-produit-open-core-saas.md",
        "notes/chantier/private-plan.md",
        "memory-bank/activeContext.md",
        "services/graph-memory/memory-bank/progress.md",
        ".mcp.json",
        "tools/.mcp.json",
    ):
        assert excluded(secret_path), (
            f"{secret_path} would enter the Docker build context"
        )
    for shipped_path in (
        "Dockerfile",
        "src/live_mem/server.py",
        "services/graph-memory/src/server.py",
        "docs/key-management.md",
    ):
        assert not excluded(shipped_path), f"{shipped_path} is over-excluded"


def _assert_no_dockerfile_specific_dockerignore(paths: list[str]) -> None:
    overrides = sorted(
        path
        for path in paths
        if path.endswith(".dockerignore")
        and PurePosixPath(path).name != ".dockerignore"
    )
    assert not overrides, (
        "Dockerfile-specific ignore files override the root .dockerignore: "
        f"{overrides}"
    )


def _resolved_compose_with_mesh_canary(
    tmp_path: Path,
    compose_text: str,
) -> dict[str, object]:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(compose_text, encoding="utf-8")
    private_canary = "p10-2-fake-mesh-private-key-canary"
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "ADMIN_BOOTSTRAP_KEY=" + "A" * 32,
                "HIVEMIND_MESH_ENABLED=true",
                f"HIVEMIND_MESH_PRIVATE_KEY={private_canary}",
                "NEO4J_PASSWORD=p10-2-fake-neo4j-password",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("HIVEMIND_MESH_PRIVATE_KEY", None)
    environment["COMPOSE_PROJECT_NAME"] = "hivemind-p10-2-secret-isolation-test"
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            os.fspath(tmp_path),
            "-f",
            os.fspath(compose_file),
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    assert type(resolved) is dict
    services = resolved.get("services")
    assert type(services) is dict
    return {"canary": private_canary, "services": services}


def test_compose_and_build_context_isolate_mesh_private_key() -> None:
    repository = Path(__file__).resolve().parents[1]
    _assert_mesh_private_key_compose_source_isolated(
        (repository / "docker-compose.yml").read_text(encoding="utf-8")
    )
    _assert_mesh_secret_build_context_isolated(
        (repository / ".dockerignore").read_text(encoding="utf-8")
    )
    _assert_no_dockerfile_specific_dockerignore(
        [
            path.relative_to(repository).as_posix()
            for path in repository.rglob("*.dockerignore")
        ]
    )


def test_mutation_red_compose_mesh_private_key_override_removed() -> None:
    repository = Path(__file__).resolve().parents[1]
    compose = (repository / "docker-compose.yml").read_text(encoding="utf-8")
    override = "      - HIVEMIND_MESH_PRIVATE_KEY=\n"
    assert compose.count(override) == 1
    with pytest.raises(AssertionError):
        _assert_mesh_private_key_compose_source_isolated(
            compose.replace(override, "", 1)
        )


@pytest.mark.parametrize(
    "removed",
    [
        ".git",
        ".git/**",
        ".claude",
        ".claude/**",
        ".codex",
        ".codex/**",
        ".env",
        ".env.*",
        "**/.env",
        "**/.env.*",
        "*.key",
        "**/*.key",
        "*.pem",
        "**/*.pem",
        ".venv",
        "**/.venv",
        ".venv-tests",
        "**/.venv-tests",
        "venv",
        "**/venv",
        "env",
        "**/env",
        ".tox",
        "**/.tox",
        "__pycache__",
        "**/__pycache__",
        "**/*.py[cod]",
        ".pytest_cache",
        "**/.pytest_cache",
        ".mypy_cache",
        "**/.mypy_cache",
        ".ruff_cache",
        "**/.ruff_cache",
        ".coverage",
        ".coverage.*",
        "**/.coverage",
        "**/.coverage.*",
        "htmlcov",
        "**/htmlcov",
        "build",
        "**/build",
        "dist",
        "**/dist",
        "*.egg-info",
        "**/*.egg-info",
        "proof-artifacts",
        "**/proof-artifacts",
        "chantier",
        "**/chantier",
        "memory-bank",
        "**/memory-bank",
        ".mcp.json",
        "**/.mcp.json",
    ],
)
def test_mutation_red_mesh_secret_dockerignore_pattern_removed(removed: str) -> None:
    repository = Path(__file__).resolve().parents[1]
    dockerignore = (repository / ".dockerignore").read_text(encoding="utf-8")
    line = removed + "\n"
    physical_lines = dockerignore.splitlines(keepends=True)
    assert physical_lines.count(line) == 1
    physical_lines.remove(line)
    with pytest.raises(AssertionError):
        _assert_mesh_secret_build_context_isolated("".join(physical_lines))


def test_mutation_red_mesh_secret_dockerignore_negation_added() -> None:
    repository = Path(__file__).resolve().parents[1]
    dockerignore = (repository / ".dockerignore").read_text(encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_mesh_secret_build_context_isolated(
            dockerignore + "\n!deploy/private/.env\n"
        )


def test_mutation_red_dockerfile_specific_dockerignore_added() -> None:
    with pytest.raises(AssertionError):
        _assert_no_dockerfile_specific_dockerignore(
            [
                ".dockerignore",
                "services/graph-memory/Dockerfile.dockerignore",
            ]
        )


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_real_docker_compose_config_keeps_mesh_private_key_only_in_hivemind(
    tmp_path: Path,
) -> None:
    version = subprocess.run(
        ["docker", "compose", "version"],
        text=True,
        capture_output=True,
        check=False,
    )
    if version.returncode != 0:
        pytest.skip("Docker Compose unavailable")

    repository = Path(__file__).resolve().parents[1]
    resolved = _resolved_compose_with_mesh_canary(
        tmp_path,
        (repository / "docker-compose.yml").read_text(encoding="utf-8"),
    )
    canary = resolved["canary"]
    services = resolved["services"]
    assert type(canary) is str and type(services) is dict
    hivemind_environment = services["hivemind"].get("environment")
    graph_environment = services["graph-memory"].get("environment")
    assert type(hivemind_environment) is dict
    assert type(graph_environment) is dict
    assert hivemind_environment[MESH_PRIVATE_KEY_ENV] == canary
    assert graph_environment[MESH_PRIVATE_KEY_ENV] == ""
    assert json.dumps(services, sort_keys=True).count(canary) == 1
    for service_name, service in services.items():
        if service_name != "hivemind":
            assert canary not in json.dumps(service, sort_keys=True)


def test_generated_identity_uses_exact_v1_encodings_and_fingerprint() -> None:
    identity = generate_mesh_identity()

    assert identity.public_key.startswith(MESH_PUBLIC_KEY_PREFIX)
    assert len(identity.public_key.removeprefix(MESH_PUBLIC_KEY_PREFIX)) == 43
    assert "=" not in identity.public_key
    assert identity.fingerprint.startswith(MESH_FINGERPRINT_PREFIX)
    assert len(identity.fingerprint.removeprefix(MESH_FINGERPRINT_PREFIX)) == 64
    assert identity.fingerprint.removeprefix(MESH_FINGERPRINT_PREFIX).islower()
    assert mesh_identity_fingerprint(identity.public_key) == identity.fingerprint


def test_fingerprint_known_vector_has_domain_separation() -> None:
    raw_public = bytes(range(32))
    encoded = MESH_PUBLIC_KEY_PREFIX + _b64(raw_public)
    expected = hashlib.sha256(b"hivemind-mesh-identity-v1\0" + raw_public).hexdigest()

    assert mesh_identity_fingerprint(encoded) == f"hm1:{expected}"


def test_private_round_trip_derives_public_key_and_signs() -> None:
    cryptography_key = Ed25519PrivateKey.generate()
    private_raw = cryptography_key.private_bytes(
        Encoding.Raw,
        PrivateFormat.Raw,
        NoEncryption(),
    )
    opaque = parse_mesh_private_key(_encoded_private(private_raw))
    public = mesh_public_key_from_private(opaque)
    payload = b"signed without exporting private material"

    parse_mesh_public_key(public).verify(opaque.sign(payload), payload)
    assert decode_membership_public_key(public) == cryptography_key.public_key().public_bytes(
        Encoding.Raw,
        PublicFormat.Raw,
    )


def test_membership_decoder_accepts_only_canonical_legacy_or_v1_public() -> None:
    raw = bytes(range(32))
    payload = _b64(raw)

    assert (
        decode_membership_public_key(LEGACY_MEMBERSHIP_PUBLIC_KEY_PREFIX + payload)
        == raw
    )
    assert decode_membership_public_key(MESH_PUBLIC_KEY_PREFIX + payload) == raw
    assert decode_mesh_public_key(MESH_PUBLIC_KEY_PREFIX + payload) == raw

    rejected = [
        payload,
        MESH_PRIVATE_KEY_PREFIX + payload,
        LEGACY_MEMBERSHIP_PUBLIC_KEY_PREFIX + payload + "=",
        MESH_PUBLIC_KEY_PREFIX + payload + "=",
        "ED25519:" + payload,
        MESH_PUBLIC_KEY_PREFIX + payload[:-1],
    ]
    for value in rejected:
        with pytest.raises(MeshIdentityError):
            decode_membership_public_key(value)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value + "=",
        lambda value: value + " ",
        lambda value: value.replace("ed25519-public:v1:", "ed25519:"),
        lambda value: value.replace("ed25519-public:v1:", "ED25519-public:v1:"),
        lambda value: value[:-1],
        lambda value: value + "A",
    ],
)
def test_mesh_public_parser_rejects_noncanonical_encodings(mutator) -> None:
    public = generate_mesh_identity().public_key
    with pytest.raises(MeshIdentityError, match="invalid Mesh public key encoding"):
        parse_mesh_public_key(mutator(public))


def test_private_parser_rejects_wrong_prefix_padding_length_and_type() -> None:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    valid = _encoded_private(raw)
    invalid = [
        valid.replace(MESH_PRIVATE_KEY_PREFIX, MESH_PUBLIC_KEY_PREFIX),
        valid + "=",
        valid[:-1],
        valid + "A",
        None,
        b"not-a-string",
    ]
    for value in invalid:
        with pytest.raises(MeshIdentityError):
            parse_mesh_private_key(value)


def test_private_key_is_redacted_non_pydantic_noncopyable_and_nonserializable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    encoded = _encoded_private(raw)
    private_key = parse_mesh_private_key(encoded)

    assert encoded not in repr(private_key)
    assert encoded not in str(private_key)
    assert "redacted" in repr(private_key)
    assert not hasattr(private_key, "__dict__")
    assert not hasattr(private_key, "model_dump")
    assert not hasattr(private_key, "model_dump_json")
    assert not hasattr(private_key, "to_bytes")
    assert not hasattr(private_key, "to_string")

    for operation in (
        lambda: copy.copy(private_key),
        lambda: copy.deepcopy(private_key),
        lambda: pickle.dumps(private_key),
        lambda: json.dumps(private_key),
    ):
        with pytest.raises(TypeError) as raised:
            operation()
        assert encoded not in str(raised.value)

    caplog.set_level(logging.INFO)
    logging.getLogger("mesh-test").info("key repr=%r str=%s", private_key, private_key)
    assert encoded not in caplog.text
    assert "redacted" in caplog.text

    identity = generate_mesh_identity()
    with pytest.raises(TypeError):
        copy.copy(identity)
    with pytest.raises(TypeError):
        copy.deepcopy(identity)
    with pytest.raises(TypeError):
        pickle.dumps(identity)


def test_pydantic_cannot_json_serialize_opaque_private_key() -> None:
    class Carrier(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        private_key: object

    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    encoded = _encoded_private(raw)
    private_key = parse_mesh_private_key(encoded)
    carrier = Carrier(private_key=private_key)
    dumped = carrier.model_dump()

    assert dumped["private_key"] is private_key
    assert encoded not in repr(dumped)
    with pytest.raises(Exception) as raised:
        carrier.model_dump_json()
    assert encoded not in str(raised.value)


@pytest.mark.parametrize("value, expected", [(None, True), ("false", False), ("true", True)])
def test_enabled_flag_is_strict_and_defaults_enabled(value, expected) -> None:
    environment = {} if value is None else {MESH_ENABLED_ENV: value}
    assert is_mesh_enabled(environment) is expected


def test_settings_default_enables_mesh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MESH_ENABLED_ENV, raising=False)
    assert Settings(_env_file=None).hivemind_mesh_enabled == "true"


@pytest.mark.parametrize(
    "value",
    ["", "TRUE", "False", "1", "0", "yes", " true", "false ", True, 1],
)
def test_enabled_flag_rejects_every_noncanonical_value(value) -> None:
    with pytest.raises(MeshConfigError, match="exactly 'true' or 'false'"):
        is_mesh_enabled({MESH_ENABLED_ENV: value})  # type: ignore[dict-item]


def test_disabled_config_ignores_all_non_gate_mesh_values() -> None:
    environment = {
        MESH_ENABLED_ENV: "false",
        MESH_PUBLIC_URL_ENV: "not a URL",
        MESH_PRIVATE_KEY_ENV: "a secret that must not be parsed",
        MESH_DISPLAY_NAME_ENV: "\x00",
        MESH_INVITATION_TTL_ENV: "invalid",
        MESH_CONTROL_MAX_BYTES_ENV: "999999999999",
        MESH_BOOTSTRAP_MAX_BYTES_ENV: "0",
        MESH_BOOTSTRAP_MAX_OBJECTS_ENV: "-1",
    }

    assert load_mesh_config(environment) is None


def test_default_enabled_config_fails_closed_without_identity() -> None:
    with pytest.raises(MeshConfigError, match=MESH_PUBLIC_URL_ENV):
        load_mesh_config({})


def test_mesh_dotenv_loader_reads_only_mesh_values_with_process_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _valid_environment()
    private_value = values[MESH_PRIVATE_KEY_ENV]
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                *(f"{name}={value}" for name, value in values.items()),
                "ADMIN_BOOTSTRAP_KEY=must-not-enter-mesh-loader",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    for name in _MESH_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(MESH_DISPLAY_NAME_ENV, "Environment wins")
    monkeypatch.chdir(tmp_path)

    loaded = load_mesh_environment()

    assert loaded[MESH_ENABLED_ENV] == "true"
    assert loaded[MESH_PRIVATE_KEY_ENV] == private_value
    assert loaded[MESH_DISPLAY_NAME_ENV] == "Environment wins"
    assert "ADMIN_BOOTSTRAP_KEY" not in loaded
    assert set(loaded).issubset(_MESH_ENV_NAMES)


def test_enabled_config_is_lazy_immutable_non_pydantic_and_uses_frozen_defaults() -> None:
    environment = _valid_environment()
    encoded = environment[MESH_PRIVATE_KEY_ENV]
    config = load_mesh_config(environment)

    assert isinstance(config, MeshEnabledConfig)
    assert config.enabled is True
    assert config.public_url == "https://mesh.example.test"
    assert config.display_name == "Amsterdam peer"
    assert config.invitation_ttl_seconds == DEFAULT_MESH_INVITATION_TTL_SECONDS
    assert config.control_max_bytes == DEFAULT_MESH_CONTROL_MAX_BYTES
    assert config.bootstrap_max_bytes == DEFAULT_MESH_BOOTSTRAP_MAX_BYTES
    assert config.bootstrap_max_objects == DEFAULT_MESH_BOOTSTRAP_MAX_OBJECTS
    assert config.fingerprint == mesh_identity_fingerprint(config.public_key)
    assert encoded not in repr(config)
    assert not hasattr(config, "model_dump")
    assert not hasattr(config, "model_dump_json")

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.display_name = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        dataclasses.asdict(config)
    with pytest.raises(TypeError):
        copy.copy(config)
    with pytest.raises(TypeError):
        copy.deepcopy(config)
    with pytest.raises(TypeError):
        pickle.dumps(config)


@pytest.mark.parametrize(
    "missing",
    [MESH_PUBLIC_URL_ENV, MESH_PRIVATE_KEY_ENV, MESH_DISPLAY_NAME_ENV],
)
@pytest.mark.parametrize("replacement", [None, "", " ", " leading", "trailing "])
def test_enabled_config_requires_canonical_identity_fields(missing, replacement) -> None:
    environment = _valid_environment()
    if replacement is None:
        environment.pop(missing)
    else:
        environment[missing] = replacement

    with pytest.raises(MeshConfigError) as raised:
        load_mesh_config(environment)
    private_value = environment.get(MESH_PRIVATE_KEY_ENV, "")
    if private_value.strip():
        assert private_value not in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://mesh.example.test",
        "https://user@mesh.example.test",
        "https://user:pass@mesh.example.test",
        "https://mesh.example.test/path",
        "https://mesh.example.test?query=1",
        "https://mesh.example.test#fragment",
        "https://",
        "HTTPS://mesh.example.test",
        "https://mesh.example.test:99999",
        " https://mesh.example.test",
        "https://mesh.example.test\\attacker",
        "https://mesh.example.test\n.attacker",
        "https://Mesh.example.test",
        "https://mesh.example.test.",
        "https://méshe.example.test",
        "https://mesh%2eexample.test",
        "https://127.0.0.1",
        "https://169.254.169.254",
        "https://[::1]",
    ],
)
def test_enabled_config_rejects_non_origin_or_non_https_public_url(url) -> None:
    environment = _valid_environment()
    environment[MESH_PUBLIC_URL_ENV] = url
    with pytest.raises(MeshConfigError):
        load_mesh_config(environment)


@pytest.mark.parametrize(
    "name",
    [
        "peer\x00name",
        "peer\nname",
        "peer\x7fname",
        "peer\x85name",
        "peer\u202ename",
        "peer\u2028name",
        "Cafe\u0301",
        "x" * 129,
        "é" * 65,
    ],
)
def test_enabled_config_rejects_unsafe_or_unbounded_display_name(name) -> None:
    environment = _valid_environment()
    environment[MESH_DISPLAY_NAME_ENV] = name
    with pytest.raises(MeshConfigError):
        load_mesh_config(environment)


@pytest.mark.parametrize("name", ["x" * 128, "é" * 64, "Pair Amsterdam 🐝"])
def test_enabled_config_accepts_bounded_nfc_display_name(name: str) -> None:
    environment = _valid_environment()
    environment[MESH_DISPLAY_NAME_ENV] = name
    config = load_mesh_config(environment)
    assert config is not None
    assert config.display_name == name


@pytest.mark.parametrize("ttl", ["1", "3599", "3601", "03600", "+3600", "3600 "])
def test_invitation_ttl_is_exactly_3600(ttl) -> None:
    environment = _valid_environment()
    environment[MESH_INVITATION_TTL_ENV] = ttl
    with pytest.raises(MeshConfigError):
        load_mesh_config(environment)


@pytest.mark.parametrize(
    "name, maximum",
    [
        (MESH_CONTROL_MAX_BYTES_ENV, DEFAULT_MESH_CONTROL_MAX_BYTES),
        (MESH_BOOTSTRAP_MAX_BYTES_ENV, DEFAULT_MESH_BOOTSTRAP_MAX_BYTES),
        (MESH_BOOTSTRAP_MAX_OBJECTS_ENV, DEFAULT_MESH_BOOTSTRAP_MAX_OBJECTS),
    ],
)
def test_enabled_config_integer_boundaries_are_closed(name, maximum) -> None:
    for accepted in (1, maximum):
        environment = _valid_environment()
        environment[name] = str(accepted)
        assert load_mesh_config(environment) is not None

    for rejected in ("0", str(maximum + 1), "01", "+1", "1 ", "1.0", "-1"):
        environment = _valid_environment()
        environment[name] = rejected
        with pytest.raises(MeshConfigError):
            load_mesh_config(environment)


def test_invalid_private_key_never_appears_in_exception_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    environment = _valid_environment()
    canary = MESH_PRIVATE_KEY_PREFIX + "S" * 43
    environment[MESH_PRIVATE_KEY_ENV] = canary
    caplog.set_level(logging.DEBUG)

    with pytest.raises(MeshConfigError) as raised:
        load_mesh_config(environment)
    logging.getLogger("mesh-test").exception("safe config failure", exc_info=raised.value)

    assert canary not in str(raised.value)
    assert canary not in caplog.text


def test_key_file_is_new_regular_single_link_0600_and_contains_only_secret(
    tmp_path: Path,
) -> None:
    output = tmp_path / "mesh.key"
    result = mesh_cli.write_mesh_identity_file(output)
    encoded = output.read_text(encoding="ascii")
    metadata = output.stat()

    assert encoded.startswith(MESH_PRIVATE_KEY_PREFIX)
    assert "\n" not in encoded
    assert result.public_key == mesh_public_key_from_private(parse_mesh_private_key(encoded))
    assert result.fingerprint == mesh_identity_fingerprint(result.public_key)
    assert result.path == os.fspath(output)
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1


def test_keygen_refuses_existing_file_directory_fifo_and_symlink(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.write_text("do-not-replace", encoding="utf-8")
    directory = tmp_path / "directory"
    directory.mkdir()
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    target = tmp_path / "target"
    target.write_text("do-not-touch", encoding="utf-8")
    symlink = tmp_path / "symlink"
    symlink.symlink_to(target)

    for path in (existing, directory, fifo, symlink):
        with pytest.raises(mesh_cli.MeshKeygenError, match="refusing to replace"):
            mesh_cli.write_mesh_identity_file(path)

    assert existing.read_text(encoding="utf-8") == "do-not-replace"
    assert target.read_text(encoding="utf-8") == "do-not-touch"
    assert symlink.is_symlink()


def test_created_file_validator_rejects_bad_mode_and_multiple_links(tmp_path: Path) -> None:
    path = tmp_path / "key"
    path.touch(mode=0o644)
    fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(mesh_cli.MeshKeygenError, match="mode must be 0600"):
            mesh_cli._validate_created_file(fd)
        os.fchmod(fd, 0o600)
        os.link(path, tmp_path / "second-link")
        with pytest.raises(mesh_cli.MeshKeygenError, match="exactly one link"):
            mesh_cli._validate_created_file(fd)
    finally:
        os.close(fd)


def test_keygen_cleans_up_created_path_on_caught_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "mesh.key"

    def fail_write(fd: int, data: object) -> int:
        del fd, data
        raise OSError("injected write failure")

    monkeypatch.setattr(mesh_cli.os, "write", fail_write)
    with pytest.raises(mesh_cli.MeshKeygenError, match="OSError") as raised:
        mesh_cli.write_mesh_identity_file(output)

    assert not output.exists()
    assert MESH_PRIVATE_KEY_PREFIX not in str(raised.value)


def test_keygen_cleans_up_created_path_on_mode_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "mesh.key"

    def fail_chmod(fd: int, mode: int) -> None:
        del fd, mode
        raise OSError("injected chmod failure")

    monkeypatch.setattr(mesh_cli.os, "fchmod", fail_chmod)
    with pytest.raises(mesh_cli.MeshKeygenError, match="OSError"):
        mesh_cli.write_mesh_identity_file(output)

    assert not output.exists()


def test_keygen_fsyncs_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synced_modes: list[int] = []
    real_fsync = mesh_cli.os.fsync

    def record_fsync(fd: int) -> None:
        synced_modes.append(mesh_cli.os.fstat(fd).st_mode)
        real_fsync(fd)

    monkeypatch.setattr(mesh_cli.os, "fsync", record_fsync)
    mesh_cli.write_mesh_identity_file(tmp_path / "mesh.key")

    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)


def test_concurrent_keygen_collision_has_exactly_one_winner(tmp_path: Path) -> None:
    output = tmp_path / "mesh.key"

    def attempt() -> mesh_cli.MeshKeygenResult | mesh_cli.MeshKeygenError:
        try:
            return mesh_cli.write_mesh_identity_file(output)
        except mesh_cli.MeshKeygenError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(32)))

    winners = [item for item in outcomes if isinstance(item, mesh_cli.MeshKeygenResult)]
    losers = [item for item in outcomes if isinstance(item, mesh_cli.MeshKeygenError)]
    assert len(winners) == 1
    assert len(losers) == 31
    encoded = output.read_text(encoding="ascii")
    assert mesh_public_key_from_private(parse_mesh_private_key(encoded)) == winners[0].public_key
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.stat().st_nlink == 1


def test_module_and_keygen_dispatch_do_not_import_server_or_mesh_config(
    tmp_path: Path,
) -> None:
    import_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import live_mem.mesh; import live_mem.__main__; "
                "assert 'live_mem.server' not in sys.modules; "
                "assert 'live_mem.mesh.config' not in sys.modules"
            ),
        ],
        cwd=tmp_path,
        env=_python_environment(HIVEMIND_MESH_ENABLED="invalid-on-purpose"),
        text=True,
        capture_output=True,
        check=False,
    )
    assert import_probe.returncode == 0, import_probe.stderr

    output = tmp_path / "mesh.key"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "live_mem",
            "mesh-keygen",
            "--output",
            os.fspath(output),
        ],
        cwd=tmp_path,
        env=_python_environment(HIVEMIND_MESH_ENABLED="invalid-on-purpose"),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    secret = output.read_text(encoding="ascii")
    assert secret not in result.stdout
    assert secret not in result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith(f"public_key={MESH_PUBLIC_KEY_PREFIX}")
    assert lines[1].startswith(f"fingerprint={MESH_FINGERPRINT_PREFIX}")
    assert lines[2] == f"path={output}"


def test_config_import_reads_only_the_strict_enabled_gate_eagerly(tmp_path: Path) -> None:
    lazy_enabled = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import live_mem.mesh.config as c; "
                "assert c.MESH_ENABLED is True; print('ok')"
            ),
        ],
        cwd=tmp_path,
        env=_python_environment(
            HIVEMIND_MESH_ENABLED="true",
            HIVEMIND_MESH_PRIVATE_KEY="invalid-but-lazy",
            HIVEMIND_MESH_PUBLIC_URL="invalid-but-lazy",
            HIVEMIND_MESH_DISPLAY_NAME="",
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert lazy_enabled.returncode == 0, lazy_enabled.stderr
    assert lazy_enabled.stdout == "ok\n"

    invalid_gate = subprocess.run(
        [sys.executable, "-c", "import live_mem.mesh.config"],
        cwd=tmp_path,
        env=_python_environment(HIVEMIND_MESH_ENABLED="TRUE"),
        text=True,
        capture_output=True,
        check=False,
    )
    assert invalid_gate.returncode != 0
    assert "must be exactly 'true' or 'false'" in invalid_gate.stderr


def test_server_disabled_mode_does_not_import_or_create_mesh_state(
    tmp_path: Path,
) -> None:
    secure_parent = tmp_path / "local-secrets"
    secure_parent.mkdir(mode=0o700)
    secure_parent.chmod(0o700)
    token_path = secure_parent / "long-token"
    environment = _python_environment(
        ADMIN_BOOTSTRAP_KEY="A" * 32,
        HIVEMIND_MESH_ENABLED="false",
        HIVEMIND_MESH_PRIVATE_KEY="invalid-but-ignored",
        HIVEMIND_MESH_PUBLIC_URL="invalid-but-ignored",
        HIVEMIND_MESH_DISPLAY_NAME="",
        LONG_EMBEDDED_TOKEN_FILE=os.fspath(token_path),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from live_mem.server import create_app; "
                "create_app(); assert 'live_mem.mesh' not in sys.modules; "
                "print('ok')"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"
    assert not (secure_parent / "mesh-process-locks").exists()


def test_server_default_mode_fails_closed_without_mesh_identity(tmp_path: Path) -> None:
    environment = _python_environment_without_mesh(
        ADMIN_BOOTSTRAP_KEY="A" * 32,
        LONG_EMBEDDED_TOKEN_FILE=os.fspath(tmp_path / "long-token"),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from live_mem.server import create_app; create_app()",
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert f"{MESH_PUBLIC_URL_ENV} is required" in result.stderr
    assert MESH_PRIVATE_KEY_ENV not in result.stderr


def test_server_enabled_mode_acquires_secure_process_identity_lock(
    tmp_path: Path,
) -> None:
    secure_parent = tmp_path / "local-secrets"
    secure_parent.mkdir(mode=0o700)
    secure_parent.chmod(0o700)
    token_path = secure_parent / "long-token"
    environment = _python_environment(
        ADMIN_BOOTSTRAP_KEY="A" * 32,
        LONG_EMBEDDED_TOKEN_FILE=os.fspath(token_path),
    )
    # Restore the exact enabled values after copying the ambient environment so
    # a developer shell cannot override the subprocess contract.
    environment.update(_valid_environment())
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                _NON_LINUX_PROCESS_LOCK_TEST_SEAM
                + "from live_mem.server import create_app, mcp; create_app(); "
                "assert all(not tool.name.startswith('mesh_') for tool in "
                "mcp._tool_manager.list_tools()); print('ok')"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"
    lock_directory = secure_parent / "mesh-process-locks"
    entries = list(lock_directory.iterdir())
    assert stat.S_IMODE(lock_directory.stat().st_mode) == 0o700
    assert len(entries) == 1
    assert entries[0].name.endswith(".lock")
    assert stat.S_IMODE(entries[0].stat().st_mode) == 0o600
    assert entries[0].stat().st_nlink == 1


def test_server_enabled_mode_loads_mesh_secret_lazily_from_dotenv(
    tmp_path: Path,
) -> None:
    secure_parent = tmp_path / "local-secrets"
    secure_parent.mkdir(mode=0o700)
    secure_parent.chmod(0o700)
    values = _valid_environment()
    private_value = values[MESH_PRIVATE_KEY_ENV]
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "ADMIN_BOOTSTRAP_KEY=" + "A" * 32,
                f"LONG_EMBEDDED_TOKEN_FILE={secure_parent / 'long-token'}",
                *(f"{name}={value}" for name, value in values.items()),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    environment = _python_environment_without_mesh()
    environment.pop("ADMIN_BOOTSTRAP_KEY", None)
    environment.pop("LONG_EMBEDDED_TOKEN_FILE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                _NON_LINUX_PROCESS_LOCK_TEST_SEAM
                + "from live_mem.server import create_app, settings; "
                "create_app(); "
                "assert settings.hivemind_mesh_enabled == 'true'; "
                "assert not hasattr(settings, 'hivemind_mesh_private_key'); "
                "print('ok')"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"
    assert private_value not in result.stdout + result.stderr
    assert (secure_parent / "mesh-process-locks").is_dir()


def test_process_environment_mesh_gate_overrides_dotenv_without_loading_secret(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "ADMIN_BOOTSTRAP_KEY=" + "A" * 32,
                "HIVEMIND_MESH_ENABLED=true",
                "HIVEMIND_MESH_PRIVATE_KEY=invalid-but-must-remain-lazy",
                "HIVEMIND_MESH_PUBLIC_URL=invalid-but-must-remain-lazy",
                "HIVEMIND_MESH_DISPLAY_NAME=",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    environment = _python_environment_without_mesh(HIVEMIND_MESH_ENABLED="false")
    environment.pop("ADMIN_BOOTSTRAP_KEY", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from live_mem.server import create_app, settings; "
                "create_app(); assert settings.hivemind_mesh_enabled == 'false'; "
                "assert 'live_mem.mesh' not in sys.modules; print('ok')"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"
    assert "invalid-but-must-remain-lazy" not in result.stdout + result.stderr


def test_invalid_mesh_gate_in_dotenv_fails_closed_without_secret_disclosure(
    tmp_path: Path,
) -> None:
    private_canary = MESH_PRIVATE_KEY_PREFIX + "A" * 43
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "ADMIN_BOOTSTRAP_KEY=" + "A" * 32,
                "HIVEMIND_MESH_ENABLED=TRUE",
                f"HIVEMIND_MESH_PRIVATE_KEY={private_canary}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-c", "import live_mem.server"],
        cwd=tmp_path,
        env=_python_environment_without_mesh(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must be exactly 'true' or 'false'" in result.stderr
    assert private_canary not in result.stdout + result.stderr


def test_cli_existing_path_failure_does_not_disclose_existing_or_generated_secret(
    tmp_path: Path,
) -> None:
    output = tmp_path / "mesh.key"
    canary = MESH_PRIVATE_KEY_PREFIX + "A" * 43
    output.write_text(canary, encoding="ascii")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "live_mem",
            "mesh-keygen",
            "--output",
            os.fspath(output),
        ],
        cwd=tmp_path,
        env=_python_environment(HIVEMIND_MESH_ENABLED="false"),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert output.read_text(encoding="ascii") == canary
    assert canary not in result.stdout
    assert canary not in result.stderr
    assert MESH_PRIVATE_KEY_PREFIX not in result.stdout + result.stderr
