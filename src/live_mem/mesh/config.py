# -*- coding: utf-8 -*-
"""Default-on, fail-closed Project Mesh configuration.

Only the feature flag is read eagerly.  The enabled configuration is built
later, is immutable, and is never a Pydantic model so its private signing
capability cannot enter ``model_dump`` or JSON serialization paths.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from dotenv import dotenv_values

from .destination import (
    MeshDestination,
    MeshDestinationError,
    is_public_mesh_address,
)
from .identity import (
    MeshIdentityError,
    MeshPrivateKey,
    mesh_identity_fingerprint,
    mesh_public_key_from_private,
    parse_mesh_private_key,
)


MESH_ENABLED_ENV: Final = "HIVEMIND_MESH_ENABLED"
MESH_PUBLIC_URL_ENV: Final = "HIVEMIND_MESH_PUBLIC_URL"
MESH_PRIVATE_KEY_ENV: Final = "HIVEMIND_MESH_PRIVATE_KEY"
MESH_DISPLAY_NAME_ENV: Final = "HIVEMIND_MESH_DISPLAY_NAME"
MESH_INVITATION_TTL_ENV: Final = "HIVEMIND_MESH_INVITATION_TTL_SECONDS"
MESH_CONTROL_MAX_BYTES_ENV: Final = "HIVEMIND_MESH_CONTROL_MAX_BYTES"
MESH_BOOTSTRAP_MAX_BYTES_ENV: Final = "HIVEMIND_MESH_BOOTSTRAP_MAX_BYTES"
MESH_BOOTSTRAP_MAX_OBJECTS_ENV: Final = "HIVEMIND_MESH_BOOTSTRAP_MAX_OBJECTS"

DEFAULT_MESH_INVITATION_TTL_SECONDS: Final = 3600
DEFAULT_MESH_CONTROL_MAX_BYTES: Final = 262_144
DEFAULT_MESH_BOOTSTRAP_MAX_BYTES: Final = 268_435_456
DEFAULT_MESH_BOOTSTRAP_MAX_OBJECTS: Final = 50_000
MAX_MESH_DISPLAY_NAME_UTF8_BYTES: Final = 128

_CANONICAL_POSITIVE_INTEGER: Final = re.compile(r"^[1-9][0-9]*$")
_MESH_ENV_NAMES: Final = (
    MESH_ENABLED_ENV,
    MESH_PUBLIC_URL_ENV,
    MESH_PRIVATE_KEY_ENV,
    MESH_DISPLAY_NAME_ENV,
    MESH_INVITATION_TTL_ENV,
    MESH_CONTROL_MAX_BYTES_ENV,
    MESH_BOOTSTRAP_MAX_BYTES_ENV,
    MESH_BOOTSTRAP_MAX_OBJECTS_ENV,
)


class MeshConfigError(RuntimeError):
    """Mesh configuration is unsafe; values are never echoed in messages."""


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def load_mesh_environment(dotenv_path: str = ".env") -> dict[str, str]:
    """Load only the eight Mesh variables with process-env precedence.

    This function is called only after the public feature gate is true.  It
    deliberately returns a short-lived plain mapping rather than adding the
    private key or other Mesh values to global Pydantic Settings.
    """

    if type(dotenv_path) is not str or not dotenv_path:
        raise MeshConfigError("Mesh environment file path is invalid")
    try:
        file_values = dotenv_values(
            dotenv_path=dotenv_path,
            encoding="utf-8",
            interpolate=True,
        )
    except (OSError, UnicodeError, ValueError):
        raise MeshConfigError("Mesh environment file is invalid") from None
    selected: dict[str, str] = {
        name: file_values[name] if file_values[name] is not None else ""
        for name in _MESH_ENV_NAMES
        if name in file_values
    }
    for name in _MESH_ENV_NAMES:
        if name in os.environ:
            selected[name] = os.environ[name]
    return selected


def is_mesh_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Strictly parse the only eagerly-read Mesh setting.

    Missing means enabled. Operators that do not run Project Mesh must set the
    canonical lowercase value ``false`` explicitly; any other value is refused.
    """

    value = _environment(environ).get(MESH_ENABLED_ENV, "true")
    if type(value) is not str:
        raise MeshConfigError(
            f"{MESH_ENABLED_ENV} must be exactly 'true' or 'false'"
        )
    if value == "true":
        return True
    if value == "false":
        return False
    raise MeshConfigError(
        f"{MESH_ENABLED_ENV} must be exactly 'true' or 'false'"
    )


# Intentional eager feature gate.  No other Mesh variable is touched here.
MESH_ENABLED: Final[bool] = is_mesh_enabled()


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if type(value) is not str or not value or value != value.strip():
        raise MeshConfigError(f"{name} is required and must be canonical")
    return value


def _canonical_positive_int(
    environ: Mapping[str, str],
    name: str,
    default: int,
    maximum: int,
) -> int:
    raw = environ.get(name, str(default))
    if type(raw) is not str or _CANONICAL_POSITIVE_INTEGER.fullmatch(raw) is None:
        raise MeshConfigError(f"{name} must be a canonical positive integer")
    value = int(raw)
    if not 1 <= value <= maximum:
        raise MeshConfigError(f"{name} must be in range 1..{maximum}")
    return value


