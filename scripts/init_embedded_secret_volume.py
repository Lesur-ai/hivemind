#!/usr/bin/env python3
"""Prepare the dedicated Compose volume for the non-root Hivemind runtime."""

from __future__ import annotations

import os
import pwd
import re
import stat
import sys
from dataclasses import dataclass

from live_mem.core.secret_fs import (
    fchownat_empty_path,
    is_temp_name,
    lock_name,
    require_supported_filesystem,
    require_supported_linux,
    temp_prefix,
)


SECRETS_DIRECTORY = "/data/secrets"
SECRET_BASENAME = "long_embedded_token"
MESH_PROCESS_LOCK_DIRECTORY = "mesh-process-locks"
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
_MESH_PROCESS_LOCK_RE = re.compile(r"[0-9a-f]{64}\.lock", re.ASCII)


@dataclass
class _BoundEntry:
    name: str
    path_fd: int
    info: os.stat_result
    temporary: bool


@dataclass
class _BoundMeshProcessLockDirectory:
    name: str
    path_fd: int
    directory_fd: int
    info: os.stat_result
    entries: list[_BoundEntry]


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _path_flags(*, directory: bool = False) -> int:
    flags = os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    if directory:
        flags |= os.O_DIRECTORY
    return flags


def _read_flags(*, directory: bool = False) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    if directory:
        flags |= os.O_DIRECTORY
    return flags


def _validate_bound_regular(
    info: os.stat_result,
    *,
    directory_device: int,
    target_uid: int,
    temporary: bool,
) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("unsupported entry type in embedded secret volume")
    if info.st_dev != directory_device or info.st_nlink != 1:
        raise RuntimeError("unsafe entry identity in embedded secret volume")
    if info.st_uid not in {0, target_uid}:
        raise RuntimeError("unsafe entry owner in embedded secret volume")
    mode = stat.S_IMODE(info.st_mode)
    if temporary and mode != FILE_MODE:
        raise RuntimeError("unsafe temporary entry mode in embedded secret volume")
    if not temporary and not (mode & stat.S_IRUSR):
        raise RuntimeError(
            "embedded secret entry is not repairable with CAP_CHOWN only"
        )


def _validate_mesh_process_lock_directory(
    info: os.stat_result,
    *,
    directory_device: int,
    target_uid: int,
) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("Mesh process-lock entry is not a directory")
    if info.st_dev != directory_device:
        raise RuntimeError("Mesh process-lock directory crosses filesystems")
    if info.st_uid not in {0, target_uid}:
        raise RuntimeError("unsafe Mesh process-lock directory owner")
    if stat.S_IMODE(info.st_mode) & 0o500 != 0o500:
        raise RuntimeError(
            "Mesh process-lock directory is not repairable with CAP_CHOWN only"
        )


def _validate_mesh_process_lock_file(
    info: os.stat_result,
    *,
    directory_device: int,
    target_uid: int,
) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("unsupported entry type in Mesh process-lock directory")
    if info.st_dev != directory_device or info.st_nlink != 1:
        raise RuntimeError("unsafe entry identity in Mesh process-lock directory")
    if info.st_uid not in {0, target_uid}:
        raise RuntimeError("unsafe entry owner in Mesh process-lock directory")
    if not (stat.S_IMODE(info.st_mode) & stat.S_IRUSR):
        raise RuntimeError(
            "Mesh process-lock entry is not repairable with CAP_CHOWN only"
        )


def _validate_mesh_process_lock_content(fd: int, name: str) -> None:
    expected = (name[:-5] + "\n").encode("ascii")
    limit = len(expected) + 1
    os.lseek(fd, 0, os.SEEK_SET)
    content = bytearray()
    while len(content) < limit:
        chunk = os.read(fd, limit - len(content))
        if not chunk:
            break
        content.extend(chunk)
    if bytes(content) not in {b"", expected}:
        raise RuntimeError("unsafe Mesh process-lock entry content")


