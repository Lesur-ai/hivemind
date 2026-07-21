# -*- coding: utf-8 -*-
"""
P7-9 (#135) — vendored Graph Memory StorageService must mirror S3_SIGNATURE_MODE.

The P7-4 unified-token patch made `auth/s3_token_validator.py` mirror Hivemind's
``S3_SIGNATURE_MODE`` for the token-store read, but the SAME service's document
storage (`core/storage.py`) kept boto3 clients hardcoded to SigV2 for every data
operation. On a ``sigv4`` install (MinIO — no SigV2 support, AWS S3 — SigV2
deprecated; exactly the providers `.env.example` documents as requiring
``sigv4``) the embedded long runtime breaks twice:

- ``system_health`` → ``StorageService.test_connection()`` PUTs via the SigV2
  client → SignatureDoesNotMatch → GM reports unhealthy → Hivemind's
  ``long_push`` embedded-provision health probe fails
  ("Runtime long embarqué indisponible (health)");
- any ingest (``long_push`` / ``long_ingest`` apply) → ``upload_document()``
  → same failure at the FIRST pipeline step (``ingest_pipeline.py``).

The health-gate failure was reproduced end-to-end by the release-gate smoke
(``scripts/release_smoke.sh``) against the compose dev stack (MinIO +
``S3_SIGNATURE_MODE=sigv4``) during P7-9 validation; the ingest-path failure
follows from the same hardcoded client (``upload_document`` writes through
``self._client``, the SigV2 data client) and was first observed on a real
SigV4-only deployment (#135). Both paths go through the clients these tests
lock.

These tests are RED on the vendored baseline and GREEN with the #135 fix:

- behavioural: the data/metadata client wiring follows the mode (``dual``
  default byte-compatible with the Dell ECS baseline; ``sigv4`` routes every
  operation through the SigV4 client; unknown values fall back to ``dual``);
- mirror contract: ``StorageService._resolve_signature_mode`` matches the P7-4
  validator's ``_default_signature_mode`` for the same inputs (single source of
  truth — the shared ``S3_SIGNATURE_MODE`` env var);
- structural guard: NO vendored module may pass a hardcoded string
  ``signature_version`` to a botocore ``Config(...)`` without also reading
  ``S3_SIGNATURE_MODE`` in that same module.

Test strategy: the Hivemind test venv has boto3 but NOT neo4j/qdrant/openai, so
``mcp_memory.core.storage`` is imported directly (the P7-9 lazy ``core/__init__``
mirrors the P7-4 lazy ``auth/__init__`` for exactly this). Building boto3
clients is local — no network I/O happens in these tests.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GM_PKG = _REPO_ROOT / "services" / "graph-memory" / "src" / "mcp_memory"


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #

def _set_gm_env(monkeypatch, mode):
    """Set every env var the GM settings/storage import path needs.

    Importing ``mcp_memory.config`` executes a module-level ``Settings()``
    (required fields without defaults), so the credentials must be in the
    environment BEFORE the first import — this keeps every test in this file
    self-sufficient regardless of test ordering and of any local ``.env``
    file (explicit env vars take precedence in pydantic-settings).
    """
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://s3.test.invalid:9000")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("S3_REGION_NAME", "fr1")
    monkeypatch.setenv("LLMAAS_API_KEY", "test-llm-key")
    monkeypatch.setenv("NEO4J_PASSWORD", "test-neo4j-password")
    # StorageService.__init__ writes this var directly into os.environ
    # (vendored baseline side effect); pre-setting it through monkeypatch
    # makes the write reversible so the suite env stays hermetic.
    monkeypatch.setenv("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    if mode is None:
        monkeypatch.delenv("S3_SIGNATURE_MODE", raising=False)
    else:
        monkeypatch.setenv("S3_SIGNATURE_MODE", mode)


def _make_storage(monkeypatch, mode):
    """Build a real StorageService with deterministic env-only settings,
    clearing the ``get_settings`` lru_cache around the construction."""
    _set_gm_env(monkeypatch, mode)

    from mcp_memory.config import get_settings
    from mcp_memory.core.storage import StorageService

    get_settings.cache_clear()
    try:
        return StorageService()
    finally:
        get_settings.cache_clear()


def _sig(client) -> str:
    """The signature version a boto3 client will actually sign with."""
    return client.meta.config.signature_version


# --------------------------------------------------------------------------- #
# Behavioural — sigv4 mode (RED on the vendored baseline)                      #
# --------------------------------------------------------------------------- #

def test_sigv4_mode_routes_data_operations_through_sigv4(monkeypatch):
    """S3_SIGNATURE_MODE=sigv4 → the data client signs SigV4 (#135 core fix)."""
    svc = _make_storage(monkeypatch, "sigv4")
    assert svc.signature_mode == "sigv4"
    assert _sig(svc._client) == "s3v4"


def test_sigv4_mode_health_check_client_is_sigv4(monkeypatch):
    """The health path (test_connection uses _client_v2) must follow the mode.

    This is the exact failure surfaced by the release smoke: GM system_health
    PUTs a probe object through ``_client_v2``; a SigV2-signed probe against
    MinIO fails and the embedded runtime reports unhealthy, which blocks
    ``long_push`` before any ingest starts.
    """
    svc = _make_storage(monkeypatch, "sigv4")
    assert _sig(svc._client_v2) == "s3v4"
    assert svc._client_v2 is svc._client_v4


# --------------------------------------------------------------------------- #
# Behavioural — dual mode (baseline preservation, Dell ECS)                    #
# --------------------------------------------------------------------------- #

def test_default_mode_is_dual_and_preserves_baseline_wiring(monkeypatch):
    """No env → dual: SigV2 data client + SigV4 metadata client (baseline)."""
    svc = _make_storage(monkeypatch, None)
    assert svc.signature_mode == "dual"
    assert _sig(svc._client) == "s3"
    assert _sig(svc._client_v4) == "s3v4"
    assert svc._client_v2 is not svc._client_v4
    assert svc._client is svc._client_v2


def test_explicit_dual_matches_default(monkeypatch):
    svc = _make_storage(monkeypatch, "dual")
    assert svc.signature_mode == "dual"
    assert _sig(svc._client) == "s3"
    assert _sig(svc._client_v4) == "s3v4"


def test_unknown_mode_falls_back_to_dual(monkeypatch):
    """Unknown values fail safe to the legacy behavior, like the validator."""
    svc = _make_storage(monkeypatch, "sigv2-forever")
    assert svc.signature_mode == "dual"
    assert _sig(svc._client) == "s3"


def test_mode_is_normalized_like_the_validator(monkeypatch):
    """Whitespace/case are normalized (mirror of the validator contract)."""
    svc = _make_storage(monkeypatch, "  SIGV4  ")
    assert svc.signature_mode == "sigv4"
    assert _sig(svc._client) == "s3v4"


# --------------------------------------------------------------------------- #
# Mirror contract — storage and validator resolve the SAME mode                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw",
    [None, "dual", "sigv4", "SIGV4", "  dual  ", "bogus", ""],
)
def test_resolve_signature_mode_mirrors_validator_contract(monkeypatch, raw):
    """One env var, one contract: storage must resolve exactly like the P7-4
    validator (`S3TokenValidator._default_signature_mode`) for the same input,
    so document storage and token validation can never diverge on an install.
    """
    # Importing mcp_memory.core.storage pulls mcp_memory.config, whose
    # module-level Settings() needs the credential env vars on FIRST import —
    # set them so this test is order-independent (it may run standalone).
    _set_gm_env(monkeypatch, raw)

    from mcp_memory.auth.s3_token_validator import S3TokenValidator
    from mcp_memory.core.storage import StorageService

    assert (
        StorageService._resolve_signature_mode()
        == S3TokenValidator._default_signature_mode()
    )


