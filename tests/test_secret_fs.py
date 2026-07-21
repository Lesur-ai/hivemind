# -*- coding: utf-8 -*-
"""Low-level syscall and filesystem allowlist guards for issue #183."""

from __future__ import annotations

import ctypes

import pytest

from live_mem.core import secret_fs


class _Function:
    def __init__(self, implementation):
        self.implementation = implementation
        self.calls: list[tuple] = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.implementation(*args)


def test_supported_filesystem_allowlist_is_deliberately_narrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for fs_type, label in secret_fs.SUPPORTED_FILESYSTEMS.items():
        monkeypatch.setattr(secret_fs, "filesystem_type", lambda _fd, t=fs_type: t)
        assert secret_fs.require_supported_filesystem(7) == label

    monkeypatch.setattr(secret_fs, "filesystem_type", lambda _fd: 0x6969)  # NFS
    with pytest.raises(secret_fs.SecretFilesystemError, match="unsupported"):
        secret_fs.require_supported_filesystem(7)


def test_fstatfs_reads_only_native_type_from_oversized_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = 0xEF53

    def _fstatfs(_fd, result_pointer):
        ctypes.cast(result_pointer, ctypes.POINTER(ctypes.c_long))[0] = expected
        return 0

    fake = type("_Libc", (), {"fstatfs": _Function(_fstatfs)})()
    monkeypatch.setattr(secret_fs, "require_supported_linux", lambda: None)
    monkeypatch.setattr(secret_fs, "_libc", lambda: fake)
    assert secret_fs.filesystem_type(42) == expected
    assert fake.fstatfs.calls[0][0] == 42


def test_fchownat_empty_path_binds_exact_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = _Function(lambda *_args: 0)
    fake = type("_Libc", (), {"fchownat": function})()
    monkeypatch.setattr(secret_fs, "require_supported_linux", lambda: None)
    monkeypatch.setattr(secret_fs, "_libc", lambda: fake)
    secret_fs.fchownat_empty_path(19, 10001, 999)
    assert function.calls == [(19, b"", 10001, 999, secret_fs.AT_EMPTY_PATH)]


def test_rename_noreplace_uses_same_directory_and_required_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = _Function(lambda *_args: 0)
    fake = type("_Libc", (), {"renameat2": function})()
    monkeypatch.setattr(secret_fs, "require_supported_linux", lambda: None)
    monkeypatch.setattr(secret_fs, "_libc", lambda: fake)
    secret_fs.rename_noreplace(23, ".tok.tmp-abc", "tok")
    assert function.calls == [
        (23, b".tok.tmp-abc", 23, b"tok", secret_fs.RENAME_NOREPLACE)
    ]


def test_reserved_temp_grammar_rejects_near_misses() -> None:
    basename = "long_embedded_token"
    assert secret_fs.is_temp_name(
        basename, f"{secret_fs.temp_prefix(basename)}{'a' * 32}"
    )
    for candidate in (
        f"{secret_fs.temp_prefix(basename)}{'a' * 31}",
        f"{secret_fs.temp_prefix(basename)}{'A' * 32}",
        f"{secret_fs.temp_prefix(basename)}{'a' * 32}.bak",
    ):
        assert not secret_fs.is_temp_name(basename, candidate)