def _bind_mesh_process_lock_children(
    directory_fd: int,
    *,
    directory_device: int,
    target_uid: int,
) -> list[_BoundEntry]:
    entries: list[_BoundEntry] = []
    try:
        for name in sorted(os.listdir(directory_fd)):
            if _MESH_PROCESS_LOCK_RE.fullmatch(name) is None:
                raise RuntimeError("unexpected entry in Mesh process-lock directory")
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            path_fd = os.open(name, _path_flags(), dir_fd=directory_fd)
            try:
                opened = os.fstat(path_fd)
                if not _same_inode(before, opened):
                    raise RuntimeError(
                        "Mesh process-lock entry changed identity during inspection"
                    )
                _validate_mesh_process_lock_file(
                    opened,
                    directory_device=directory_device,
                    target_uid=target_uid,
                )
                entry = _BoundEntry(name, path_fd, opened, False)
            except BaseException:
                os.close(path_fd)
                raise
            entries.append(entry)
        return entries
    except BaseException:
        for entry in entries:
            os.close(entry.path_fd)
        raise


def _bind_mesh_process_lock_directory(
    directory_fd: int,
    *,
    directory_device: int,
    target_uid: int,
) -> _BoundMeshProcessLockDirectory:
    name = MESH_PROCESS_LOCK_DIRECTORY
    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    path_fd = os.open(name, _path_flags(directory=True), dir_fd=directory_fd)
    mesh_directory_fd = -1
    entries: list[_BoundEntry] = []
    try:
        opened = os.fstat(path_fd)
        if not _same_inode(before, opened):
            raise RuntimeError(
                "Mesh process-lock directory changed identity during inspection"
            )
        _validate_mesh_process_lock_directory(
            opened,
            directory_device=directory_device,
            target_uid=target_uid,
        )

        fchownat_empty_path(path_fd, 0, 0)
        mesh_directory_fd = os.open(
            name, _read_flags(directory=True), dir_fd=directory_fd
        )
        reopened = os.fstat(mesh_directory_fd)
        if not _same_inode(opened, reopened):
            raise RuntimeError("Mesh process-lock directory changed identity")
        _validate_mesh_process_lock_directory(
            reopened,
            directory_device=directory_device,
            target_uid=target_uid,
        )
        os.fchmod(mesh_directory_fd, DIRECTORY_MODE)
        entries = _bind_mesh_process_lock_children(
            mesh_directory_fd,
            directory_device=directory_device,
            target_uid=target_uid,
        )
        return _BoundMeshProcessLockDirectory(
            name,
            path_fd,
            mesh_directory_fd,
            opened,
            entries,
        )
    except BaseException:
        for entry in entries:
            os.close(entry.path_fd)
        if mesh_directory_fd >= 0:
            os.close(mesh_directory_fd)
        os.close(path_fd)
        raise


def _close_mesh_process_lock_directory(
    directory: _BoundMeshProcessLockDirectory,
) -> None:
    for entry in directory.entries:
        os.close(entry.path_fd)
    os.close(directory.directory_fd)
    os.close(directory.path_fd)


def _bind_children(
    directory_fd: int,
    *,
    directory_device: int,
    target_uid: int,
) -> tuple[list[_BoundEntry], _BoundMeshProcessLockDirectory | None]:
    accepted_regular = {SECRET_BASENAME, lock_name(SECRET_BASENAME)}
    prefix = temp_prefix(SECRET_BASENAME)
    entries: list[_BoundEntry] = []
    mesh_directory: _BoundMeshProcessLockDirectory | None = None
    try:
        for name in sorted(os.listdir(directory_fd)):
            if name == MESH_PROCESS_LOCK_DIRECTORY:
                mesh_directory = _bind_mesh_process_lock_directory(
                    directory_fd,
                    directory_device=directory_device,
                    target_uid=target_uid,
                )
                continue
            temporary = name.startswith(prefix)
            if name not in accepted_regular and not (
                temporary and is_temp_name(SECRET_BASENAME, name)
            ):
                raise RuntimeError("unexpected entry in embedded secret volume")
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            path_fd = os.open(name, _path_flags(), dir_fd=directory_fd)
            try:
                opened = os.fstat(path_fd)
                if not _same_inode(before, opened):
                    raise RuntimeError(
                        "embedded secret entry changed identity during inspection"
                    )
                _validate_bound_regular(
                    opened,
                    directory_device=directory_device,
                    target_uid=target_uid,
                    temporary=temporary,
                )
            except BaseException:
                os.close(path_fd)
                raise
            entries.append(_BoundEntry(name, path_fd, opened, temporary))
        return entries, mesh_directory
    except BaseException:
        for entry in entries:
            os.close(entry.path_fd)
        if mesh_directory is not None:
            _close_mesh_process_lock_directory(mesh_directory)
        raise


