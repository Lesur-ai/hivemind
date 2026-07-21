# -*- coding: utf-8 -*-
"""Source guards for the blocking issue #183 Docker runtime proof.

The behavior itself runs on the Linux Docker runner. These tests make removal or
weakening of that runtime job visible in the ordinary Python test job.
"""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    path = ROOT / relative
    if not path.exists():
        # The private CI workflow is excluded from the public release tree; this
        # lint targets the private repo and skips when its subject is absent.
        pytest.skip(f"{relative} is private-only (absent from the public release tree)")
    return path.read_text(encoding="utf-8")


def _assert_ci_gate(workflow: str) -> None:
    assert "  embedded_secret_runtime:" in workflow
    assert "run: bash scripts/verify_embedded_secret_docker.sh" in workflow
    # embedded_secret_runtime must be a REQUIRED build dependency (issue #183),
    # regardless of any other required jobs (e.g. the P9-3 public_tree gate) also
    # being listed in the same `needs:`.
    needs_lines = [ln for ln in workflow.splitlines() if ln.strip().startswith("needs:")]
    assert any("embedded_secret_runtime" in ln for ln in needs_lines), (
        "embedded_secret_runtime must be listed in the build job's needs"
    )


def _assert_proof_matrix(shell: str, helper: str) -> None:
    for marker in (
        "PROOF_QUIESCENCE_OK",
        "root_owned_entries",
        "PROOF_INIT_CONTENTS_OK",
        "PROOF_RECREATE_OK",
        "PROOF_MESH_LOCK_CREATED",
        "PROOF_MESH_LOCK_RESTART_OK",
        "PROOF_INIT_REJECTED kind=$suffix",
        "PROOF_REJECTED_ENTRY_RETAINED",
        "PROOF_CAP_CHOWN_REQUIRED",
        "PROOF_UNSUPPORTED_FILESYSTEM_REJECTED",
        "PROOF_ISSUE_183_DOCKER_OK",
    ):
        assert marker in shell or marker in helper
    for command in (
        "main-write",
        "main-read",
        "mesh-lock-create",
        "mesh-lock-reacquire",
        "inspect-initialized",
        "legacy-wait",
        "expect-entry",
    ):
        assert command in shell
        assert command in helper
    for runtime_assertion in (
        'int(_status_value("CapEff"), 16)',
        '_status_value("NoNewPrivs")',
        'os.statvfs("/").f_flag & os.ST_RDONLY',
        'Path("/proc/net/dev")',
        "initialize_volume()",
        "resolve_embedded_token(generate=generate)",
        "hashlib.sha256",
        "MeshProcessIdentityLock(",
        "_assert_main_profile()",
        'entry.read_bytes() == (stem + "\\n").encode("ascii")',
        "stat.S_IMODE(directory_info.st_mode) == 0o700",
        "stat.S_IMODE(info.st_mode) == 0o600",
    ):
        assert runtime_assertion in helper

    create_position = shell.index("mesh-lock-create")
    initializer_position = shell.index("PRODUCTION_INIT_NAME=", create_position)
    reacquire_position = shell.index("mesh-lock-reacquire", initializer_position)
    assert create_position < initializer_position < reacquire_position
    for exact_profile in (
        'compose_for "$PRIMARY_PROJECT" run --rm --no-deps -T \\\n'
        '  --name "$MESH_CREATE_NAME" hivemind',
        'compose_for "$PRIMARY_PROJECT" run --rm --no-deps -T \\\n'
        '  --name "$PRODUCTION_INIT_NAME" hivemind-secrets-init',
        'compose_for "$PRIMARY_PROJECT" run --rm --no-deps -T \\\n'
        '  --name "$MESH_REACQUIRE_NAME" hivemind',
    ):
        assert exact_profile in shell

    helper_position = helper.index("def _mesh_process_lock(")
    main_profile_position = helper.index(
        "uid, gid = _assert_main_profile()", helper_position
    )
    pre_reacquire_validation = helper.index(
        "_assert_mesh_process_lock(uid=uid, gid=gid)", main_profile_position
    )
    real_lock_position = helper.index(
        "lock = MeshProcessIdentityLock(", pre_reacquire_validation
    )
    assert (
        helper_position
        < main_profile_position
        < pre_reacquire_validation
        < real_lock_position
    )


def test_issue_183_docker_proof_is_blocking_and_complete() -> None:
    _assert_ci_gate(_read(".github/workflows/build.yml"))
    _assert_proof_matrix(
        _read("scripts/verify_embedded_secret_docker.sh"),
        _read("scripts/verify_embedded_secret_container.py"),
    )


def test_mutation_red_runtime_job_removed_from_build_dependencies() -> None:
    workflow = _read(".github/workflows/build.yml").replace(
        "needs: [test, test_python314_arm64, audit, embedded_secret_runtime, public_tree]",
        "needs: [test, test_python314_arm64, audit, public_tree]",
    )
    with pytest.raises(AssertionError):
        _assert_ci_gate(workflow)


def test_mutation_red_persistence_recreate_proof_removed() -> None:
    shell = _read("scripts/verify_embedded_secret_docker.sh").replace(
        "PROOF_RECREATE_OK", "PROOF_RECREATE_REMOVED"
    )
    with pytest.raises(AssertionError):
        _assert_proof_matrix(
            shell,
            _read("scripts/verify_embedded_secret_container.py"),
        )


def test_mutation_red_mesh_restart_initializer_removed() -> None:
    shell = _read("scripts/verify_embedded_secret_docker.sh")
    start = shell.index("PRODUCTION_INIT_NAME=")
    end = shell.index("# Back under the exact main profile", start)
    shell = shell[:start] + shell[end:]
    with pytest.raises((AssertionError, ValueError)):
        _assert_proof_matrix(
            shell,
            _read("scripts/verify_embedded_secret_container.py"),
        )
