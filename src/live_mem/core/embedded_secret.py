# -*- coding: utf-8 -*-
"""Durable local credential resolution for the embedded long runtime.

Resolution order remains explicit environment override, secure local file,
then generation.  Unlike the historical best-effort cache, generation succeeds
only after the plaintext is durably and atomically published.  File-backed use
is a reviewed 64-bit Linux/local-filesystem contract; the environment override
returns before any Unix-only helper is imported.
"""

from __future__ import annotations

import errno
import logging
import os
import secrets
import stat
from collections.abc import Callable
from typing import Optional

from .models import EMBEDDED_TOKEN_SENTINEL

logger = logging.getLogger("live_mem.embedded_secret")

_TOKEN_PREFIX = "lm_"
_MAX_SECRET_BYTES = 4096
_FILE_MODE = 0o600
_DIRECTORY_MODE = 0o700


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _read_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _validate_directory(info: os.stat_result) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("embedded credential parent is not a directory")
    if info.st_uid != os.geteuid():
        raise RuntimeError("embedded credential directory has an unsafe owner")
    if stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE:
        raise RuntimeError("embedded credential directory must use mode 0700")


def _validate_regular(
    info: os.stat_result,
    *,
    directory_device: int,
    require_size: bool,
) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("embedded credential entry is not a regular file")
    if info.st_dev != directory_device:
        raise RuntimeError("embedded credential entry crosses filesystem boundary")
    if info.st_uid != os.geteuid():
        raise RuntimeError("embedded credential entry has an unsafe owner")
    if stat.S_IMODE(info.st_mode) != _FILE_MODE:
        raise RuntimeError("embedded credential entry must use mode 0600")
    if info.st_nlink != 1:
        raise RuntimeError("embedded credential entry has an unsafe link count")
    if require_size and not (0 < info.st_size <= _MAX_SECRET_BYTES):
        raise RuntimeError("embedded credential file has an invalid size")