def _remove_temporary(directory_fd: int, entry: _BoundEntry) -> None:
    current = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
    if not _same_inode(current, entry.info):
        raise RuntimeError("temporary entry changed identity before cleanup")
    os.unlink(entry.name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _repair_regular(
    directory_fd: int,
    entry: _BoundEntry,
    *,
    directory_device: int,
    target_uid: int,
    target_gid: int,
) -> None:
    fchownat_empty_path(entry.path_fd, 0, 0)
    fd = os.open(entry.name, _read_flags(), dir_fd=directory_fd)
    try:
        opened = os.fstat(fd)
        if not _same_inode(opened, entry.info) or not stat.S_ISREG(opened.st_mode):
            raise RuntimeError("embedded secret entry changed identity during repair")
        if opened.st_dev != directory_device or opened.st_nlink != 1:
            raise RuntimeError("embedded secret entry became unsafe during repair")

        os.fchmod(fd, FILE_MODE)
        os.fchown(fd, target_uid, target_gid)
        final = os.fstat(fd)
        if (
            not _same_inode(final, entry.info)
            or not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or final.st_uid != target_uid
            or final.st_gid != target_gid
            or stat.S_IMODE(final.st_mode) != FILE_MODE
        ):
            raise RuntimeError("embedded secret entry repair could not be verified")
        os.fsync(fd)
    finally:
        os.close(fd)


def _repair_mesh_process_lock_file(
    directory_fd: int,
    entry: _BoundEntry,
    *,
    directory_device: int,
    target_uid: int,
    target_gid: int,
) -> None:
    fchownat_empty_path(entry.path_fd, 0, 0)
    fd = os.open(entry.name, _read_flags(), dir_fd=directory_fd)
    try:
        opened = os.fstat(fd)
        if not _same_inode(opened, entry.info):
            raise RuntimeError(
                "Mesh process-lock entry changed identity during repair"
            )
        _validate_mesh_process_lock_file(
            opened,
            directory_device=directory_device,
            target_uid=target_uid,
        )
        _validate_mesh_process_lock_content(fd, entry.name)

        current = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_inode(current, entry.info):
            raise RuntimeError(
                "Mesh process-lock entry changed identity during repair"
            )
        os.fchmod(fd, FILE_MODE)
        os.fchown(fd, target_uid, target_gid)
        final = os.fstat(fd)
        if (
            not _same_inode(final, entry.info)
            or not stat.S_ISREG(final.st_mode)
            or final.st_dev != directory_device
            or final.st_nlink != 1
            or final.st_uid != target_uid
            or final.st_gid != target_gid
            or stat.S_IMODE(final.st_mode) != FILE_MODE
        ):
            raise RuntimeError("Mesh process-lock entry repair could not be verified")
        _validate_mesh_process_lock_content(fd, entry.name)
        os.fsync(fd)
    finally:
        os.close(fd)


def _repair_mesh_process_lock_directory(
    parent_directory_fd: int,
    directory: _BoundMeshProcessLockDirectory,
    *,
    directory_device: int,
    target_uid: int,
    target_gid: int,
) -> None:
    for entry in directory.entries:
        _repair_mesh_process_lock_file(
            directory.directory_fd,
            entry,
            directory_device=directory_device,
            target_uid=target_uid,
            target_gid=target_gid,
        )

    current = os.stat(
        directory.name,
        dir_fd=parent_directory_fd,
        follow_symlinks=False,
    )
    if not _same_inode(current, directory.info):
        raise RuntimeError("Mesh process-lock directory changed identity during repair")
    os.fchmod(directory.directory_fd, DIRECTORY_MODE)
    os.fchown(directory.directory_fd, target_uid, target_gid)
    final = os.fstat(directory.directory_fd)
    if (
        not _same_inode(final, directory.info)
        or not stat.S_ISDIR(final.st_mode)
        or final.st_dev != directory_device
        or final.st_uid != target_uid
        or final.st_gid != target_gid
        or stat.S_IMODE(final.st_mode) != DIRECTORY_MODE
    ):
        raise RuntimeError(
            "Mesh process-lock directory repair could not be verified"
        )
    os.fsync(directory.directory_fd)


def initialize_volume() -> None:
    require_supported_linux()
    account = pwd.getpwnam("mcp")
    target_uid = account.pw_uid
    target_gid = account.pw_gid

    path_fd = os.open(SECRETS_DIRECTORY, _path_flags(directory=True))
    directory_fd = -1
    entries: list[_BoundEntry] = []
    mesh_directory: _BoundMeshProcessLockDirectory | None = None
    try:
        bound = os.fstat(path_fd)
        if not stat.S_ISDIR(bound.st_mode):
            raise RuntimeError("embedded secret volume target is not a directory")
        if stat.S_IMODE(bound.st_mode) & 0o500 != 0o500:
            raise RuntimeError(
                "embedded secret directory is not repairable with CAP_CHOWN only"
            )
        require_supported_filesystem(path_fd)

        fchownat_empty_path(path_fd, 0, 0)
        directory_fd = os.open(SECRETS_DIRECTORY, _read_flags(directory=True))
        reopened = os.fstat(directory_fd)
        if not _same_inode(bound, reopened) or not stat.S_ISDIR(reopened.st_mode):
            raise RuntimeError("embedded secret directory changed identity")
        os.fchmod(directory_fd, DIRECTORY_MODE)

        entries, mesh_directory = _bind_children(
            directory_fd,
            directory_device=reopened.st_dev,
            target_uid=target_uid,
        )
        for entry in entries:
            if entry.temporary:
                _remove_temporary(directory_fd, entry)
            else:
                _repair_regular(
                    directory_fd,
                    entry,
                    directory_device=reopened.st_dev,
                    target_uid=target_uid,
                    target_gid=target_gid,
                )

        if mesh_directory is not None:
            _repair_mesh_process_lock_directory(
                directory_fd,
                mesh_directory,
                directory_device=reopened.st_dev,
                target_uid=target_uid,
                target_gid=target_gid,
            )

        os.fchmod(directory_fd, DIRECTORY_MODE)
        os.fchown(directory_fd, target_uid, target_gid)
        final = os.fstat(directory_fd)
        if (
            not _same_inode(final, bound)
            or not stat.S_ISDIR(final.st_mode)
            or final.st_uid != target_uid
            or final.st_gid != target_gid
            or stat.S_IMODE(final.st_mode) != DIRECTORY_MODE
        ):
            raise RuntimeError("embedded secret directory repair could not be verified")
        os.fsync(directory_fd)
    finally:
        for entry in entries:
            os.close(entry.path_fd)
        if mesh_directory is not None:
            _close_mesh_process_lock_directory(mesh_directory)
        if directory_fd >= 0:
            os.close(directory_fd)
        os.close(path_fd)


def main() -> int:
    try:
        initialize_volume()
    except Exception as exc:
        print(f"embedded secret volume initialization failed: {exc}", file=sys.stderr)
        return 1
    print("embedded secret volume initialized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
