# -*- coding: utf-8 -*-
"""Descriptor-bound Compose volume initializer guards for issue #183."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from live_mem.core import secret_fs
from live_mem.mesh import replay as mesh_replay
from live_mem.mesh.replay import MeshProcessIdentityLock
from scripts import init_embedded_secret_volume as init


@pytest.fixture
def local_initializer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(init, "SECRETS_DIRECTORY", str(tmp_path))
    monkeypatch.setattr(init, "require_supported_linux", lambda: None)
    monkeypatch.setattr(init, "require_supported_filesystem", lambda _fd: "testfs")
    monkeypatch.setattr(init, "fchownat_empty_path", lambda *_args: None)
    monkeypatch.setattr(
        mesh_replay, "_require_process_lock_filesystem", lambda _fd: None
    )
    monkeypatch.setattr(
        init.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid()),
    )
    # Darwin has no O_PATH. O_RDONLY is sufficient for these same-uid unit
    # tests; the real CAP_CHOWN/O_PATH contract is proven in Linux Docker.
    monkeypatch.setattr(init.os, "O_PATH", getattr(init.os, "O_PATH", 0), raising=False)
    return tmp_path


def _write(path: Path, value: str, mode: int) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)


def _mesh_lock_directory(local_initializer: Path, mode: int = 0o700) -> Path:
    directory = local_initializer / init.MESH_PROCESS_LOCK_DIRECTORY
    directory.mkdir()
    directory.chmod(mode)
    return directory


def _mesh_lock_file(
    directory: Path,
    *,
    stem: str = "a" * 64,
    content: bytes | None = None,
    mode: int = 0o600,
) -> Path:
    path = directory / f"{stem}.lock"
    path.write_bytes((stem + "\n").encode("ascii") if content is None else content)
    path.chmod(mode)
    return path


def test_empty_volume_initialization_is_repeatable(local_initializer: Path) -> None:
    init.initialize_volume()
    assert stat.S_IMODE(local_initializer.stat().st_mode) == 0o700
    init.initialize_volume()
    assert stat.S_IMODE(local_initializer.stat().st_mode) == 0o700


def test_mesh_process_lock_survives_initializer_restart(
    local_initializer: Path,
) -> None:
    init.initialize_volume()
    lock_directory = local_initializer / init.MESH_PROCESS_LOCK_DIRECTORY
    identity = "hm1:" + "ab" * 32

    first = MeshProcessIdentityLock(lock_directory, identity)
    first.acquire(timeout_seconds=0)
    first.close()
    entries = list(lock_directory.iterdir())
    assert len(entries) == 1
    assert entries[0].read_bytes() == (entries[0].stem + "\n").encode("ascii")

    init.initialize_volume()
    assert stat.S_IMODE(lock_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(entries[0].stat().st_mode) == 0o600
    second = MeshProcessIdentityLock(lock_directory, identity)
    second.acquire(timeout_seconds=0)
    second.close()


def test_empty_interrupted_mesh_process_lock_is_preserved(
    local_initializer: Path,
) -> None:
    directory = _mesh_lock_directory(local_initializer)
    lock = _mesh_lock_file(directory, content=b"")
    init.initialize_volume()
    assert lock.read_bytes() == b""
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_mesh_process_lock_directory_and_file_modes_are_repaired(
    local_initializer: Path,
) -> None:
    directory = _mesh_lock_directory(local_initializer, 0o755)
    lock = _mesh_lock_file(directory, mode=0o644)
    init.initialize_volume()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "name",
    [
        "a" * 63 + ".lock",
        "A" * 64 + ".lock",
        "a" * 64 + ".LOCK",
        "a" * 64 + ".lock.extra",
    ],
)
def test_invalid_mesh_process_lock_names_fail_closed(
    local_initializer: Path, name: str
) -> None:
    directory = _mesh_lock_directory(local_initializer)
    _write(directory / name, "untrusted", 0o600)
    with pytest.raises((OSError, RuntimeError)):
        init.initialize_volume()


@pytest.mark.parametrize(
    "content",
    [
        b"a" * 64,
        b"b" * 64 + b"\n",
        b"a" * 64 + b"\nextra",
        b"\xff",
    ],
)
def test_invalid_mesh_process_lock_content_fails_closed(
    local_initializer: Path, content: bytes
) -> None:
    directory = _mesh_lock_directory(local_initializer)
    lock = _mesh_lock_file(directory, content=content)
    with pytest.raises((OSError, RuntimeError)):
        init.initialize_volume()
    assert lock.read_bytes() == content


def test_mesh_process_lock_directory_symlink_is_never_followed(
    local_initializer: Path,
) -> None:
    outside = local_initializer.parent / "outside-lock-directory"
    outside.mkdir()
    canary = outside / "canary"
    canary.write_text("untouched", encoding="utf-8")
    (local_initializer / init.MESH_PROCESS_LOCK_DIRECTORY).symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises((OSError, RuntimeError)):
        init.initialize_volume()
    assert canary.read_text(encoding="utf-8") == "untouched"


def test_mesh_process_lock_file_symlink_is_never_followed(
    local_initializer: Path,
) -> None:
    directory = _mesh_lock_directory(local_initializer)
    outside = local_initializer.parent / "outside-lock-file"
    outside.write_text("untouched", encoding="utf-8")
    (directory / ("a" * 64 + ".lock")).symlink_to(outside)
    with pytest.raises((OSError, RuntimeError)):
        init.initialize_volume()
    assert outside.read_text(encoding="utf-8") == "untouched"


def test_mesh_process_lock_hardlink_fails_closed(local_initializer: Path) -> None:
    directory = _mesh_lock_directory(local_initializer)
    outside = local_initializer.parent / "outside-lock-hardlink"
    outside.write_bytes(b"a" * 64 + b"\n")
    outside.chmod(0o600)
    os.link(outside, directory / ("a" * 64 + ".lock"))
    with pytest.raises((OSError, RuntimeError)):
        init.initialize_volume()
    assert outside.read_bytes() == b"a" * 64 + b"\n"


def test_mesh_process_lock_nested_entry_fails_closed(
    local_initializer: Path,
) -> None:
    directory = _mesh_lock_directory(local_initializer)
    (directory / ("a" * 64 + ".lock")).mkdir()
    with pytest.raises((OSError, RuntimeError)):
        init.initialize_volume()


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_mode_000_mesh_process_lock_entry_is_not_claimed_repairable(
    local_initializer: Path, kind: str
) -> None:
    directory = _mesh_lock_directory(local_initializer)
    lock = _mesh_lock_file(directory)
    target = directory if kind == "directory" else lock
    target.chmod(0o000)
    try:
        with pytest.raises((OSError, RuntimeError)):
            init.initialize_volume()
        assert stat.S_IMODE(target.stat().st_mode) == 0o000
    finally:
        target.chmod(0o700 if kind == "directory" else 0o600)
        lock.unlink()
        directory.rmdir()


def test_existing_destination_and_lock_are_repaired(
    local_initializer: Path,
) -> None:
    destination = local_initializer / init.SECRET_BASENAME
    lock = local_initializer / secret_fs.lock_name(init.SECRET_BASENAME)
    _write(destination, "secret", 0o644)
    _write(lock, "", 0o640)
    init.initialize_volume()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_valid_orphan_temp_is_removed(local_initializer: Path) -> None:
    orphan = local_initializer / (
        secret_fs.temp_prefix(init.SECRET_BASENAME) + "a" * 32
    )
    _write(orphan, "partial", 0o600)
    init.initialize_volume()
    assert not orphan.exists()


@pytest.mark.parametrize("kind", ["unknown", "malformed-temp", "directory"])
def test_unexpected_entries_fail_closed(local_initializer: Path, kind: str) -> None:
    if kind == "unknown":
        (local_initializer / "other").write_text("x", encoding="utf-8")
    elif kind == "malformed-temp":
        (local_initializer / f"{secret_fs.temp_prefix(init.SECRET_BASENAME)}bad").write_text(
            "x", encoding="utf-8"
        )
    else:
        (local_initializer / init.SECRET_BASENAME).mkdir()
    with pytest.raises((OSError, RuntimeError)):
        init.initialize_volume()


def test_symlink_entry_is_never_followed(local_initializer: Path) -> None:
    target = local_initializer.parent / "outside-secret"
    target.write_text("untouched", encoding="utf-8")
    (local_initializer / init.SECRET_BASENAME).symlink_to(target)
    with pytest.raises((OSError, RuntimeError)):
        init.initialize_volume()
    assert target.read_text(encoding="utf-8") == "untouched"


def test_mode_000_entry_is_not_claimed_repairable(local_initializer: Path) -> None:
    destination = local_initializer / init.SECRET_BASENAME
    _write(destination, "secret", 0o000)
    with pytest.raises((OSError, RuntimeError)):
        init.initialize_volume()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o000


def test_child_durability_order_is_chmod_chown_verify_fsync(
    local_initializer: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = local_initializer / init.SECRET_BASENAME
    _write(destination, "secret", 0o600)
    directory_fd = os.open(local_initializer, os.O_RDONLY)
    path_fd = os.open(destination, os.O_RDONLY)
    entry = init._BoundEntry(init.SECRET_BASENAME, path_fd, os.fstat(path_fd), False)
    events: list[str] = []
    real_fchmod = init.os.fchmod
    real_fchown = init.os.fchown
    real_fstat = init.os.fstat
    real_fsync = init.os.fsync

    monkeypatch.setattr(
        init.os,
        "fchmod",
        lambda fd, mode: (events.append("chmod"), real_fchmod(fd, mode))[1],
    )
    monkeypatch.setattr(
        init.os,
        "fchown",
        lambda fd, uid, gid: (
            events.append("chown"),
            real_fchown(fd, uid, gid),
        )[1],
    )
    monkeypatch.setattr(
        init.os,
        "fstat",
        lambda fd: (events.append("stat"), real_fstat(fd))[1],
    )
    monkeypatch.setattr(
        init.os,
        "fsync",
        lambda fd: (events.append("fsync"), real_fsync(fd))[1],
    )
    try:
        init._repair_regular(
            directory_fd,
            entry,
            directory_device=entry.info.st_dev,
            target_uid=os.geteuid(),
            target_gid=os.getegid(),
        )
    finally:
        os.close(path_fd)
        os.close(directory_fd)

    tail = events[events.index("chmod") :]
    assert tail == ["chmod", "chown", "stat", "fsync"]


def test_mesh_process_lock_is_claimed_before_content_open_under_cap_chown(
    local_initializer: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _mesh_lock_directory(local_initializer)
    lock = _mesh_lock_file(directory)
    directory_fd = os.open(directory, os.O_RDONLY)
    path_fd = os.open(lock, os.O_RDONLY)
    entry = init._BoundEntry(lock.name, path_fd, os.fstat(path_fd), False)
    real_open = init.os.open
    events: list[str] = []
    claimed = False

    def claim(fd: int, uid: int, gid: int) -> None:
        nonlocal claimed
        assert fd == path_fd
        assert (uid, gid) == (0, 0)
        claimed = True
        events.append("claim")

    def guarded_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == entry.name:
            assert claimed, "CAP_CHOWN-only init must claim before O_RDONLY"
            events.append("open")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(init, "fchownat_empty_path", claim)
    monkeypatch.setattr(init.os, "open", guarded_open)
    try:
        init._repair_mesh_process_lock_file(
            directory_fd,
            entry,
            directory_device=entry.info.st_dev,
            target_uid=os.geteuid(),
            target_gid=os.getegid(),
        )
    finally:
        os.close(path_fd)
        os.close(directory_fd)

    assert events == ["claim", "open"]


def test_post_chown_fsync_failure_is_fatal(
    local_initializer: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = local_initializer / init.SECRET_BASENAME
    _write(destination, "secret", 0o600)
    directory_fd = os.open(local_initializer, os.O_RDONLY)
    path_fd = os.open(destination, os.O_RDONLY)
    entry = init._BoundEntry(init.SECRET_BASENAME, path_fd, os.fstat(path_fd), False)
    monkeypatch.setattr(
        init.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError(errno.EIO, "fsync failed")),
    )
    try:
        with pytest.raises(OSError, match="fsync failed"):
            init._repair_regular(
                directory_fd,
                entry,
                directory_device=entry.info.st_dev,
                target_uid=os.geteuid(),
                target_gid=os.getegid(),
            )
    finally:
        os.close(path_fd)
        os.close(directory_fd)


def test_initializer_source_has_no_recursive_or_pathname_mutation() -> None:
    source = Path(init.__file__).read_text(encoding="utf-8")
    for forbidden in ("os.walk(", "os.chmod(", "os.chown(", "shutil.chown"):
        assert forbidden not in source
    for required in (
        "O_NOFOLLOW",
        "dir_fd=directory_fd",
        "fchownat_empty_path",
        "os.fchmod(",
        "os.fchown(",
        "os.fsync(",
    ):
        assert required in source
