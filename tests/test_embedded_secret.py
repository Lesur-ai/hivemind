# -*- coding: utf-8 -*-
"""Issue #183 — durable, fail-closed embedded credential publication."""

from __future__ import annotations

import errno
import multiprocessing
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from live_mem.core import embedded_secret, secret_fs
from live_mem.core.embedded_secret import resolve_embedded_token
from live_mem.core.models import EMBEDDED_TOKEN_SENTINEL


def _stub(token: str, path: str):
    return SimpleNamespace(long_embedded_token=token, long_embedded_token_file=path)


def _secure_parent(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _secure_file(path: Path, value: str) -> None:
    _secure_parent(path.parent)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def emulated_supported_fs(monkeypatch: pytest.MonkeyPatch):
    """Exercise descriptor logic on non-Linux developer hosts.

    Linux CI and Docker proofs below use the real fstatfs/renameat2 path.  This
    fixture replaces only those two Linux-specific syscall wrappers.
    """

    monkeypatch.setattr(
        secret_fs, "require_supported_filesystem", lambda _fd: "test-local-fs"
    )
    monkeypatch.setattr(secret_fs, "require_supported_linux", lambda: None)

    def _rename_noreplace(directory_fd: int, source: str, destination: str) -> None:
        try:
            os.stat(destination, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(
                source,
                destination,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            return
        raise FileExistsError(errno.EEXIST, "destination exists", destination)

    monkeypatch.setattr(secret_fs, "rename_noreplace", _rename_noreplace)


def test_env_token_takes_precedence_before_file_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "tok"

    def _must_not_resolve_file(*_args, **_kwargs):
        raise AssertionError("file-backed path must not run for explicit env")

    monkeypatch.setattr(embedded_secret, "_resolve_file_token", _must_not_resolve_file)
    assert resolve_embedded_token(_stub("env-tok", str(f))) == "env-tok"
    assert not f.exists()


def test_generate_persists_0600_and_is_stable(
    tmp_path: Path, emulated_supported_fs
) -> None:
    parent = tmp_path / "sub"
    _secure_parent(parent)
    f = parent / "tok"
    tok1 = resolve_embedded_token(_stub("", str(f)))
    assert tok1 and tok1 != EMBEDDED_TOKEN_SENTINEL
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert resolve_embedded_token(_stub("", str(f))) == tok1


def test_reads_existing_secure_file(tmp_path: Path, emulated_supported_fs) -> None:
    f = tmp_path / "tok"
    _secure_file(f, "file-tok")
    assert resolve_embedded_token(_stub("", str(f))) == "file-tok"


def test_generate_false_is_side_effect_free(
    tmp_path: Path, emulated_supported_fs
) -> None:
    _secure_parent(tmp_path)
    f = tmp_path / "absent"
    assert resolve_embedded_token(_stub("", str(f)), generate=False) is None
    assert not f.exists()
    assert not (tmp_path / secret_fs.lock_name(f.name)).exists()


@pytest.mark.parametrize("generate", [False, True])
def test_sentinel_in_env_fails_closed(tmp_path: Path, generate: bool) -> None:
    f = tmp_path / "absent"
    assert (
        resolve_embedded_token(
            _stub(EMBEDDED_TOKEN_SENTINEL, str(f)), generate=generate
        )
        is None
    )
    assert not f.exists()


@pytest.mark.parametrize("generate", [False, True])
def test_sentinel_in_file_is_never_rewritten(
    tmp_path: Path, emulated_supported_fs, generate: bool
) -> None:
    f = tmp_path / "tok"
    _secure_file(f, EMBEDDED_TOKEN_SENTINEL)
    assert resolve_embedded_token(_stub("", str(f)), generate=generate) is None
    assert f.read_text(encoding="utf-8") == EMBEDDED_TOKEN_SENTINEL


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400])
def test_existing_file_with_unsafe_mode_is_not_repaired_or_replaced(
    tmp_path: Path, emulated_supported_fs, mode: int
) -> None:
    f = tmp_path / "tok"
    _secure_file(f, "existing-secret")
    f.chmod(mode)
    assert resolve_embedded_token(_stub("", str(f)), generate=True) is None
    assert f.read_text(encoding="utf-8") == "existing-secret"
    assert stat.S_IMODE(f.stat().st_mode) == mode


def test_symlink_destination_fails_closed(
    tmp_path: Path, emulated_supported_fs
) -> None:
    target = tmp_path / "target"
    _secure_file(target, "do-not-read")
    destination = tmp_path / "tok"
    destination.symlink_to(target)
    assert resolve_embedded_token(_stub("", str(destination))) is None
    assert target.read_text(encoding="utf-8") == "do-not-read"


def test_parent_requires_exact_0700(tmp_path: Path, emulated_supported_fs) -> None:
    tmp_path.chmod(0o755)
    destination = tmp_path / "tok"
    assert resolve_embedded_token(_stub("", str(destination))) is None
    assert not destination.exists()


def test_partial_writes_are_completed(
    tmp_path: Path,
    emulated_supported_fs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _secure_parent(tmp_path)
    destination = tmp_path / "tok"
    real_write = embedded_secret.os.write

    def _short_write(fd: int, payload: bytes) -> int:
        return real_write(fd, payload[:3])

    monkeypatch.setattr(embedded_secret.os, "write", _short_write)
    token = resolve_embedded_token(_stub("", str(destination)))
    assert token
    assert destination.read_text(encoding="utf-8") == token


def test_write_failure_leaves_no_destination_or_temp(
    tmp_path: Path,
    emulated_supported_fs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _secure_parent(tmp_path)
    destination = tmp_path / "tok"

    def _fail_write(_fd: int, _payload: bytes) -> int:
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(embedded_secret.os, "write", _fail_write)
    assert resolve_embedded_token(_stub("", str(destination))) is None
    assert not destination.exists()
    assert not list(tmp_path.glob(f"{secret_fs.temp_prefix(destination.name)}*"))


def test_orphan_temp_is_removed_before_generation(
    tmp_path: Path, emulated_supported_fs
) -> None:
    _secure_parent(tmp_path)
    destination = tmp_path / "tok"
    orphan = tmp_path / f"{secret_fs.temp_prefix(destination.name)}{'a' * 32}"
    _secure_file(orphan, "partial")
    token = resolve_embedded_token(_stub("", str(destination)))
    assert token and destination.read_text(encoding="utf-8") == token
    assert not orphan.exists()


def test_malformed_reserved_temp_fails_without_cleanup(
    tmp_path: Path, emulated_supported_fs
) -> None:
    _secure_parent(tmp_path)
    destination = tmp_path / "tok"
    malformed = tmp_path / f"{secret_fs.temp_prefix(destination.name)}bad"
    _secure_file(malformed, "partial")
    assert resolve_embedded_token(_stub("", str(destination))) is None
    assert malformed.exists()
    assert not destination.exists()


def test_orphan_unlink_failure_fails_closed(
    tmp_path: Path,
    emulated_supported_fs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _secure_parent(tmp_path)
    destination = tmp_path / "tok"
    orphan = tmp_path / f"{secret_fs.temp_prefix(destination.name)}{'b' * 32}"
    _secure_file(orphan, "partial")

    monkeypatch.setattr(
        embedded_secret.os,
        "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EPERM, "unlink denied")
        ),
    )
    assert resolve_embedded_token(_stub("", str(destination))) is None
    assert orphan.exists() and not destination.exists()


def test_post_publish_directory_fsync_failure_reuses_published_secret(
    tmp_path: Path,
    emulated_supported_fs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _secure_parent(tmp_path)
    destination = tmp_path / "tok"
    after_publish = False
    real_fsync = embedded_secret.os.fsync

    def _mark_published() -> None:
        nonlocal after_publish
        after_publish = True

    def _fail_directory_fsync(fd: int) -> None:
        if after_publish and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(embedded_secret.os, "fsync", _fail_directory_fsync)
    with pytest.raises(OSError, match="directory fsync failed"):
        embedded_secret._resolve_file_token(
            str(destination), generate=True, after_publish=_mark_published
        )
    assert destination.exists()

    monkeypatch.setattr(embedded_secret.os, "fsync", real_fsync)
    published = destination.read_text(encoding="utf-8")
    assert resolve_embedded_token(_stub("", str(destination))) == published


def test_filesystem_rejected_before_destination_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _secure_parent(tmp_path)
    destination = tmp_path / "tok"
    monkeypatch.setattr(
        secret_fs,
        "require_supported_filesystem",
        lambda _fd: (_ for _ in ()).throw(RuntimeError("unsupported fs")),
    )
    monkeypatch.setattr(
        embedded_secret,
        "_open_existing_secret",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("destination touched before fs classification")
        ),
    )
    assert resolve_embedded_token(_stub("", str(destination))) is None


def _crash_worker(path: str, stage: str) -> None:
    callback = lambda: os._exit(91 if stage == "temp" else 92)
    embedded_secret._resolve_file_token(
        path,
        generate=True,
        after_temp_fsync=callback if stage == "temp" else None,
        after_publish=callback if stage == "publish" else None,
    )


def _concurrent_worker(path: str, queue) -> None:
    queue.put(resolve_embedded_token(_stub("", path)))


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux syscall proof")
@pytest.mark.parametrize("stage,exit_code", [("temp", 91), ("publish", 92)])
def test_process_death_publication_boundaries_recover(
    tmp_path: Path, stage: str, exit_code: int
) -> None:
    _secure_parent(tmp_path)
    destination = tmp_path / "tok"
    process = multiprocessing.get_context("fork").Process(
        target=_crash_worker, args=(str(destination), stage)
    )
    process.start()
    process.join(10)
    assert process.exitcode == exit_code

    value_after_crash = (
        destination.read_text(encoding="utf-8") if destination.exists() else None
    )
    recovered = resolve_embedded_token(_stub("", str(destination)))
    assert recovered
    if value_after_crash is not None:
        assert recovered == value_after_crash
    assert not list(tmp_path.glob(f"{secret_fs.temp_prefix(destination.name)}*"))


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux syscall proof")
def test_multiprocess_generation_publishes_one_plaintext(tmp_path: Path) -> None:
    _secure_parent(tmp_path)
    destination = tmp_path / "tok"
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    processes = [
        context.Process(target=_concurrent_worker, args=(str(destination), queue))
        for _ in range(6)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    values = [queue.get(timeout=2) for _ in processes]
    assert len(set(values)) == 1
    assert values[0] == destination.read_text(encoding="utf-8")
