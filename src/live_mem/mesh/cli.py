# -*- coding: utf-8 -*-
"""Local-only Project Mesh identity generation command."""

from __future__ import annotations

import argparse
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .identity import MeshIdentity, _write_generated_mesh_identity


class MeshKeygenError(RuntimeError):
    """Atomic key-file generation failed without exposing secret material."""


@dataclass(frozen=True, slots=True)
class MeshKeygenResult:
    """Safe key-generation result; intentionally excludes the private key."""

    public_key: str
    fingerprint: str
    path: str


def _validate_created_file(fd: int) -> None:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise MeshKeygenError("created key output is not a regular file")
    if metadata.st_nlink != 1:
        raise MeshKeygenError("created key output must have exactly one link")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise MeshKeygenError("created key output mode must be 0600")


def _unlink_created_file(
    parent_fd: int,
    name: str,
    identity: tuple[int, int] | None,
) -> None:
    """Best-effort cleanup without unlinking an attacker-replaced pathname."""

    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if identity is not None and (current.st_dev, current.st_ino) != identity:
            return
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileNotFoundError:
        return


def _write_private_key_file(path: Path) -> MeshIdentity:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
    )
    fd = -1
    created_identity: tuple[int, int] | None = None
    try:
        fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        metadata = os.fstat(fd)
        created_identity = (metadata.st_dev, metadata.st_ino)
        os.fchmod(fd, 0o600)
        _validate_created_file(fd)

        identity = _write_generated_mesh_identity(fd)
        os.fsync(fd)
        _validate_created_file(fd)
        pathname_metadata = os.stat(
            path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            (pathname_metadata.st_dev, pathname_metadata.st_ino)
            != created_identity
            or not stat.S_ISREG(pathname_metadata.st_mode)
            or pathname_metadata.st_nlink != 1
            or stat.S_IMODE(pathname_metadata.st_mode) != 0o600
        ):
            raise MeshKeygenError("created key output changed during creation")
        os.fsync(parent_fd)
        return identity
    except BaseException:
        if fd >= 0:
            if created_identity is None:
                try:
                    metadata = os.fstat(fd)
                    created_identity = (metadata.st_dev, metadata.st_ino)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass
            finally:
                fd = -1
        if created_identity is not None:
            _unlink_created_file(parent_fd, path.name, created_identity)
        raise
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.close(parent_fd)
        except OSError:
            pass


def write_mesh_identity_file(output: str | os.PathLike[str]) -> MeshKeygenResult:
    """Create one new 0600 key file atomically and return safe metadata."""

    path = Path(output)
    if not path.name or path.name in (".", ".."):
        raise MeshKeygenError("key output must name a file")
    try:
        identity = _write_private_key_file(path)
    except FileExistsError:
        raise MeshKeygenError("refusing to replace existing key output") from None
    except MeshKeygenError:
        raise
    except OSError as exc:
        raise MeshKeygenError(
            f"unable to create key output ({exc.__class__.__name__})"
        ) from None
    return MeshKeygenResult(
        public_key=identity.public_key,
        fingerprint=identity.fingerprint,
        path=os.fspath(path),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m live_mem mesh-keygen",
        description="Generate a local Project Mesh Ed25519 identity.",
    )
    parser.add_argument("--output", required=True, metavar="PATH")
    return parser


def mesh_keygen_main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint whose stdout contains safe public metadata only."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        result = write_mesh_identity_file(arguments.output)
    except MeshKeygenError as exc:
        parser.error(str(exc))
    print(f"public_key={result.public_key}")
    print(f"fingerprint={result.fingerprint}")
    print(f"path={result.path}")
    return 0
