# -*- coding: utf-8 -*-
"""Split role configuration and the strict ``LLMAAS_*`` 1.x legacy path.

Selection uses raw environment-variable presence (ADR-0027):

1. if any ``INFERENCE_CHAT_*`` or ``INFERENCE_EMBEDDING_*`` variable is
   present — INCLUDING a present empty value — the new configuration path is
   selected exclusively;
2. if any ``LLMAAS_*`` variable is then also present, configuration fails
   before any network access (no per-field legacy fallback);
3. if no new variable is present, the historical legacy path activates only
   with the complete non-empty ``LLMAAS_API_URL`` + ``LLMAAS_API_KEY`` pair
   (both empty/absent preserves the historical "inference unconfigured"
   startup, exactly like 1.x);
4. historical optional legacy tunables retain their 1.x defaults;
5. a partial legacy pair, an empty required value, a mixed family, or an
   unknown provider fails before network access.

Environment names are matched case-insensitively, mirroring the historical
``pydantic-settings`` (``case_sensitive=False``) behavior both services always
had for ``LLMAAS_*``.

Every diagnostic echoes variable NAMES and safe constraints only — never a
configured value, URL, or key.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Mapping

from .profiles import (
    MAX_CHAT_GENERATION_TOKENS,
    ResolvedChatProfile,
    ResolvedEmbeddingProfile,
)
from .registry import (
    ADAPTER_TEMPERATURE_RANGES,
    CHAT_PROVIDER_IDS,
    EMBEDDING_PROVIDER_IDS,
    PROVIDER_TO_ADAPTER,
    adapter_for_provider,
    validate_endpoint,
)

_logger = logging.getLogger("hivemind_inference.config")

_CHAT_PREFIX = "INFERENCE_CHAT_"
_EMBEDDING_PREFIX = "INFERENCE_EMBEDDING_"
_INFERENCE_PREFIX = "INFERENCE_"
_LEGACY_PREFIX = "LLMAAS_"

CHAT_REQUIRED_VARIABLES: tuple[str, ...] = (
    "INFERENCE_CHAT_PROVIDER",
    "INFERENCE_CHAT_API_URL",
    "INFERENCE_CHAT_API_KEY",
    "INFERENCE_CHAT_MODEL",
    "INFERENCE_CHAT_CONTEXT_WINDOW",
    "INFERENCE_CHAT_MAX_OUTPUT_TOKENS",
)
CHAT_OPTIONAL_VARIABLES: tuple[str, ...] = ("INFERENCE_CHAT_TEMPERATURE",)
EMBEDDING_REQUIRED_VARIABLES: tuple[str, ...] = (
    "INFERENCE_EMBEDDING_PROVIDER",
    "INFERENCE_EMBEDDING_API_URL",
    "INFERENCE_EMBEDDING_API_KEY",
    "INFERENCE_EMBEDDING_MODEL",
    "INFERENCE_EMBEDDING_DIMENSIONS",
)

# Frozen 1.x legacy defaults (ADR-0027 table).
LEGACY_DEFAULT_MODEL = "qwen3.5:27b"
LEGACY_DEFAULT_CONTEXT_WINDOW = 131072
LEGACY_DEFAULT_MAX_TOKENS = 16384
LEGACY_DEFAULT_TEMPERATURE = 0.3
LEGACY_DEFAULT_EMBEDDING_MODEL = "bge-m3:567m"
LEGACY_DEFAULT_EMBEDDING_DIMENSIONS = 1024

LEGACY_KNOWN_VARIABLES: tuple[str, ...] = (
    "LLMAAS_API_URL",
    "LLMAAS_API_KEY",
    "LLMAAS_MODEL",
    "LLMAAS_CONTEXT_WINDOW",
    "LLMAAS_MAX_TOKENS",
    "LLMAAS_TEMPERATURE",
    "LLMAAS_EMBEDDING_MODEL",
    "LLMAAS_EMBEDDING_DIMENSIONS",
)

# ASCII base-10 only: ``\d`` also matches Unicode decimal digits (e.g.
# Arabic-Indic ``١٢٣``), which ``int()`` would then accept — silently violating
# the documented base-10 integer contract. Pin to ``[0-9]`` so a configured
# integer is exactly ASCII base-10 or fails closed with a value-free error.
_POSITIVE_INT_RE = re.compile(r"^[0-9]+$")

# Reject an implausibly long digit run BEFORE int(): the CPython integer-string
# conversion limit (~4300 digits) would otherwise escape _parse_positive_int as
# a raw ValueError. No real context/output/dimension value approaches this, so
# 18 digits (up to 10**18) is generous headroom while staying value-free.
_MAX_CONFIG_INT_DIGITS = 18


class InferenceConfigError(ValueError):
    """Aggregated field-level, secret-free configuration failure."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__(
            "Inference configuration errors:\n  - " + "\n  - ".join(self.errors)
        )