def _validate_display_name(value: str) -> str:
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise MeshConfigError(f"{MESH_DISPLAY_NAME_ENV} is invalid") from None
    if (
        not encoded
        or len(encoded) > MAX_MESH_DISPLAY_NAME_UTF8_BYTES
        or unicodedata.normalize("NFC", value) != value
        or any(
            unicodedata.category(character).startswith("C")
            or character in {"\u2028", "\u2029"}
            for character in value
        )
    ):
        raise MeshConfigError(
            f"{MESH_DISPLAY_NAME_ENV} must be canonical printable text of at most "
            f"{MAX_MESH_DISPLAY_NAME_UTF8_BYTES} UTF-8 bytes"
        )
    return value


def _validate_public_url(value: str) -> str:
    try:
        destination = MeshDestination.parse(value)
    except MeshDestinationError:
        raise MeshConfigError(f"{MESH_PUBLIC_URL_ENV} is invalid") from None
    if destination.scheme != "https":  # Production parser is HTTPS-only.
        raise MeshConfigError(
            f"{MESH_PUBLIC_URL_ENV} must be an HTTPS origin without "
            "credentials, path, query, or fragment"
        )
    if destination.literal_ip is not None and not is_public_mesh_address(
        destination.literal_ip
    ):
        raise MeshConfigError(f"{MESH_PUBLIC_URL_ENV} is invalid")
    return destination.canonical_url


@dataclass(frozen=True, slots=True)
class MeshEnabledConfig:
    """Immutable configuration that exists only when Mesh is enabled."""

    enabled: bool
    public_url: str
    display_name: str
    private_key: MeshPrivateKey = field(repr=False, compare=False)
    public_key: str
    fingerprint: str
    invitation_ttl_seconds: int
    control_max_bytes: int
    bootstrap_max_bytes: int
    bootstrap_max_objects: int

    def __post_init__(self) -> None:
        if self.enabled is not True:
            raise MeshConfigError("MeshEnabledConfig requires enabled=true")

    def __repr__(self) -> str:
        return (
            "MeshEnabledConfig("
            "enabled=True, "
            f"public_url={self.public_url!r}, "
            f"display_name={self.display_name!r}, "
            "private_key=<redacted>, "
            f"public_key={self.public_key!r}, "
            f"fingerprint={self.fingerprint!r}, "
            f"invitation_ttl_seconds={self.invitation_ttl_seconds!r}, "
            f"control_max_bytes={self.control_max_bytes!r}, "
            f"bootstrap_max_bytes={self.bootstrap_max_bytes!r}, "
            f"bootstrap_max_objects={self.bootstrap_max_objects!r})"
        )

    def __copy__(self) -> "MeshEnabledConfig":
        raise TypeError("MeshEnabledConfig containing a private key cannot be copied")

    def __deepcopy__(self, memo: object) -> "MeshEnabledConfig":
        del memo
        raise TypeError("MeshEnabledConfig containing a private key cannot be copied")


def load_mesh_config(
    environ: Mapping[str, str] | None = None,
) -> MeshEnabledConfig | None:
    """Lazily construct enabled Mesh configuration, or return ``None``.

    With no explicit mapping the eager process feature flag is authoritative.
    Passing a mapping is an injection seam for deterministic startup tests.
    Disabled mode deliberately ignores all seven non-gate Mesh variables.
    """

    source = _environment(environ)
    enabled = MESH_ENABLED if environ is None else is_mesh_enabled(source)
    if not enabled:
        return None

    public_url = _validate_public_url(_required(source, MESH_PUBLIC_URL_ENV))
    display_name = _validate_display_name(_required(source, MESH_DISPLAY_NAME_ENV))
    encoded_private_key = _required(source, MESH_PRIVATE_KEY_ENV)
    try:
        private_key = parse_mesh_private_key(encoded_private_key)
    except MeshIdentityError:
        raise MeshConfigError(f"{MESH_PRIVATE_KEY_ENV} is invalid") from None

    invitation_ttl_seconds = _canonical_positive_int(
        source,
        MESH_INVITATION_TTL_ENV,
        DEFAULT_MESH_INVITATION_TTL_SECONDS,
        DEFAULT_MESH_INVITATION_TTL_SECONDS,
    )
    if invitation_ttl_seconds != DEFAULT_MESH_INVITATION_TTL_SECONDS:
        raise MeshConfigError(f"{MESH_INVITATION_TTL_ENV} must be exactly 3600")

    public_key = mesh_public_key_from_private(private_key)
    return MeshEnabledConfig(
        enabled=True,
        public_url=public_url,
        display_name=display_name,
        private_key=private_key,
        public_key=public_key,
        fingerprint=mesh_identity_fingerprint(public_key),
        invitation_ttl_seconds=invitation_ttl_seconds,
        control_max_bytes=_canonical_positive_int(
            source,
            MESH_CONTROL_MAX_BYTES_ENV,
            DEFAULT_MESH_CONTROL_MAX_BYTES,
            DEFAULT_MESH_CONTROL_MAX_BYTES,
        ),
        bootstrap_max_bytes=_canonical_positive_int(
            source,
            MESH_BOOTSTRAP_MAX_BYTES_ENV,
            DEFAULT_MESH_BOOTSTRAP_MAX_BYTES,
            DEFAULT_MESH_BOOTSTRAP_MAX_BYTES,
        ),
        bootstrap_max_objects=_canonical_positive_int(
            source,
            MESH_BOOTSTRAP_MAX_OBJECTS_ENV,
            DEFAULT_MESH_BOOTSTRAP_MAX_OBJECTS,
            DEFAULT_MESH_BOOTSTRAP_MAX_OBJECTS,
        ),
    )
