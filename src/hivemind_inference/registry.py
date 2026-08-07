# -*- coding: utf-8 -*-
"""Provider registry: explicit identifier-to-adapter resolution (ADR-0027).

``INFERENCE_*_PROVIDER`` carries an operator-facing registered profile
identifier; this registry maps that identifier to exactly one adapter. A URL,
hostname, response header, or model name never selects or changes an adapter
implicitly, and unknown identifiers fail closed before any network access —
there is no default and no fallback adapter.

Scope (P13-1A / #274): this module freezes the provider→adapter/role MAP and
the fail-closed ``adapter_for_provider`` resolution primitive.

Scope (P13-1B / #275): the concrete ``build_*`` adapter/probe factories below
instantiate an SDK-backed provider from a resolved profile. They are the only
provider-network construction seam; adapter modules are imported lazily
inside each factory so this module's top level stays stdlib-only (the
frozen P13-1A map/resolution above is unchanged).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a circular import
    from .profiles import ResolvedChatProfile, ResolvedEmbeddingProfile

# Frozen ADR-0027 provider-to-adapter table. Exposed as an immutable
# ``Mapping`` built directly from an unreferenced literal, so there is no
# addressable mutable backing dict: no in-process importer can remap a provider
# to the wrong adapter after import. The frozen identity is what later adapters
# trust.
PROVIDER_TO_ADAPTER: Mapping[str, str] = MappingProxyType(
    {
        "cloud-temple": "openai-compatible",
        "scaleway": "openai-compatible",
        "openai": "openai-compatible",
        "mistral": "openai-compatible",
        "gemini": "openai-compatible",
        "ovhcloud": "openai-compatible",
        "anthropic": "anthropic",
        "ollama": "openai-compatible",
        "openai-compatible": "openai-compatible",
    }
)

# Role restrictions: ``anthropic`` is chat-only — embeddings fail
# configuration validation (Anthropic has no native embedding model).
CHAT_PROVIDER_IDS: tuple[str, ...] = tuple(PROVIDER_TO_ADAPTER)
EMBEDDING_PROVIDER_IDS: tuple[str, ...] = tuple(
    provider for provider in PROVIDER_TO_ADAPTER if provider != "anthropic"
)

# Documented HTTPS endpoint shapes for named hosted profiles: exact host AND
# exact normalized path. An arbitrary same-host path — e.g. a stray ``/v1`` on
# the native Anthropic base — must fail at configuration time, never surface as
# a runtime inference outage. The path ``""`` means the bare API base.
# Operators with a nonstandard endpoint use
# the explicit generic ``openai-compatible`` profile (or ``ollama`` for the
# documented local endpoint) — brand identifiers never stretch to arbitrary
# hosts or paths.
HOSTED_PROVIDER_HOSTS: Mapping[str, str] = MappingProxyType(
    {
        "cloud-temple": "api.ai.cloud-temple.com",
        "scaleway": "api.scaleway.ai",
        "openai": "api.openai.com",
        "mistral": "api.mistral.ai",
        "gemini": "generativelanguage.googleapis.com",
        "ovhcloud": "oai.endpoints.kepler.ai.cloud.ovh.net",
        "anthropic": "api.anthropic.com",
    }
)
HOSTED_PROVIDER_PATHS: Mapping[str, str] = MappingProxyType(
    {
        "cloud-temple": "/v1",
        "scaleway": "/v1",
        "openai": "/v1",
        "mistral": "/v1",
        "gemini": "/v1beta/openai",
        "ovhcloud": "/v1",
        "anthropic": "",
    }
)

# Ollama is the documented LOCAL development endpoint (ADR-0027), NOT a generic
# gateway: it is pinned to the documented local hosts, the HTTP scheme, the
# default Ollama port, and the ``/v1`` path. A nonstandard local gateway uses
# the explicit ``openai-compatible`` profile. This keeps the trusted ``ollama``
# identity from resolving an arbitrary (possibly external) endpoint and sending
# prompts/documents off-box under a local-development label.
OLLAMA_ALLOWED_HOSTS: frozenset[str] = frozenset({"localhost", "host.docker.internal"})
OLLAMA_PORT: int = 11434
OLLAMA_PATH: str = "/v1"

# Supported finite temperature range per adapter family.
ADAPTER_TEMPERATURE_RANGES: Mapping[str, tuple[float, float]] = MappingProxyType(
    {
        "openai-compatible": (0.0, 2.0),
        "anthropic": (0.0, 1.0),
    }
)


def adapter_for_provider(provider_id: str, *, role: str) -> str:
    """Resolve a provider identifier to its adapter, fail-closed.

    Raises ``ValueError`` for unknown identifiers, role-incompatible
    selections, and unknown roles; configuration validation converts that
    into the aggregated startup diagnostic and never reaches network access
    with an unresolved identity.
    """
    if role not in ("chat", "embedding"):
        raise ValueError(f"unknown inference role '{role}'")
    if provider_id not in PROVIDER_TO_ADAPTER:
        raise ValueError(f"unknown inference provider identifier '{provider_id}'")
    if role == "embedding" and provider_id not in EMBEDDING_PROVIDER_IDS:
        raise ValueError(
            f"provider '{provider_id}' does not support the embedding role"
        )
    return PROVIDER_TO_ADAPTER[provider_id]


def validate_endpoint(name: str, value: str, *, provider_id: str) -> list[str]:
    """Provider-aware absolute-URL validation, shared by config resolution and
    resolved-profile construction so the two cannot drift.

    Returns a list of value-free diagnostics (empty means valid). It never
    echoes the configured value: a malformed authority (bad port / IPv6) that
    makes urlsplit or the lazy accessors raise ValueError is converted to a
    fixed name-only message, and the raise stays inside this function so no
    secret-bearing original reaches a caller's exception chain. Named hosted
    providers are pinned to their documented HTTPS host, default port, and exact
    path; the generic ``openai-compatible`` and ``ollama`` profiles accept the
    operator's explicit absolute HTTP(S) endpoint.
    """
    try:
        parts = urlsplit(value)
        scheme = parts.scheme
        hostname = parts.hostname
    except ValueError:
        return [f"{name} is not a valid absolute http(s) URL"]
    if scheme not in ("http", "https") or not hostname:
        return [f"{name} must be an absolute http(s) URL with a host"]
    try:
        port = parts.port
    except ValueError:
        return [f"{name} endpoint port is invalid"]
    errors: list[str] = []
    if port is not None and not (1 <= port <= 65535):
        errors.append(f"{name} endpoint port is out of range")
    if parts.username is not None or parts.password is not None:
        errors.append(f"{name} must not contain userinfo credentials")
    if parts.query:
        errors.append(f"{name} must not contain a query component")
    if parts.fragment:
        errors.append(f"{name} must not contain a fragment component")
    # endpoint_sha256 normalizes a single trailing slash away, so '/v1' and
    # '/v1/' share one identity by design; but repeated trailing slashes
    # ('/v1//') would route differently on a path-sensitive gateway while
    # hashing identically. Reject them for EVERY provider (generic and hosted)
    # so the endpoint identity stays injective over accepted paths.
    if parts.path.endswith("//"):
        errors.append(f"{name} must not contain repeated trailing slashes")
    hosted_host = HOSTED_PROVIDER_HOSTS.get(provider_id)
    if hosted_host is not None:
        if scheme != "https":
            errors.append(f"{name} must use https for the '{provider_id}' profile")
        if (hostname or "").lower() != hosted_host:
            errors.append(
                f"{name} must point at the documented '{provider_id}' host "
                "(see ADR-0027; use the generic 'openai-compatible' profile "
                "for a nonstandard endpoint)"
            )
        if port is not None and port != 443:
            errors.append(
                f"{name} must use the default https port for the "
                f"'{provider_id}' profile"
            )
        documented_path = HOSTED_PROVIDER_PATHS[provider_id]
        # Accept only the exact documented path or a single cosmetic trailing
        # slash. A broad rstrip('/') would accept '/v1//', which routes to a
        # different path on a path-sensitive provider yet normalizes to the same
        # endpoint fingerprint — a route/identity mismatch.
        if parts.path not in (documented_path, documented_path + "/"):
            errors.append(
                f"{name} must use the documented '{provider_id}' endpoint "
                "path exactly (see ADR-0027; use the generic "
                "'openai-compatible' profile for a nonstandard endpoint)"
            )
    elif provider_id == "ollama":
        # Ollama is the documented LOCAL endpoint, not a generic gateway: pin
        # its scheme/host/port/path so the trusted local identity cannot resolve
        # an arbitrary (possibly external) URL. A nonstandard local gateway must
        # use the explicit 'openai-compatible' profile instead.
        if scheme != "http":
            errors.append(
                f"{name} must use http for the local 'ollama' profile "
                "(use the generic 'openai-compatible' profile for a nonstandard "
                "endpoint)"
            )
        if (hostname or "").lower() not in OLLAMA_ALLOWED_HOSTS:
            errors.append(
                f"{name} must point at a documented local 'ollama' host "
                "(see ADR-0027; use the generic 'openai-compatible' profile "
                "for a nonstandard endpoint)"
            )
        if port != OLLAMA_PORT:
            errors.append(
                f"{name} must use the documented 'ollama' port "
                "(use the generic 'openai-compatible' profile for a nonstandard "
                "endpoint)"
            )
        if parts.path not in (OLLAMA_PATH, OLLAMA_PATH + "/"):
            errors.append(
                f"{name} must use the documented 'ollama' endpoint path "
                "(see ADR-0027; use the generic 'openai-compatible' profile "
                "for a nonstandard endpoint)"
            )
    return errors


def build_chat_provider(profile: "ResolvedChatProfile", *, proxy_url: str | None = None):
    """Construct the chat adapter for a resolved profile (SDK seam, #275)."""
    if profile.adapter_id == "openai-compatible":
        from .adapters.openai_compatible import OpenAICompatibleChatProvider

        return OpenAICompatibleChatProvider(profile, proxy_url=proxy_url)
    if profile.adapter_id == "anthropic":
        from .adapters.anthropic_native import AnthropicChatProvider

        return AnthropicChatProvider(profile, proxy_url=proxy_url)
    raise ValueError(f"unknown adapter identifier '{profile.adapter_id}'")


def build_embedding_provider(
    profile: "ResolvedEmbeddingProfile", *, proxy_url: str | None = None
):
    """Construct the embedding adapter for a resolved profile (SDK seam, #275)."""
    if profile.adapter_id == "openai-compatible":
        from .adapters.openai_compatible import OpenAICompatibleEmbeddingProvider

        return OpenAICompatibleEmbeddingProvider(profile, proxy_url=proxy_url)
    raise ValueError(
        f"adapter '{profile.adapter_id}' does not support the embedding role"
    )


def build_chat_probe(profile: "ResolvedChatProfile", *, proxy_url: str | None = None):
    """Construct the role-scoped probe for a resolved chat profile (#275)."""
    if profile.adapter_id == "openai-compatible":
        from .adapters.openai_compatible import OpenAICompatibleProbe

        return OpenAICompatibleProbe(profile, proxy_url=proxy_url)
    if profile.adapter_id == "anthropic":
        from .adapters.anthropic_native import AnthropicProbe

        return AnthropicProbe(profile, proxy_url=proxy_url)
    raise ValueError(f"unknown adapter identifier '{profile.adapter_id}'")


def build_embedding_probe(
    profile: "ResolvedEmbeddingProfile", *, proxy_url: str | None = None
):
    """Construct the role-scoped probe for a resolved embedding profile (#275)."""
    if profile.adapter_id == "openai-compatible":
        from .adapters.openai_compatible import OpenAICompatibleProbe

        return OpenAICompatibleProbe(profile, proxy_url=proxy_url)
    raise ValueError(
        f"adapter '{profile.adapter_id}' does not support the embedding role"
    )