@dataclass(frozen=True, slots=True)
class InferenceConfig:
    """Outcome of one resolution: per-role immutable profiles.

    A wholly absent role resolves to ``None`` and is reported as
    unavailable/not-configured by health surfaces; operations on it fail
    closed at call time.
    """

    chat: ResolvedChatProfile | None
    embedding: ResolvedEmbeddingProfile | None
    legacy_active: bool

    @property
    def configured_roles(self) -> tuple[str, ...]:
        roles: list[str] = []
        if self.chat is not None:
            roles.append("chat")
        if self.embedding is not None:
            roles.append("embedding")
        return tuple(roles)


def merged_environment(env_file: str | None = ".env") -> dict[str, str]:
    """Process environment overlaid on the optional ``.env`` file.

    Mirrors the precedence both services already use through
    ``pydantic-settings`` (real environment wins over the file). Presence in
    either source counts as raw presence for the ADR-0027 selection rules,
    including present-empty values.
    """
    merged: dict[str, str] = {}
    if env_file:
        try:
            from dotenv import dotenv_values

            for key, value in dotenv_values(env_file).items():
                merged[key] = "" if value is None else str(value)
        except ImportError:  # pragma: no cover - python-dotenv ships with both services
            pass
    merged.update(os.environ)
    return merged


_legacy_deprecation_emitted = False


def _emit_legacy_deprecation_warning_once() -> None:
    global _legacy_deprecation_emitted
    if _legacy_deprecation_emitted:
        return
    _legacy_deprecation_emitted = True
    _logger.warning(
        "LLMAAS_* configuration is deprecated: it remains supported through "
        "every 1.x release, but new deployments should migrate to the split "
        "INFERENCE_CHAT_* / INFERENCE_EMBEDDING_* families before 2.0"
    )


def _reset_legacy_deprecation_warning_for_tests() -> None:
    global _legacy_deprecation_emitted
    _legacy_deprecation_emitted = False


def _normalized_presence(environ: Mapping[str, str]) -> dict[str, str]:
    """Upper-cased view of the environment (last case-variant wins)."""
    normalized: dict[str, str] = {}
    for key, value in environ.items():
        if isinstance(key, str):
            normalized[key.upper()] = "" if value is None else str(value)
    return normalized


def _case_variant_collisions(environ: Mapping[str, str]) -> list[str]:
    """Canonical inference/legacy names present under more than one spelling.

    Environment variables are case-sensitive on Linux/macOS, so both
    ``LLMAAS_API_URL`` and ``llmaas_api_url`` can coexist. Case-insensitive
    resolution then silently keeps only one mapping. ANY canonical
    inference/legacy name appearing under more than one case-spelling is
    ambiguous operator intent and must fail closed — even when the duplicated
    values happen to match, since which spelling is later changed would silently
    flip behavior. A single spelling (in any case) is not a collision, so the
    historical case-insensitive resolution of one name is preserved. Only the
    inference/legacy families matter; unrelated variables are ignored.
    """
    spellings: dict[str, set[str]] = {}
    for key in environ:
        if not isinstance(key, str):
            continue
        upper = key.upper()
        if not upper.startswith((_INFERENCE_PREFIX, _LEGACY_PREFIX)):
            continue
        spellings.setdefault(upper, set()).add(key)
    return sorted(name for name, seen in spellings.items() if len(seen) > 1)


def _validate_endpoint_url(
    name: str, value: str, *, provider_id: str, errors: list[str]
) -> bool:
    """Aggregate the shared provider-aware endpoint diagnostics into ``errors``
    (value-free) and report validity.

    The rules live in :func:`registry.validate_endpoint` so configuration
    resolution and direct resolved-profile construction enforce exactly the same
    host/path/scheme policy and cannot drift.
    """
    endpoint_errors = validate_endpoint(name, value, provider_id=provider_id)
    errors.extend(endpoint_errors)
    return not endpoint_errors


def _parse_positive_int(
    name: str, value: str, errors: list[str]
) -> int | None:
    stripped = value.strip()
    # Bound the digit count BEFORE int() so a 4300+-digit value cannot escape as
    # the CPython integer-string digit-limit ValueError; `or` short-circuits so
    # int() only runs on a length-bounded digit string. Format and range are
    # still validated.
    if (
        not _POSITIVE_INT_RE.fullmatch(stripped)
        or len(stripped) > _MAX_CONFIG_INT_DIGITS
        or int(stripped) < 1
    ):
        errors.append(f"{name} must be a positive base-10 integer")
        return None
    return int(stripped)


