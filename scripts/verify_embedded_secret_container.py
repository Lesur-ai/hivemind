#!/usr/bin/env python3
"""Container-side assertions for the issue #183 Docker runtime proof.

This helper is intentionally invoked only by ``verify_embedded_secret_docker.sh``.
It prints metadata and SHA-256 fingerprints, never credential plaintext.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pwd
import stat
import time
from pathlib import Path

from live_mem.core.embedded_secret import resolve_embedded_token
from live_mem.core.secret_fs import lock_name, temp_prefix
from live_mem.mesh.replay import MeshProcessIdentityLock
from init_embedded_secret_volume import (
    MESH_PROCESS_LOCK_DIRECTORY,
    SECRET_BASENAME,
    SECRETS_DIRECTORY,
    initialize_volume,
)


_DIRECTORY = Path(SECRETS_DIRECTORY)
_SECRET = _DIRECTORY / SECRET_BASENAME
_LOCK = _DIRECTORY / lock_name(SECRET_BASENAME)
_VALID_ORPHAN = _DIRECTORY / f"{temp_prefix(SECRET_BASENAME)}{'a' * 32}"
_MALFORMED_ORPHAN = _DIRECTORY / f"{temp_prefix(SECRET_BASENAME)}bad"
_UNKNOWN_ENTRY = _DIRECTORY / "unexpected-entry"
_MESH_LOCK_DIRECTORY = _DIRECTORY / MESH_PROCESS_LOCK_DIRECTORY
_MESH_LOCK_IDENTITY = "hm1:" + "a" * 64


def _status_value(key: str) -> str:
    prefix = f"{key}:"
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"missing {key} in /proc/self/status")


def _assert_profile(
    *,
    expected_uid: int,
    expected_gid: int,
    expected_cap_eff: int,
    networkless: bool,
    rootfs_read_only: bool,
) -> None:
    assert os.geteuid() == expected_uid
    assert os.getegid() == expected_gid
    assert int(_status_value("CapEff"), 16) == expected_cap_eff
    assert _status_value("NoNewPrivs") == "1"
    if rootfs_read_only:
        assert os.statvfs("/").f_flag & os.ST_RDONLY
    if networkless:
        interfaces = {
            line.split(":", 1)[0].strip()
            for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()
            if ":" in line
        }
        assert interfaces <= {"lo"}, f"networkless profile exposed {interfaces!r}"


def _assert_metadata(path: Path, *, mode: int, uid: int, gid: int) -> None:
    info = path.stat(follow_symlinks=False)
    assert info.st_uid == uid
    assert info.st_gid == gid
    assert stat.S_IMODE(info.st_mode) == mode
    if path != _DIRECTORY:
        assert stat.S_ISREG(info.st_mode)
        assert info.st_nlink == 1


def _seed(kind: str) -> None:
    assert os.geteuid() == 0
    os.chown(_DIRECTORY, 0, 0)
    os.chmod(_DIRECTORY, 0o755)
    if kind == "valid-orphan":
        paths = (_VALID_ORPHAN, _LOCK)
    elif kind == "unknown-entry":
        paths = (_UNKNOWN_ENTRY,)
    elif kind == "malformed-orphan":
        paths = (_MALFORMED_ORPHAN,)
    else:  # pragma: no cover - argparse constrains this
        raise AssertionError(f"unknown seed kind: {kind}")
    for path in paths:
        path.write_bytes(b"fixture-only-not-a-credential")
        os.chmod(path, 0o600)
        os.chown(path, 0, 0)
    _assert_metadata(_DIRECTORY, mode=0o755, uid=0, gid=0)
    for path in paths:
        _assert_metadata(path, mode=0o600, uid=0, gid=0)
    print(
        f"PROOF_SEEDED kind={kind} uid=0 gid=0 mode=0755 "
        f"root_owned_entries={len(paths)}"
    )


def _init(expected_cap_eff: int) -> None:
    _assert_profile(
        expected_uid=0,
        expected_gid=0,
        expected_cap_eff=expected_cap_eff,
        networkless=True,
        rootfs_read_only=True,
    )
    initialize_volume()
    account = pwd.getpwnam("mcp")
    _assert_metadata(
        _DIRECTORY,
        mode=0o700,
        uid=account.pw_uid,
        gid=account.pw_gid,
    )
    print(
        "PROOF_INIT_OK "
        f"cap_eff={expected_cap_eff:x} uid={account.pw_uid} gid={account.pw_gid} "
        "mode=0700"
    )


def _assert_main_profile() -> tuple[int, int]:
    account = pwd.getpwnam("mcp")
    _assert_profile(
        expected_uid=account.pw_uid,
        expected_gid=account.pw_gid,
        expected_cap_eff=0,
        networkless=False,
        rootfs_read_only=False,
    )
    return account.pw_uid, account.pw_gid


def _main(*, generate: bool) -> None:
    uid, gid = _assert_main_profile()
    value = resolve_embedded_token(generate=generate)
    assert value is not None
    assert value.startswith("lm_")
    _assert_metadata(_DIRECTORY, mode=0o700, uid=uid, gid=gid)
    _assert_metadata(_SECRET, mode=0o600, uid=uid, gid=gid)
    _assert_metadata(
        _DIRECTORY / lock_name(SECRET_BASENAME),
        mode=0o600,
        uid=uid,
        gid=gid,
    )
    fingerprint = hashlib.sha256(value.encode("utf-8")).hexdigest()
    print(f"PROOF_SHA={fingerprint}")


def _assert_mesh_process_lock(*, uid: int, gid: int) -> None:
    parent_info = _DIRECTORY.stat(follow_symlinks=False)
    directory_info = _MESH_LOCK_DIRECTORY.stat(follow_symlinks=False)
    assert stat.S_ISDIR(directory_info.st_mode)
    assert directory_info.st_dev == parent_info.st_dev
    assert directory_info.st_uid == uid
    assert directory_info.st_gid == gid
    assert stat.S_IMODE(directory_info.st_mode) == 0o700

    entries = list(_MESH_LOCK_DIRECTORY.iterdir())
    assert len(entries) == 1
    entry = entries[0]
    info = entry.stat(follow_symlinks=False)
    assert stat.S_ISREG(info.st_mode)
    assert info.st_dev == directory_info.st_dev
    assert info.st_nlink == 1
    assert info.st_uid == uid
    assert info.st_gid == gid
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert entry.name.endswith(".lock")
    stem = entry.name.removesuffix(".lock")
    assert len(stem) == 64
    assert set(stem) <= set("0123456789abcdef")
    assert entry.read_bytes() == (stem + "\n").encode("ascii")


def _mesh_process_lock(*, restart: bool) -> None:
    uid, gid = _assert_main_profile()
    if restart:
        _assert_mesh_process_lock(uid=uid, gid=gid)
    else:
        assert not _MESH_LOCK_DIRECTORY.exists()

    lock = MeshProcessIdentityLock(_MESH_LOCK_DIRECTORY, _MESH_LOCK_IDENTITY)
    try:
        lock.acquire(timeout_seconds=0)
        assert lock.acquired
    finally:
        lock.close()
    _assert_mesh_process_lock(uid=uid, gid=gid)
    if restart:
        print(
            "PROOF_MESH_LOCK_RESTART_OK "
            "owner_target=true directory_mode=0700 file_mode=0600 "
            "content_verified=true reacquired=true"
        )
    else:
        print("PROOF_MESH_LOCK_CREATED main_profile=true acquired=true")


def _inspect_initialized() -> None:
    uid, gid = _assert_main_profile()
    _assert_metadata(_DIRECTORY, mode=0o700, uid=uid, gid=gid)
    _assert_metadata(_LOCK, mode=0o600, uid=uid, gid=gid)
    entries = sorted(path.name for path in _DIRECTORY.iterdir())
    assert entries == [lock_name(SECRET_BASENAME)]
    print("PROOF_INIT_CONTENTS_OK valid_orphan_removed=true lock_repaired=true")


def _legacy_wait() -> None:
    _assert_main_profile()
    assert resolve_embedded_token(generate=False) is None
    print("PROOF_QUIESCENCE_OLD_PROCESS_READY", flush=True)
    while True:
        time.sleep(30)


def _expect_entry(kind: str) -> None:
    expected = {
        "unknown-entry": _UNKNOWN_ENTRY,
        "malformed-orphan": _MALFORMED_ORPHAN,
    }[kind]
    assert expected.exists()
    print(f"PROOF_REJECTED_ENTRY_RETAINED kind={kind}")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed = subparsers.add_parser("seed")
    seed.add_argument(
        "kind",
        choices=("valid-orphan", "unknown-entry", "malformed-orphan"),
    )

    init = subparsers.add_parser("init")
    init.add_argument("--expected-cap-eff", type=int, default=1)

    subparsers.add_parser("main-write")
    subparsers.add_parser("main-read")
    subparsers.add_parser("mesh-lock-create")
    subparsers.add_parser("mesh-lock-reacquire")
    subparsers.add_parser("inspect-initialized")
    subparsers.add_parser("legacy-wait")
    expect = subparsers.add_parser("expect-entry")
    expect.add_argument("kind", choices=("unknown-entry", "malformed-orphan"))

    args = parser.parse_args()
    if args.command == "seed":
        _seed(args.kind)
    elif args.command == "init":
        _init(args.expected_cap_eff)
    elif args.command == "main-write":
        _main(generate=True)
    elif args.command == "main-read":
        _main(generate=False)
    elif args.command == "mesh-lock-create":
        _mesh_process_lock(restart=False)
    elif args.command == "mesh-lock-reacquire":
        _mesh_process_lock(restart=True)
    elif args.command == "inspect-initialized":
        _inspect_initialized()
    elif args.command == "legacy-wait":
        _legacy_wait()
    elif args.command == "expect-entry":
        _expect_entry(args.kind)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
