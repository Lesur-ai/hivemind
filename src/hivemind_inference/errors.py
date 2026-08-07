# -*- coding: utf-8 -*-
"""Normalized safe error envelope for the inference boundary (ADR-0027).

Adapters convert every provider/transport failure into one immutable
:class:`InferenceError`. Its complete safe fields are limited to ``category``,
``role``, ``provider_id``, ``adapter_id``, ``retryable``, and a generated
``correlation_id``. Provider exception messages, response payloads, endpoint
URLs, headers, prompts, completions, and embeddings are NEVER copied into this
envelope: ``str(error)`` is built exclusively from those six safe fields, so
every downstream ``str(e)`` formatting site inherits a secret-free message by
construction.

Task cancellation is control flow, not a provider error: adapters never catch
``asyncio.CancelledError`` into this envelope.
"""

from __future__ import annotations

from .records import ERROR_CATEGORIES, _validate_correlation_id
from .registry import EMBEDDING_PROVIDER_IDS, PROVIDER_TO_ADAPTER

# ERROR_CATEGORIES (the frozen ADR-0027 safe category set) is defined with the
# normalized record vocabularies in ``records`` and re-exported here, so the
# probe record and this envelope share one source without an import cycle.
__all__ = ["ERROR_CATEGORIES", "InferenceError"]

_ROLES = ("chat", "embedding")


class InferenceError(Exception):
    """Immutable safe provider-failure envelope.

    ``retryable`` records whether THIS occurrence authorized the single
    ADR-0027 bounded retry (explicitly transient rate limit, or a transport
    failure proven pre-send). It is informational for callers: the retry loop
    itself lives inside the adapters and has already run when the error
    escapes.
    """

    __slots__ = ("category", "role", "provider_id", "adapter_id", "retryable", "correlation_id")

    def __init__(
        self,
        *,
        category: str,
        role: str,
        provider_id: str,
        adapter_id: str,
        retryable: bool,
        correlation_id: str,
    ) -> None:
        if category not in ERROR_CATEGORIES:
            raise ValueError("category is not a recognized inference error category")
        if role not in _ROLES:
            raise ValueError("role is not a recognized inference role")
        # Every OUTWARD field is validated to a safe, registry-bound or
        # grammar-checked value before it can reach ``str(error)`` or
        # ``safe_payload()``. This closes the channel by which a configured
        # endpoint, credential, or raw provider text could otherwise be
        # presented as a provider/adapter identity or correlation id. All
        # rejection diagnostics are value-free (they never echo the field).
        if provider_id not in PROVIDER_TO_ADAPTER:
            raise ValueError("provider_id is not a registered provider identifier")
        if role == "embedding" and provider_id not in EMBEDDING_PROVIDER_IDS:
            raise ValueError("provider does not support the embedding role")
        if adapter_id != PROVIDER_TO_ADAPTER[provider_id]:
            raise ValueError(
                "adapter_id is not the registered adapter for the provider"
            )
        if not isinstance(retryable, bool):
            raise ValueError("retryable must be a bool")
        _validate_correlation_id(correlation_id)
        # The message is assembled ONLY from the six safe fields — never from
        # provider text — and frozen at construction time.
        message = (
            f"inference {role} failure: category={category} "
            f"provider={provider_id} adapter={adapter_id} "
            f"retryable={'true' if retryable else 'false'} "
            f"correlation_id={correlation_id}"
        )
        super().__init__(message)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "adapter_id", adapter_id)
        object.__setattr__(self, "retryable", retryable)
        object.__setattr__(self, "correlation_id", correlation_id)

    def __setattr__(self, name: str, value: object) -> None:  # pragma: no cover
        raise AttributeError("InferenceError is immutable")

    def __delattr__(self, name: str) -> None:  # pragma: no cover
        raise AttributeError("InferenceError is immutable")

    def safe_payload(self) -> dict:
        """Client-facing structured view (exactly the six safe fields)."""
        return {
            "category": self.category,
            "role": self.role,
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "retryable": self.retryable,
            "correlation_id": self.correlation_id,
        }
