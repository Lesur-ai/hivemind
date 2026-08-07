# -*- coding: utf-8 -*-
"""Provider-neutral inference boundary shared by the Hivemind core and the
embedded Graph Memory runtime (P13-1, ADR-0027).

This package is a pure provider boundary:

- it imports neither ``live_mem`` nor ``mcp_memory``/Graph Memory (dependency
  direction is enforced by ``tests/test_p13_inference_package.py``);
- provider SDK construction is confined to the registered adapters;
- normalized request/result and probe records are immutable and carry no
  ``space_id``, Hivemind commit, membership, queue, lease, term, fencing,
  staging, manifest, tombstone, or watermark authority.

Scope of this foundation slice (P13-1A / #274): the immutable records, the safe
error envelope, the canonical profile/endpoint identities, the split role
configuration with strict ``LLMAAS_*`` legacy selection, the provider→adapter/
role registry map and resolution, the single Hivemind-owned bounded retry loop,
and the owned outbound transport with redaction. The concrete provider network
adapters and the deterministic emulator (#275) build on that frozen surface
without changing it.

Scope of the consumer slice (P13-1C / #276): ``InferenceRuntime``, the
per-process holder each consuming service keeps. It snapshots the resolved
configuration once, owns the adapter transports, and closes them on ASGI
shutdown, so the core and the embedded Graph Memory runtime resolve the SAME
immutable role profiles.

Scope of the identity slice (P13-1D / #277): collision-resistant canonical
Qdrant collection names and the pure compact embedding-identity record,
fingerprint, parser, builders, and validators. Qdrant observation, lifecycle,
and mutations remain in the consuming Graph Memory runtime.

Import discipline: this module and every submodule keep their top level
import-light (stdlib only). ``httpx`` and provider SDKs are imported lazily
inside the transport/adapter seams so the auth/storage modules of both
consumers keep importing cleanly in environments without the full inference
stack.
"""

from .config import (
    InferenceConfig,
    InferenceConfigError,
    merged_environment,
    resolve_inference_config,
)
from .collection_identity import (
    EMBEDDING_COLLECTION_IDENTITY_FIELDS,
    EmbeddingCollectionIdentity,
    EmbeddingIdentityError,
    build_configured_embedding_collection_identity,
    build_embedding_collection_identity,
    canonical_qdrant_collection_name,
    embedding_metadata_fingerprint,
    parse_embedding_collection_identity,
    validate_embedding_collection_identity,
)
from .errors import ERROR_CATEGORIES, InferenceError
from .profiles import (
    EMBEDDING_COLLECTION_SCHEMA_VERSION,
    EMBEDDING_CONTRACT_VERSION,
    ResolvedChatProfile,
    ResolvedEmbeddingProfile,
    embedding_profile_fingerprint,
    endpoint_sha256,
)
from .records import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    EmbeddingRequest,
    EmbeddingResult,
    ProbeResult,
)
from .registry import (
    CHAT_PROVIDER_IDS,
    EMBEDDING_PROVIDER_IDS,
    PROVIDER_TO_ADAPTER,
    adapter_for_provider,
)
from .runtime import InferenceRoleUnavailable, InferenceRuntime

__all__ = [
    "CHAT_PROVIDER_IDS",
    "ChatMessage",
    "ChatRequest",
    "ChatResult",
    "EMBEDDING_COLLECTION_IDENTITY_FIELDS",
    "EMBEDDING_COLLECTION_SCHEMA_VERSION",
    "EMBEDDING_CONTRACT_VERSION",
    "EMBEDDING_PROVIDER_IDS",
    "ERROR_CATEGORIES",
    "EmbeddingCollectionIdentity",
    "EmbeddingIdentityError",
    "EmbeddingRequest",
    "EmbeddingResult",
    "InferenceConfig",
    "InferenceConfigError",
    "InferenceError",
    "InferenceRoleUnavailable",
    "InferenceRuntime",
    "PROVIDER_TO_ADAPTER",
    "ProbeResult",
    "ResolvedChatProfile",
    "ResolvedEmbeddingProfile",
    "adapter_for_provider",
    "build_configured_embedding_collection_identity",
    "build_embedding_collection_identity",
    "canonical_qdrant_collection_name",
    "embedding_metadata_fingerprint",
    "embedding_profile_fingerprint",
    "endpoint_sha256",
    "merged_environment",
    "parse_embedding_collection_identity",
    "resolve_inference_config",
    "validate_embedding_collection_identity",
]