def _read_secret_fd(fd: int) -> str:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(1024, _MAX_SECRET_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_SECRET_BYTES:
            raise RuntimeError("embedded credential file is too large")
    value = b"".join(chunks).decode("utf-8", errors="strict").strip()
    if not value:
        raise RuntimeError("embedded credential file is empty")
    if value == EMBEDDED_TOKEN_SENTINEL:
        raise RuntimeError("embedded credential file contains the reserved sentinel")
    return value


def _open_existing_secret(
    directory_fd: int,
    basename: str,
    directory_device: int,
) -> tuple[str, os.stat_result] | None:
    try:
        fd = os.open(basename, _read_flags(), dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        _validate_regular(
            info, directory_device=directory_device, require_size=True
        )
        value = _read_secret_fd(fd)
        return value, info
    finally:
        os.close(fd)


def _open_lock(directory_fd: int, basename: str, directory_device: int) -> int:
    from .secret_fs import lock_name

    name = lock_name(basename)
    flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    created = False
    try:
        fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, _FILE_MODE, dir_fd=directory_fd)
        created = True
    except FileExistsError:
        fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        if created:
            os.fchmod(fd, _FILE_MODE)
        info = os.fstat(fd)
        _validate_regular(
            info, directory_device=directory_device, require_size=False
        )
        if created:
            os.fsync(fd)
            os.fsync(directory_fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _unlink_stable_temp(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
) -> None:
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not _same_inode(current, expected):
        raise RuntimeError("embedded credential temporary entry changed identity")
    os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _cleanup_orphan_temps(
    directory_fd: int,
    basename: str,
    directory_device: int,
) -> None:
    from .secret_fs import is_temp_name, temp_prefix

    prefix = temp_prefix(basename)
    for name in sorted(os.listdir(directory_fd)):
        if not name.startswith(prefix):
            continue
        if not is_temp_name(basename, name):
            raise RuntimeError("malformed embedded credential temporary entry")
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_regular(
            before, directory_device=directory_device, require_size=False
        )
        fd = os.open(name, _read_flags(), dir_fd=directory_fd)
        try:
            opened = os.fstat(fd)
            _validate_regular(
                opened, directory_device=directory_device, require_size=False
            )
            if not _same_inode(before, opened):
                raise RuntimeError(
                    "embedded credential temporary entry changed identity"
                )
            _unlink_stable_temp(directory_fd, name, opened)
        finally:
            os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "embedded credential write made no progress")
        offset += written


def _generate_and_publish(
    directory_fd: int,
    basename: str,
    directory_device: int,
    *,
    after_temp_fsync: Callable[[], None] | None = None,
    after_publish: Callable[[], None] | None = None,
) -> str:
    from .secret_fs import rename_noreplace, temp_prefix

    generated = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    payload = generated.encode("utf-8")
    temp_name = temp_prefix(basename) + secrets.token_hex(16)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    temp_fd = os.open(temp_name, flags, _FILE_MODE, dir_fd=directory_fd)
    temp_info: os.stat_result | None = None
    published = False
    cleaned = False
    try:
        os.fchmod(temp_fd, _FILE_MODE)
        temp_info = os.fstat(temp_fd)
        _validate_regular(
            temp_info, directory_device=directory_device, require_size=False
        )
        _write_all(temp_fd, payload)
        os.fsync(temp_fd)
        if after_temp_fsync is not None:
            after_temp_fsync()

        try:
            rename_noreplace(directory_fd, temp_name, basename)
        except FileExistsError:
            winner = _open_existing_secret(directory_fd, basename, directory_device)
            if winner is None:
                raise RuntimeError("embedded credential publication winner vanished")
            _unlink_stable_temp(directory_fd, temp_name, temp_info)
            cleaned = True
            return winner[0]

        published = True
        if after_publish is not None:
            after_publish()
        os.fsync(directory_fd)

        winner = _open_existing_secret(directory_fd, basename, directory_device)
        if winner is None or not _same_inode(winner[1], temp_info):
            raise RuntimeError("embedded credential publication changed identity")
        return winner[0]
    finally:
        os.close(temp_fd)
        if not published and not cleaned:
            try:
                current = os.stat(
                    temp_name, dir_fd=directory_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                pass
            else:
                if temp_info is None or not _same_inode(current, temp_info):
                    raise RuntimeError(
                        "embedded credential temporary cleanup changed identity"
                    )
                _unlink_stable_temp(directory_fd, temp_name, temp_info)


def _resolve_file_token(
    path: str,
    *,
    generate: bool,
    after_temp_fsync: Callable[[], None] | None = None,
    after_publish: Callable[[], None] | None = None,
) -> str | None:
    from .secret_fs import (
        lock_name,
        require_supported_filesystem,
        require_supported_linux,
    )

    require_supported_linux()
    absolute = os.path.abspath(path)
    parent = os.path.dirname(absolute)
    basename = os.path.basename(absolute)
    if not basename or basename in {".", ".."}:
        raise RuntimeError("embedded credential file path is invalid")

    directory_fd = os.open(parent, _directory_flags())
    try:
        directory_info = os.fstat(directory_fd)
        _validate_directory(directory_info)
        require_supported_filesystem(directory_fd)

        existing = _open_existing_secret(
            directory_fd, basename, directory_info.st_dev
        )
        if existing is not None:
            return existing[0]
        if not generate:
            return None

        import fcntl

        lock_fd = _open_lock(directory_fd, basename, directory_info.st_dev)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            lock_info = os.fstat(lock_fd)
            lock_entry = os.stat(
                lock_name(basename),
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if not _same_inode(lock_info, lock_entry):
                raise RuntimeError("embedded credential lock changed identity")

            _cleanup_orphan_temps(
                directory_fd, basename, directory_info.st_dev
            )
            existing = _open_existing_secret(
                directory_fd, basename, directory_info.st_dev
            )
            if existing is not None:
                return existing[0]
            return _generate_and_publish(
                directory_fd,
                basename,
                directory_info.st_dev,
                after_temp_fsync=after_temp_fsync,
                after_publish=after_publish,
            )
        finally:
            os.close(lock_fd)
    finally:
        os.close(directory_fd)


def resolve_embedded_token(settings=None, *, generate: bool = True) -> Optional[str]:
    """Resolve the embedded bearer, returning ``None`` on unsafe local state."""
    if settings is None:
        from ..config import get_settings

        settings = get_settings()

    env_raw = (getattr(settings, "long_embedded_token", "") or "").strip()
    if env_raw:
        if env_raw == EMBEDDED_TOKEN_SENTINEL:
            logger.error("LONG_EMBEDDED_TOKEN uses the reserved sentinel")
            return None
        return env_raw

    path = (getattr(settings, "long_embedded_token_file", "") or "").strip()
    if not path:
        logger.error("LONG_EMBEDDED_TOKEN_FILE is required when no override is set")
        return None

    try:
        return _resolve_file_token(path, generate=generate)
    except (OSError, RuntimeError, UnicodeError) as exc:
        logger.error("Embedded credential file is unavailable (%s): %s", path, exc)
        return None