def _parse_temperature(
    name: str, value: str, *, adapter_id: str, errors: list[str]
) -> float | None:
    try:
        parsed = float(value.strip())
    except ValueError:
        errors.append(f"{name} must be a finite number")
        return None
    if not math.isfinite(parsed):
        errors.append(f"{name} must be a finite number")
        return None
    low, high = ADAPTER_TEMPERATURE_RANGES[adapter_id]
    if not (low <= parsed <= high):
        errors.append(
            f"{name} out of range [{low}, {high}] for the selected profile"
        )
        return None
    return parsed


def _resolve_new_role(
    role: str,
    present: dict[str, str],
    errors: list[str],
) -> ResolvedChatProfile | ResolvedEmbeddingProfile | None:
    if role == "chat":
        prefix = _CHAT_PREFIX
        required = CHAT_REQUIRED_VARIABLES
        optional = CHAT_OPTIONAL_VARIABLES
        allowed_providers = CHAT_PROVIDER_IDS
    else:
        prefix = _EMBEDDING_PREFIX
        required = EMBEDDING_REQUIRED_VARIABLES
        optional = EMBEDDING_REQUIRED_VARIABLES[:0]
        allowed_providers = EMBEDDING_PROVIDER_IDS

    family = {
        name: value for name, value in present.items() if name.startswith(prefix)
    }
    if not family:
        return None

    known = set(required) | set(optional)
    for name in sorted(family):
        if name not in known:
            errors.append(
                f"unknown inference variable {name} (known {role} variables: "
                + ", ".join(sorted(known))
                + ")"
            )

    missing = [name for name in required if name not in family]
    empty = [name for name in required if family.get(name, "absent") == ""]
    if missing:
        errors.append(
            f"incomplete {role} role: missing " + ", ".join(missing)
            + " (each role is all-or-nothing)"
        )
    if empty:
        errors.append(
            f"incomplete {role} role: empty value for " + ", ".join(empty)
        )
    if missing or empty or any(name not in known for name in family):
        return None

    provider_id = family[f"{prefix}PROVIDER"].strip()
    if provider_id not in PROVIDER_TO_ADAPTER:
        errors.append(
            f"{prefix}PROVIDER must be one of: "
            + ", ".join(sorted(PROVIDER_TO_ADAPTER))
        )
        return None
    if provider_id not in allowed_providers:
        errors.append(
            f"provider '{provider_id}' does not support the {role} role"
            + (
                " (Anthropic has no native embedding model — pair a separate"
                " embedding provider)"
                if provider_id == "anthropic" and role == "embedding"
                else ""
            )
        )
        return None
    # The registry is the single provider→adapter resolution authority. The
    # provider/role validity was already aggregated above (so this cannot
    # raise here); delegating keeps the mapping defined in exactly one place.
    adapter_id = adapter_for_provider(provider_id, role=role)

    url = family[f"{prefix}API_URL"].strip()
    url_ok = _validate_endpoint_url(
        f"{prefix}API_URL", url, provider_id=provider_id, errors=errors
    )
    api_key = family[f"{prefix}API_KEY"]
    if not api_key.strip():
        errors.append(f"{prefix}API_KEY must not be blank")
        url_ok = False
    model = family[f"{prefix}MODEL"].strip()
    if not model:
        errors.append(f"{prefix}MODEL must not be blank")
        url_ok = False

    if role == "chat":
        context_window = _parse_positive_int(
            "INFERENCE_CHAT_CONTEXT_WINDOW",
            family["INFERENCE_CHAT_CONTEXT_WINDOW"],
            errors,
        )
        max_output = _parse_positive_int(
            "INFERENCE_CHAT_MAX_OUTPUT_TOKENS",
            family["INFERENCE_CHAT_MAX_OUTPUT_TOKENS"],
            errors,
        )
        temperature: float | None = None
        if "INFERENCE_CHAT_TEMPERATURE" in family:
            raw_temperature = family["INFERENCE_CHAT_TEMPERATURE"]
            if raw_temperature == "":
                errors.append(
                    "INFERENCE_CHAT_TEMPERATURE must not be blank "
                    "(omit the variable to omit the wire parameter)"
                )
                return None
            temperature = _parse_temperature(
                "INFERENCE_CHAT_TEMPERATURE",
                raw_temperature,
                adapter_id=adapter_id,
                errors=errors,
            )
            if temperature is None:
                return None
        if context_window is None or max_output is None or not url_ok:
            return None
        if max_output > MAX_CHAT_GENERATION_TOKENS:
            errors.append(
                "INFERENCE_CHAT_MAX_OUTPUT_TOKENS exceeds Hivemind's supported "
                "generation budget"
            )
            return None
        if max_output >= context_window:
            errors.append(
                "INFERENCE_CHAT_MAX_OUTPUT_TOKENS must be strictly below "
                "INFERENCE_CHAT_CONTEXT_WINDOW (output budget must leave "
                "room for input in the total context window)"
            )
            return None
        return ResolvedChatProfile(
            provider_id=provider_id,
            adapter_id=adapter_id,
            endpoint=url,
            api_key=api_key,
            configured_model=model,
            context_window=context_window,
            max_output_tokens=max_output,
            temperature=temperature,
            source="inference",
        )

    dimensions = _parse_positive_int(
        "INFERENCE_EMBEDDING_DIMENSIONS",
        family["INFERENCE_EMBEDDING_DIMENSIONS"],
        errors,
    )
    if dimensions is None or not url_ok:
        return None
    return ResolvedEmbeddingProfile(
        provider_id=provider_id,
        adapter_id=adapter_id,
        endpoint=url,
        api_key=api_key,
        configured_model=model,
        expected_dimensions=dimensions,
        source="inference",
    )