# --------------------------------------------------------------------------- #
# Structural guard — no vendored hardcoded signature without the mode read     #
# --------------------------------------------------------------------------- #

def _config_signature_literal_calls(tree: ast.AST):
    """Yield string literals passed as ``signature_version=`` to Config(...)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute) else None
        )
        if name != "Config":
            continue
        for kw in node.keywords:
            if (
                kw.arg == "signature_version"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                yield kw.value.value


def _module_reads_signature_mode(tree: ast.AST) -> bool:
    """True when the module consults the shared S3_SIGNATURE_MODE env var.

    Requires the constant to appear as an ARGUMENT of an environment read
    (``os.getenv(...)`` / ``os.environ.get(...)`` / ``os.environ[...]``) —
    a docstring or comment-like constant mention does not count, so the
    guard cannot be satisfied without actually consulting the mode.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if name in ("getenv", "get") and any(
                isinstance(a, ast.Constant) and a.value == "S3_SIGNATURE_MODE"
                for a in node.args
            ):
                return True
        if isinstance(node, ast.Subscript):
            sl = node.slice
            if isinstance(sl, ast.Constant) and sl.value == "S3_SIGNATURE_MODE":
                return True
    return False


def test_no_vendored_boto3_client_hardcodes_signature_without_mode_read():
    """Topology guard (#135): a vendored module may only pass a literal
    ``signature_version`` to a botocore Config if that same module reads
    ``S3_SIGNATURE_MODE`` — i.e. the literal is one branch of the shared-mode
    logic, never an unconditional hardcode. RED on the vendored baseline
    (core/storage.py hardcoded SigV2 with no mode read).
    """
    offenders = []
    modules_with_literals = []
    for path in sorted(_GM_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        literals = list(_config_signature_literal_calls(tree))
        if not literals:
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        modules_with_literals.append(rel)
        if not _module_reads_signature_mode(tree):
            offenders.append(f"{rel}: hardcodes signature_version={literals}")

    # Non-vacuous: the storage module MUST be in scope of this guard.
    assert (
        "services/graph-memory/src/mcp_memory/core/storage.py"
        in modules_with_literals
    ), "guard lost its subject — storage.py no longer builds signed clients?"
    assert offenders == []
