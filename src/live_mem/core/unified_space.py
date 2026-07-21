# -*- coding: utf-8 -*-
"""
Unified space model (P2-1, ADR-0004).

One ``space_id`` owns FOUR concerns — **short**, **mid**, **long** and
**hive** — that physically co-reside under the single ``{space_id}/`` S3
prefix. This module is the *typed, import-side-effect-free* description of that
contract: it names each concern and where its state lives, so downstream code
(P3 engine boundaries, #5 bootstrap, #8 commit manifest) binds to one
vocabulary instead of rediscovering the S3 layout ad hoc.

It introduces **no** behavior change:

- it performs **no storage or network access on import** (pure data + helpers);
- it does **not** touch ``SpaceService`` or the on-disk ``_meta.json`` format.

The concrete key strings stay owned by their modules — short notes by
``core/live.py``, the bank/rules/synthesis objects by ``core/space.py``, the
consolidation counters by ``SpaceMeta``/``core/consolidator.py``, and the whole
``_hivemind/`` subtree by ``core/hivemind/layout.py``. This module *references*
them (the HIVE concern is built from ``layout.py`` builders, never a hardcoded
``_hivemind/`` literal) and an anti-drift test (``tests/test_unified_space.py``)
pins the two representations in sync.

See ``docs/adr/0004-unified-space-model.md`` and
``DESIGN/hivemind/UNIFIED_SPACE.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .hivemind import layout

#: Template of the per-space metadata object. Formatted with ``space_id=``.
#: ``_meta.json`` is *split-ownership* (see ``META_JSON_OWNERS``): it is not
#: owned exclusively by any single concern.
META_JSON_KEY_TEMPLATE = "{space_id}/_meta.json"


class Concern(str, Enum):
    """The four memory/coordination concerns owned by one ``space_id``."""

    SHORT = "short"
    MID = "mid"
    LONG = "long"
    HIVE = "hive"


@dataclass(frozen=True)
class ConcernLocation:
    """
    Where a single :class:`Concern`'s state physically lives.

    Locations are expressed as **templates** (formatted with ``space_id``) for
    the inline f-string keys owned by ``live.py``/``space.py``, and as
    **builders** (callables) for keys owned by ``hivemind/layout.py`` so the
    mapping cannot drift from the layout module and never hardcodes the
    ``_hivemind/`` prefix.
    """

    concern: Concern
    summary: str
    #: ``str.format(space_id=...)`` templates for S3 *prefixes* (LIST roots).
    s3_prefix_templates: tuple[str, ...] = ()
    #: ``str.format(space_id=...)`` templates for individual S3 *objects*.
    s3_object_templates: tuple[str, ...] = ()
    #: ``layout.py`` builders producing S3 *prefixes* (e.g. ``HIVEMIND_PREFIX``).
    s3_prefix_builders: tuple[Callable[[str], str], ...] = ()
    #: ``layout.py`` builders for objects that are **node-local** (never
    #: replicated into a shared snapshot; cf.
    #: ``lifecycle._NODE_LOCAL_HIVEMIND_PATHS``).
    node_local_builders: tuple[Callable[[str], str], ...] = ()
    #: Field names that live **inside** ``{space_id}/_meta.json`` rather than as
    #: standalone S3 objects (consolidation counters, the ``graph_memory`` block).
    meta_fields: tuple[str, ...] = ()
    #: True when the concern's projection/index is **not** stored under the
    #: space's ``{space_id}/`` S3 prefix (so it owns no S3 key under the space).
    #: Set for the long tier: its ontology/graph projection lives in the long
    #: engine's own store, not under the space prefix. This is an *addressing*
    #: fact, not an authority statement — long stays an internal,
    #: protocol-derived/non-authoritative Hivemind tier (ADR-0010).
    owns_no_space_s3_key: bool = False
    notes: str = ""

    def prefixes(self, space_id: str) -> tuple[str, ...]:
        """Concrete S3 prefixes for ``space_id`` (templates + builders)."""
        templated = tuple(t.format(space_id=space_id) for t in self.s3_prefix_templates)
        built = tuple(b(space_id) for b in self.s3_prefix_builders)
        return templated + built

    def objects(self, space_id: str) -> tuple[str, ...]:
        """Concrete standalone S3 object keys for ``space_id``."""
        return tuple(t.format(space_id=space_id) for t in self.s3_object_templates)

    def node_local_objects(self, space_id: str) -> tuple[str, ...]:
        """Concrete node-local S3 object keys for ``space_id`` (HIVE only)."""
        return tuple(b(space_id) for b in self.node_local_builders)


#: The authoritative concern -> location mapping. Importing this performs no
#: I/O; resolving concrete keys requires an explicit ``space_id``.
OWNED_CONCERNS: dict[Concern, ConcernLocation] = {
    Concern.SHORT: ConcernLocation(
        concern=Concern.SHORT,
        summary="Short-term memory: atomic live notes and immediate context.",
        s3_prefix_templates=("{space_id}/live/",),
        notes=(
            "One S3 object per note under {space_id}/live/, named "
            "{YYYYMMDD}T{HHMMSS}_{agent}_{category}_{uuid8}.md (see core/live.py)."
        ),
    ),
    Concern.MID: ConcernLocation(
        concern=Concern.MID,
        summary=(
            "Mid-term memory: Markdown bank files, rules, synthesis and "
            "consolidation state."
        ),
        s3_prefix_templates=("{space_id}/bank/",),
        s3_object_templates=("{space_id}/_rules.md", "{space_id}/_synthesis.md"),
        meta_fields=(
            "last_consolidation",
            "consolidation_count",
            "total_notes_processed",
        ),
        notes=(
            "Consolidation counters (last_consolidation, consolidation_count, "
            "total_notes_processed) are FIELDS inside {space_id}/_meta.json, "
            "not standalone S3 objects."
        ),
    ),
    Concern.LONG: ConcernLocation(
        concern=Concern.LONG,
        summary=(
            "Long-term memory: the ontology/knowledge-graph engine tier, "
            "bound via the graph_memory config block."
        ),
        meta_fields=("graph_memory",),
        owns_no_space_s3_key=True,
        notes=(
            "The long tier is a mandatory internal Hivemind engine (ontology/"
            "knowledge graph), but protocol-derived and NON-AUTHORITATIVE: never "
            "commit/rollback/audit/watermark/recovery truth (ADR-0010). The "
            "'graph_memory' block inside {space_id}/_meta.json configures the "
            "binding (it may point at a graph-memory backend); the long "
            "projection/index is held by the long engine, NOT under the "
            "{space_id}/ S3 prefix — so the long tier owns no S3 key under the "
            "space. The graph_memory block is local-only and never replicated "
            "into a shared commit (core/models.SHARED_META_FIELDS excludes it)."
        ),
    ),
    Concern.HIVE: ConcernLocation(
        concern=Concern.HIVE,
        summary=(
            "Hivemind coordination: membership, token lease, queue, commits, "
            "tombstones, watermarks and recovery state."
        ),
        s3_prefix_builders=(layout.HIVEMIND_PREFIX,),
        node_local_builders=(layout.node_key, layout.node_status_key),
        notes=(
            "The entire {space_id}/_hivemind/ subtree; every key is built by "
            "core/hivemind/layout.py (never hardcoded here). node.json and "
            "node_status.json are NODE-LOCAL and never replicated into a shared "
            "snapshot (cf. lifecycle._NODE_LOCAL_HIVEMIND_PATHS)."
        ),
    ),
}

#: ``{space_id}/_meta.json`` is split-ownership: MID owns the consolidation
#: counters, LONG owns the ``graph_memory`` block, plus identity/shared fields
#: (space_id, description, owner, created_at, version). No single concern owns
#: it exclusively. The authoritative shared/local field split lives in
#: ``core/models.SHARED_META_FIELDS`` + ``meta_shared_projection`` and
#: ``core/hivemind/lifecycle._META_IDENTITY_LOCAL`` — this module does not
#: re-derive it.
META_JSON_OWNERS: tuple[Concern, ...] = (Concern.MID, Concern.LONG)


def concern_location(concern: Concern) -> ConcernLocation:
    """Return the :class:`ConcernLocation` for ``concern``."""
    return OWNED_CONCERNS[concern]


__all__ = [
    "Concern",
    "ConcernLocation",
    "OWNED_CONCERNS",
    "META_JSON_KEY_TEMPLATE",
    "META_JSON_OWNERS",
    "concern_location",
]
