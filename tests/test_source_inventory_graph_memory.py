# -*- coding: utf-8 -*-
"""
P7-1 (EPIC #115 / issue #117) — vendored Graph Memory source-inventory gate.

P7-1 vendors the embedded ``long`` runtime (Graph Memory) into
``services/graph-memory/`` at a pinned upstream commit (ADR-0019). This module
LOCKS the invariants of that import so a regression — a missing runtime file, a
re-introduced submodule, a committed secret, or a dragged-in ``.git`` / cache —
goes RED.

It is a PURE pathlib gate: no ``live_mem`` import, no ``mcp_memory`` import, no
boto3 / network / S3 / LLM / Docker. The repo root is resolved from ``__file__``
so the gate runs from any working directory (agent threads reset cwd between
calls).

Build-in-isolation (``docker build services/graph-memory``) is verified manually
/ in CI (P7-7), not here — this gate stays offline and deterministic.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SVC = _REPO_ROOT / "services" / "graph-memory"
_NOTICES = _REPO_ROOT / "THIRD_PARTY_NOTICES.md"

# Pinned upstream provenance (must match THIRD_PARTY_NOTICES.md).
_UPSTREAM_URL = "https://github.com/cloud-temple/graph-memory"
_PINNED_SHA = "ae9afb0b95d449b68a8fb3ca3e70674b8f26eeb8"


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _git_tracked_under_svc() -> list[str]:
    """Return the repo-relative paths git TRACKS under services/graph-memory/.

    The hygiene guards below check what is COMMITTED (git-tracked), not what
    happens to be on disk: importing the vendored package during a test run
    (e.g. via tests/conftest.py) legitimately creates gitignored
    ``__pycache__/*.pyc`` that must never fail this gate — they are never
    committed.
    """
    out = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "services/graph-memory"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


# --------------------------------------------------------------------------- #
# 1. Required runtime files exist and are non-empty                            #
# --------------------------------------------------------------------------- #


def test_required_runtime_files_present():
    required = [
        _SVC / "Dockerfile",
        _SVC / "requirements.lock",
        _SVC / "requirements.txt",
        _SVC / "VERSION",
        _SVC / "LICENSE",
        _SVC / "src" / "mcp_memory" / "__init__.py",
        _SVC / "src" / "mcp_memory" / "server.py",
        _SVC / "src" / "mcp_memory" / "config.py",
    ]
    missing = [str(p.relative_to(_REPO_ROOT)) for p in required if not _nonempty(p)]
    assert not missing, f"vendored runtime is missing required files: {missing}"


def test_ontologies_dir_has_yaml():
    ont = _SVC / "ONTOLOGIES"
    assert ont.is_dir(), "services/graph-memory/ONTOLOGIES/ must exist"
    yamls = list(ont.glob("*.yaml")) + list(ont.glob("*.yml"))
    assert yamls, "ONTOLOGIES/ must contain at least one ontology YAML file"


def test_requirements_declare_datastore_clients():
    """The embedded runtime needs Neo4j + Qdrant clients and boto3 (the boto3
    dep is what makes the P7-4 unified-token S3 read feasible)."""
    reqs = (_SVC / "requirements.txt").read_text(encoding="utf-8").lower()
    for pkg in ("neo4j", "qdrant-client", "boto3"):
        assert pkg in reqs, (
            f"vendored requirements.txt must declare {pkg!r} "
            "(embedded long runtime datastore/storage client)"
        )


def _mutable_requirement_lines(text: str) -> list[str]:
    active = [
        line.split("#", 1)[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    exact_pin = re.compile(
        r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?=="
        r"[A-Za-z0-9_.+!-]+(?:\s*;\s*.+)?$"
    )
    return [line for line in active if not exact_pin.fullmatch(line)]


def test_graph_runtime_direct_dependencies_are_exact_pins():
    """Exported builds must not silently cross a direct dependency release."""
    requirements = (_SVC / "requirements.txt").read_text(encoding="utf-8")
    assert not _mutable_requirement_lines(requirements)

    mutated = requirements.replace("mcp==1.28.1", "mcp>=1.8.0", 1)
    assert _mutable_requirement_lines(mutated) == ["mcp>=1.8.0"]


def test_graph_runtime_transitives_are_hash_locked_and_installed_fail_closed():
    """The container must install the complete resolution with hashes only."""
    lock = (_SVC / "requirements.lock").read_text(encoding="utf-8")
    assert lock.count("--hash=sha256:") >= 79
    requirement_starts = [
        line for line in lock.splitlines() if line and not line[0].isspace()
    ]
    assert requirement_starts
    assert all("==" in line for line in requirement_starts)

    dockerfile = (_SVC / "Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes -r requirements.lock" in dockerfile
    assert "pip install --no-cache-dir --upgrade pip" not in dockerfile
    assert "apt-get" not in dockerfile

    mutated = dockerfile.replace("--require-hashes ", "", 1)
    assert "--require-hashes -r requirements.lock" not in mutated


def test_server_entrypoint_module_importable_layout():
    """The Dockerfile ENTRYPOINT is ``python -m src.mcp_memory.server``; the
    package layout under src/ must back that entrypoint."""
    assert _nonempty(_SVC / "src" / "__init__.py") or (
        _SVC / "src"
    ).is_dir(), "services/graph-memory/src/ must exist for the module entrypoint"
    assert _nonempty(_SVC / "src" / "mcp_memory" / "server.py"), (
        "src/mcp_memory/server.py must exist (the -m entrypoint target)"
    )


# --------------------------------------------------------------------------- #
# 2. Import hygiene — no submodule, no .git, no secret, no cache               #
# --------------------------------------------------------------------------- #


def test_no_submodule_reference():
    assert not (_SVC / ".gitmodules").exists(), (
        "no Git submodule may be used for the vendored runtime source (P7-1)"
    )
    # also guard the repo root .gitmodules from referencing the vendored tree
    root_gm = _REPO_ROOT / ".gitmodules"
    if root_gm.exists():
        assert "graph-memory" not in root_gm.read_text(encoding="utf-8"), (
            "root .gitmodules must not reference the vendored graph-memory tree"
        )


def test_no_nested_git_dir():
    assert not (_SVC / ".git").exists(), (
        "the vendored tree must not contain a nested .git directory "
        "(naive full-clone copy hazard)"
    )


def test_no_committed_secret_env():
    """Only ``.env.example`` is allowed; a real ``.env`` must never be vendored."""
    assert _nonempty(_SVC / ".env.example"), (
        "services/graph-memory/.env.example (config reference) must exist"
    )
    leaked = [
        p for p in _git_tracked_under_svc()
        if Path(p).name.startswith(".env") and Path(p).name != ".env.example"
    ]
    assert not leaked, f"vendored tree must not commit a real .env file: {leaked}"


def test_no_compiled_python_or_cache_committed():
    tracked = _git_tracked_under_svc()
    pyc = [p for p in tracked if p.endswith((".pyc", ".pyo"))]
    caches = [p for p in tracked if "__pycache__/" in p]
    assert not pyc, f"vendored tree must not commit compiled .pyc files: {pyc}"
    assert not caches, f"vendored tree must not commit __pycache__ dirs: {caches}"


def test_no_venv_committed():
    """P7-7 deferral (#123 — MINOR hardening deferred from the P7-1 gate): the
    hygiene gate covered ``.env`` and Python caches but not virtualenvs. The
    realistic import mistake is vendoring a LIVE upstream checkout whose
    ``venv/`` (or ``.venv/``, ``virtualenv/``) rides along in the copy.
    Git-tracked view only (``git ls-files``, like the other hygiene guards —
    P7-4 lesson): a local, gitignored venv on disk must never fail this gate."""
    tracked = _git_tracked_under_svc()
    venv_dir_names = {"venv", ".venv", "virtualenv", "virtualenvs"}
    leaked = sorted(
        p
        for p in tracked
        if venv_dir_names.intersection(Path(p).parts[:-1])
        or Path(p).name == "pyvenv.cfg"  # the venv marker file itself
    )
    assert not leaked, f"vendored tree must not commit a virtualenv: {leaked}"


def test_no_datastore_volume_or_dump_committed():
    """P7-7 deferral (#123 — MINOR hardening deferred from the P7-1 gate): no
    Neo4j/Qdrant datastore volume content or dump may be tracked under the
    vendored tree — datastore state lives in the compose named volumes
    (``neo4j_data`` / ``neo4j_logs`` / ``qdrant_data``), never in git. Catches
    vendoring an upstream checkout that ran with bind-mounted state dirs
    (``neo4j/data``, ``qdrant/storage``, ``data/neo4j``, …) or a committed
    ``.dump`` / ``.snapshot`` / ``.backup`` artifact. Git-tracked view only
    (``git ls-files`` — P7-4 lesson: never a filesystem scan)."""
    tracked = _git_tracked_under_svc()
    volume_dir_names = {
        "neo4j_data", "neo4j-data", "neo4j_logs", "neo4j-logs",
        "qdrant_data", "qdrant-data", "qdrant_storage", "qdrant-storage",
    }
    dump_suffixes = {".dump", ".snapshot", ".backup"}
    datastores = {"neo4j", "qdrant"}
    state_children = {"data", "storage", "logs", "import", "snapshots"}

    def _is_datastore_state(rel: str) -> bool:
        parts = Path(rel).parts
        if volume_dir_names.intersection(parts[:-1]):
            return True
        if Path(rel).suffix.lower() in dump_suffixes:
            return True
        for parent, child in zip(parts, parts[1:]):
            # bind-mount shapes: <datastore>/<state-dir>/… or data|volumes/<datastore>/…
            if parent in datastores and child in state_children:
                return True
            if parent in {"data", "volumes"} and child in datastores:
                return True
        return False

    leaked = sorted(p for p in tracked if _is_datastore_state(p))
    assert not leaked, (
        f"vendored tree must not commit datastore volume state or dumps: {leaked}"
    )


# --------------------------------------------------------------------------- #
# 3. Provenance is tracked and pins the upstream commit                        #
# --------------------------------------------------------------------------- #


def test_third_party_notices_records_provenance():
    assert _nonempty(_NOTICES), "THIRD_PARTY_NOTICES.md must exist at repo root"
    body = _NOTICES.read_text(encoding="utf-8")
    assert _UPSTREAM_URL in body, (
        f"THIRD_PARTY_NOTICES.md must record the upstream URL {_UPSTREAM_URL}"
    )
    assert _PINNED_SHA in body, (
        f"THIRD_PARTY_NOTICES.md must pin the vendored commit {_PINNED_SHA}"
    )
    assert "Apache" in body, (
        "THIRD_PARTY_NOTICES.md must record the upstream Apache-2.0 license"
    )