def _resolve_legacy(
    present: dict[str, str], errors: list[str]
) -> tuple[ResolvedChatProfile | None, ResolvedEmbeddingProfile | None, bool]:
    url = present.get("LLMAAS_API_URL", "")
    key = present.get("LLMAAS_API_KEY", "")
    if not url and not key:
        # Historical 1.x semantics: an absent/empty pair is a VALID
        # "inference unconfigured" startup, never an error.
        return None, None, False
    if bool(url) != bool(key):
        errors.append(
            "LLMaaS partially configured — set both LLMAAS_API_URL and "
            "LLMAAS_API_KEY or neither"
        )
        return None, None, False

    ok = _validate_endpoint_url(
        "LLMAAS_API_URL", url.strip(), provider_id="openai-compatible", errors=errors
    )
    if not key.strip():
        errors.append("LLMAAS_API_KEY must not be blank")
        ok = False

    model = present.get("LLMAAS_MODEL", LEGACY_DEFAULT_MODEL)
    if not model.strip():
        errors.append("LLMAAS_MODEL must not be blank")
        ok = False
    embedding_model = present.get(
        "LLMAAS_EMBEDDING_MODEL", LEGACY_DEFAULT_EMBEDDING_MODEL
    )
    if not embedding_model.strip():
        errors.append("LLMAAS_EMBEDDING_MODEL must not be blank")
        ok = False

    context_window: int | None = LEGACY_DEFAULT_CONTEXT_WINDOW
    if "LLMAAS_CONTEXT_WINDOW" in present:
        context_window = _parse_positive_int(
            "LLMAAS_CONTEXT_WINDOW", present["LLMAAS_CONTEXT_WINDOW"], errors
        )
    max_tokens: int | None = LEGACY_DEFAULT_MAX_TOKENS
    if "LLMAAS_MAX_TOKENS" in present:
        max_tokens = _parse_positive_int(
            "LLMAAS_MAX_TOKENS", present["LLMAAS_MAX_TOKENS"], errors
        )
    temperature: float | None = LEGACY_DEFAULT_TEMPERATURE
    if "LLMAAS_TEMPERATURE" in present:
        temperature = _parse_temperature(
            "LLMAAS_TEMPERATURE",
            present["LLMAAS_TEMPERATURE"],
            adapter_id="openai-compatible",
            errors=errors,
        )
    dimensions: int | None = LEGACY_DEFAULT_EMBEDDING_DIMENSIONS
    if "LLMAAS_EMBEDDING_DIMENSIONS" in present:
        dimensions = _parse_positive_int(
            "LLMAAS_EMBEDDING_DIMENSIONS",
            present["LLMAAS_EMBEDDING_DIMENSIONS"],
            errors,
        )

    if (
        not ok
        or context_window is None
        or max_tokens is None
        or temperature is None
        or dimensions is None
    ):
        return None, None, False
    if max_tokens > MAX_CHAT_GENERATION_TOKENS:
        errors.append(
            "LLMAAS_MAX_TOKENS exceeds Hivemind's supported generation budget"
        )
        return None, None, False
    if max_tokens >= context_window:
        errors.append(
            "LLMAAS_MAX_TOKENS must be strictly less than "
            "LLMAAS_CONTEXT_WINDOW (output budget must leave room for "
            "input in the total context window)"
        )
        return None, None, False

    # One immutable generic openai-compatible effective profile whose URL and
    # key feed both roles; endpoint shape never upgrades that identity to a
    # brand profile.
    chat = ResolvedChatProfile(
        provider_id="openai-compatible",
        adapter_id="openai-compatible",
        endpoint=url.strip(),
        api_key=key,
        configured_model=model.strip(),
        context_window=context_window,
        max_output_tokens=max_tokens,
        temperature=temperature,
        source="llmaas-legacy",
    )
    embedding = ResolvedEmbeddingProfile(
        provider_id="openai-compatible",
        adapter_id="openai-compatible",
        endpoint=url.strip(),
        api_key=key,
        configured_model=embedding_model.strip(),
        expected_dimensions=dimensions,
        source="llmaas-legacy",
    )
    return chat, embedding, True


