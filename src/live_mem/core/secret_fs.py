# -*- coding: utf-8 -*-
"""Low-level Linux filesystem guards for the embedded runtime credential.

The plaintext credential is local-only security state.  File-backed resolution
therefore supports only a deliberately small set of local Linux filesystems and
uses descriptor-bound syscalls for publication and ownership repair.  The
explicit environment override is resolved before this module is imported.
"""

from __future__ import annotations

import ctypes
import errno
import os
import re
import sys
from functools import lru_cache


class SecretFilesystemError(RuntimeError):
    """The configured local secret filesystem cannot be used safely."""


# Linux filesystem magic values from linux/magic.h.  Keep this allowlist small:
# adding a filesystem changes the durability/locking support contract and must
# arrive with runtime evidence.
SUPPORTED_FILESYSTEMS: dict[int, str] = {
    0xEF53: "ext2/3/4",
    0x58465342: "xfs",
    0x9123683E: "btrfs",
    0x01021994: "tmpfs",
    0x794C7630: "overlayfs",
}

AT_EMPTY_PATH = 0x1000
RENAME_NOREPLACE = 1
_STATFS_BUFFER_BYTES = 256


def lock_name(basename: str) -> str:
    """Return the reserved sibling lock name for one credential file."""
    return f".{basename}.lock"


def temp_prefix(basename: str) -> str:
    """Return the reserved sibling prefix for unpublished credential files."""
    return f".{basename}.tmp-"


def is_temp_name(basename: str, candidate: str) -> bool:
    """Match only the reviewed 128-bit lowercase-hex temporary grammar."""
    return bool(
        re.fullmatch(rf"{re.escape(temp_prefix(basename))}[0-9a-f]{{32}}", candidate)
    )


def require_supported_linux() -> None:
    """Reject platforms outside the reviewed 64-bit Linux syscall contract."""
    if sys.platform != "linux" or ctypes.sizeof(ctypes.c_long) != 8:
        raise SecretFilesystemError(
            "file-backed embedded credentials require supported 64-bit Linux; "
            "use LONG_EMBEDDED_TOKEN for a fileless override"
        )


@lru_cache(maxsize=1)
def _libc() -> ctypes.CDLL:
    require_supported_linux()
    return ctypes.CDLL(None, use_errno=True)


def _raise_oserror(call: str) -> None:
    error_number = ctypes.get_errno() or errno.EIO
    raise OSError(error_number, f"{call} failed: {os.strerror(error_number)}")


def filesystem_type(fd: int) -> int:
    """Return ``f_type`` from Linux ``fstatfs(fd)`` using a bounded buffer."""
    require_supported_linux()
    result = ctypes.create_string_buffer(_STATFS_BUFFER_BYTES)
    function = _libc().fstatfs
    function.argtypes = [ctypes.c_int, ctypes.c_void_p]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    if function(fd, ctypes.byref(result)) != 0:
        _raise_oserror("fstatfs")
    return int(ctypes.c_long.from_buffer_copy(result.raw).value)


def require_supported_filesystem(fd: int) -> str:
    """Require an explicitly supported local filesystem for ``fd``."""
    fs_type = filesystem_type(fd)
    label = SUPPORTED_FILESYSTEMS.get(fs_type)
    if label is None:
        raise SecretFilesystemError(
            "embedded credential directory uses unsupported filesystem type "
            f"0x{fs_type:x}; use a supported Docker named volume or the "
            "fileless LONG_EMBEDDED_TOKEN override"
        )
    return label


def rename_noreplace(
    directory_fd: int, source_name: str, destination_name: str
) -> None:
    """Atomically publish one sibling without replacing an existing name."""
    require_supported_linux()
    try:
        function = _libc().renameat2
    except AttributeError as exc:
        raise SecretFilesystemError(
            "libc renameat2 is unavailable; embedded credential publication "
            "cannot be made atomic"
        ) from exc
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    if (
        function(
            directory_fd,
            os.fsencode(source_name),
            directory_fd,
            os.fsencode(destination_name),
            RENAME_NOREPLACE,
        )
        != 0
    ):
        _raise_oserror("renameat2(RENAME_NOREPLACE)")


def fchownat_empty_path(fd: int, uid: int, gid: int) -> None:
    """Change ownership of the inode bound to an ``O_PATH`` descriptor."""
    require_supported_linux()
    function = _libc().fchownat
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_int,
    ]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    if function(fd, b"", uid, gid, AT_EMPTY_PATH) != 0:
        _raise_oserror("fchownat(AT_EMPTY_PATH)")