def resolve_inference_config(environ: Mapping[str, str]) -> InferenceConfig:
    """Resolve one immutable :class:`InferenceConfig` from raw presence.

    Raises :class:`InferenceConfigError` with aggregated field-level,
    secret-free diagnostics on any partial, empty, mixed, unknown, or
    role-incompatible configuration — always before any network access.
    """
    present = _normalized_presence(environ)
    errors: list[str] = []

    # Ambiguous case-variant names make the case-folded view unreliable (it
    # keeps only one value), so fail closed before any family logic. Names only.
    collisions = _case_variant_collisions(environ)
    if collisions:
        errors.append(
            "case-variant environment collision for "
            + ", ".join(collisions)
            + " (the same variable set under more than one case-spelling is "
            "ambiguous; use exactly one canonical name)"
        )
        raise InferenceConfigError(errors)

    # Any INFERENCE_* name signals intent to use split configuration. A name
    # under neither exact role prefix (e.g. a typo like INFERENCE_CHATX_*) is a
    # stray that must fail closed here — otherwise it would be ignored and a
    # valid LLMAAS_* pair would silently activate legacy egress instead.
    inference_present_names = sorted(
        name for name in present if name.startswith(_INFERENCE_PREFIX)
    )
    stray_inference_names = [
        name
        for name in inference_present_names
        if not name.startswith((_CHAT_PREFIX, _EMBEDDING_PREFIX))
    ]
    new_family_present = bool(inference_present_names)
    legacy_present_names = sorted(
        name
        for name in present
        if name.startswith(_LEGACY_PREFIX)
    )

    if new_family_present:
        if stray_inference_names:
            errors.append(
                "unknown inference variable(s): "
                + ", ".join(stray_inference_names)
                + " (split configuration variables must be under the "
                "INFERENCE_CHAT_ or INFERENCE_EMBEDDING_ prefix; a typo must "
                "fail closed, never fall back to legacy LLMAAS_*)"
            )
        if legacy_present_names:
            errors.append(
                "legacy LLMAAS_* and split INFERENCE_* configuration "
                "families must not coexist (present legacy variables: "
                + ", ".join(legacy_present_names)
                + ") — remove one complete family; there is no per-field "
                "fallback"
            )
        if errors:
            raise InferenceConfigError(errors)
        chat = _resolve_new_role("chat", present, errors)
        embedding = _resolve_new_role("embedding", present, errors)
        if errors:
            raise InferenceConfigError(errors)
        return InferenceConfig(
            chat=chat,  # type: ignore[arg-type]
            embedding=embedding,  # type: ignore[arg-type]
            legacy_active=False,
        )

    # Fail closed on any unrecognized LLMAAS_* name, symmetric with the new
    # path's unknown-variable rejection. Without this a typo such as
    # LLMAAS_MODLE would be silently ignored and the runtime would fall back to
    # a default model — a fail-open contradiction of the strict legacy contract.
    # Diagnostics echo variable NAMES only.
    unknown_legacy = [
        name for name in legacy_present_names if name not in LEGACY_KNOWN_VARIABLES
    ]
    if unknown_legacy:
        errors.append(
            "unknown legacy variable(s): "
            + ", ".join(unknown_legacy)
            + " (known LLMAAS_* variables: "
            + ", ".join(sorted(LEGACY_KNOWN_VARIABLES))
            + "; there is no per-field fallback and a typo never selects a default)"
        )

    chat, embedding, legacy_active = _resolve_legacy(present, errors)
    if errors:
        raise InferenceConfigError(errors)
    if legacy_active:
        _emit_legacy_deprecation_warning_once()
    return InferenceConfig(chat=chat, embedding=embedding, legacy_active=legacy_active)
