# -*- coding: utf-8 -*-
"""
WriteSink — single typed durable-write boundary (P3-3, EPIC-P3 / ADR-0007),
with the P5-8 (#16) capstone staged-commit body.

This module defines the *only* seam through which durable mutations are routed.
It mirrors the mutation surface of :class:`live_mem.core.storage.StorageService`
EXACTLY (verified ``storage.py`` ``put`` / ``put_json`` / ``delete`` /
``delete_many``) so a direct adapter is a byte-for-byte pass-through.

Boundary scope (ADR-0007):

- ONLY durable mutations route through ``WriteSink``: ``put``, ``put_json``,
  ``delete``, ``delete_many``.
- Reads stay on ``StorageService`` and are deliberately NOT on this boundary
  (``get`` / ``get_json`` / ``list_objects`` / ``list_prefixes`` / ``exists`` /
  ``list_and_get`` / ``test_connection``).
- ``copy_object`` is intentionally EXCLUDED from the Wave-1 ``WriteSink``: it is
  the backup-only path (``storage.copy_object`` — "Utile pour les backups"),
  and ``backup_restore`` over a shared Hivemind space is the explicit open gap
  deferred to ADR-0014 / issue #9. It must NOT be added here.

Two implementations:

- :class:`DirectLocalWriteSink` — default for non-Hivemind (``is_hive=False``)
  spaces. Delegates VERBATIM to ``StorageService`` with zero transformation so
  output is byte-for-byte identical to today's ``storage.put`` / ``put_json`` /
  ``delete`` / ``delete_many``. ``commit()`` is a no-op (writes happen eagerly).
- :class:`StagedHivemindWriteSink` — Hivemind spaces (P5-8 #16 CAPSTONE). The
  real staged body: ``put`` / ``put_json`` of ``{space}/bank/*`` keys BUFFER;
  one :meth:`StagedHivemindWriteSink.commit` assembles the proposed bank and
  drives ONE atomic ``CommitRuntime`` commit whose G0 gate
  (``assert_commit_allowed``, ADR-0011) is the SINGLE authorization. A direct
  S3 write is NEVER performed by the per-op methods.

  Code-grounded scope (the per-op -> atomic mapping is bounded by what
  ``CommitRuntime.apply_commit`` can express):

  - ``apply_commit`` step 1 PROMOTES (``put``) each manifest entry to
    ``{space}/bank/{rel_path}`` and performs NO live-bank deletion — it is
    put-only / additive-overwrite, NOT full-replacement. A commit therefore
    cannot express a deletion of an existing bank file. Consequently
    :meth:`StagedHivemindWriteSink.delete` / :meth:`delete_many` FAIL CLOSED
    (raise :class:`StagedWriteNotImplemented`) rather than silently NOT deleting
    or bypassing the single-writer path. The live-delete-via-commit choreography
    is an explicit, documented deferral.
  - the promote loop lands every entry under ``{space}/bank/`` (see
    ``commit_runtime._bank_live_key``), so ONLY ``{space}/bank/*`` keys can be
    staged. A buffered ``put`` to a key outside ``{space}/bank/`` (e.g. a
    top-level ``_synthesis.md`` / ``_rules.md`` / ``_meta.json``) FAILS CLOSED at
    :meth:`commit` — there is no current commit path for non-``bank/`` files.

This module is boundary-only for routing: ``StagedHivemindWriteSink`` is
constructed by the engine registry (P3-7 / ADR-0007), which injects the store +
commit runtime + holder context. Nothing is re-exported from ``live_mem.core``
here.
"""

from __future__ import annotations

import abc
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Optional

from .storage import bank_relpath, get_storage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from .hivemind.commit_runtime import CommitRuntime
    from .hivemind.lease_runtime import LeaseRuntime
    from .hivemind.models import BankVersionPointer
    from .hivemind.state import HivemindStateStore
    from .storage import StorageService


# Mirror of StorageService.put's content_type default (storage.py: put).
# Declared here so DirectLocalWriteSink forwards the SAME default StorageService
# declares — byte-for-byte parity demands the sink not invent its own default.
DEFAULT_CONTENT_TYPE = "text/plain; charset=utf-8"


def _new_commit_id() -> str:
    """Fresh commit id (no slash — ``staging_commit_prefix`` rejects slashes).

    A hex uuid4 is slash-free and unique; the staging tree is keyed on it so a
    fenced retry targeting the same version never overwrites another holder's
    staged bytes (``layout`` docstring)."""
    return uuid.uuid4().hex


def _now_utc() -> "datetime":
    """Default wall-clock seam (injectable via the sink's ``clock=``)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


class StagedWriteNotImplemented(RuntimeError):
    """Raised when a durable write to a Hivemind space cannot be staged.

    Subclasses :class:`RuntimeError` for consistency with this codebase's other
    domain errors (``CorruptedStateError`` / ``BootstrapError`` /
    ``RegistryRefused`` are all ``RuntimeError`` subclasses).

    Post-P5-8 this is the FAIL-CLOSED backstop for the staged-write operations
    that the current ``CommitRuntime`` cannot express atomically — a
    live-bank-key DELETE (``apply_commit`` is put-only) and a write to a key
    OUTSIDE ``{space}/bank/`` (the promote loop only lands under ``bank/``). A
    valid Hivemind shared write must fail LOUDLY rather than silently bypass the
    single-writer path or silently no-op. The wired path (``put`` of a
    ``{space}/bank/*`` key + :meth:`StagedHivemindWriteSink.commit`) does NOT
    raise this; it stages + commits via ``assert_commit_allowed`` (#7) +
    ``CommitRuntime.apply_commit`` (#8). The deferred legs (live delete, non-bank
    file staging) keep raising it (#9 follow-up).
    """

    def __init__(self, op: Optional[str] = None, key: Optional[str] = None) -> None:
        detail = ""
        if op:
            detail = f" (op={op}"
            if key is not None:
                detail += f", key={key!r}"
            detail += ")"
        super().__init__(
            "Durable write to a Hivemind space could not be staged"
            f"{detail}: this leg of the staged commit pipeline is deferred "
            "(assert_commit_allowed #7, staging/manifest/BANK_COMMIT #8, mutation "
            "protection #9). Refusing rather than bypassing the single-writer "
            "path or silently no-op'ing."
        )


class DirectLocalWriteFenced(RuntimeError):
    """A registry-resolved local sink is no longer allowed to mutate.

    Project Mesh source preparation can change a space from ``DIRECT_LOCAL`` to
    ``STAGED`` after a caller resolved its sink. Registry-created direct sinks
    therefore revalidate the durable reservation, current route, and
    irreversible source provenance at the last typed mutation boundary. This
    is fail-closed defence in depth, not a claim that the check and following
    object-store mutation are one transaction.
    """

    def __init__(self, space_id: str) -> None:
        super().__init__(
            f"Direct local mutation for space {space_id!r} is fenced because "
            "its reservation or Hivemind route changed"
        )
        self.space_id = space_id


class WriteSink(abc.ABC):
    """Abstract single typed durable-write boundary.

    Defines EXACTLY the four durable mutation operations, mirroring
    ``StorageService`` signatures so a concrete adapter can be a verbatim
    pass-through. Reads are NOT part of this boundary and stay on
    ``StorageService``; ``copy_object`` is intentionally excluded (backup-only,
    deferred to ADR-0014 / #9).

    :meth:`commit` is an OPTIONAL flush hook. ``DirectLocalWriteSink`` writes
    eagerly so its ``commit`` is a no-op; ``StagedHivemindWriteSink`` buffers the
    per-op writes and overrides ``commit`` to drive the SINGLE atomic commit. A
    tool stays route-blind: it calls ``put`` / ``delete`` then ``commit`` once,
    and the SINK alone decides local-vs-staged.
    """

    @abc.abstractmethod
    async def put(
        self, key: str, content: str, content_type: str = DEFAULT_CONTENT_TYPE
    ) -> None:
        """Durable write of a single text object (mirrors ``StorageService.put``)."""
        raise NotImplementedError

    @abc.abstractmethod
    async def put_json(self, key: str, data: dict) -> None:
        """Durable write of a JSON object (mirrors ``StorageService.put_json``)."""
        raise NotImplementedError

    @abc.abstractmethod
    async def delete(self, key: str) -> None:
        """Durable delete of a single object (mirrors ``StorageService.delete``)."""
        raise NotImplementedError

    @abc.abstractmethod
    async def delete_many(self, keys: list[str]) -> int:
        """Durable batch delete; returns the count deleted.

        Mirrors ``StorageService.delete_many`` (deletes one-by-one, returns an
        int count).
        """
        raise NotImplementedError

    async def commit(
        self, *, reason: str = "", notes_consumed: Optional[list[str]] = None
    ) -> object | None:
        """Flush hook. Default no-op (eager sinks write through immediately).

        ``StagedHivemindWriteSink`` overrides this to drive the single atomic
        commit over the buffered ops. Returns ``None`` by default.
        """
        return None


class DirectLocalWriteSink(WriteSink):
    """Default sink for non-Hivemind (``is_hive=False``) spaces.

    Delegates VERBATIM to ``StorageService`` with zero transformation, so the
    durable output is byte-for-byte identical to calling ``storage`` directly.
    This is the ONLY path that ever reaches direct local storage; every shared /
    corrupted / unsafe / resync space is staged-or-refused (enforced upstream by
    the P3-7 routing seam, not here).

    ``storage`` is injectable for tests; when ``None`` it is resolved lazily via
    ``get_storage()`` (the singleton). Resolution is lazy — instantiating
    ``StorageService`` builds boto3 clients and reads settings, so it must not
    happen at import/def time.
    """

    def __init__(
        self,
        storage: "StorageService | None" = None,
        *,
        space_id: str | None = None,
    ) -> None:
        # Lazy default: only construct/resolve the singleton when no storage was
        # injected. Tests inject a FakeStorage; never builds a real S3 client.
        self._storage = storage if storage is not None else get_storage()
        self._space_id = space_id

    @property
    def storage(self) -> "StorageService":
        return self._storage

    async def _assert_mutation_allowed(self, keys: list[str]) -> None:
        """Revalidate a registry-bound local sink immediately before mutation.

        Explicitly constructed compatibility/test sinks have no ``space_id`` and
        retain their byte-for-byte pass-through contract. Production registry
        sinks are bound to one space: keys cannot escape that prefix, an active
        Mesh reservation refuses, and either a route/provenance mismatch fences
        the stale sink.
        """

        space_id = self._space_id
        if space_id is None:
            return
        prefix = f"{space_id}/"
        if any(type(key) is not str or not key.startswith(prefix) for key in keys):
            raise DirectLocalWriteFenced(space_id)

        # Lazy imports avoid a write_sink <-> engine-registry import cycle.
        from .hivemind.lifecycle import WriteRoute, resolve_write_route
        from .reservation_guard import (
            assert_direct_local_allowed,
            assert_space_not_reserved,
        )

        await assert_space_not_reserved(space_id)
        route = await resolve_write_route(self._storage, space_id)
        if route is not WriteRoute.DIRECT_LOCAL:
            raise DirectLocalWriteFenced(space_id)
        await assert_direct_local_allowed(space_id)

    async def put(
        self, key: str, content: str, content_type: str = DEFAULT_CONTENT_TYPE
    ) -> None:
        await self._assert_mutation_allowed([key])
        # Pass content_type straight through so the StorageService default and
        # any explicit value are forwarded byte-identically (no re-defaulting).
        await self._storage.put(key, content, content_type)

    async def put_json(self, key: str, data: dict) -> None:
        await self._assert_mutation_allowed([key])
        # Delegate; do NOT re-serialize. StorageService does
        # json.dumps(data, indent=2, ensure_ascii=False) + application/json.
        # Re-implementing it here would risk a different byte layout and break
        # P3-8 golden tests.
        await self._storage.put_json(key, data)

    async def delete(self, key: str) -> None:
        await self._assert_mutation_allowed([key])
        await self._storage.delete(key)

    async def delete_many(self, keys: list[str]) -> int:
        await self._assert_mutation_allowed(keys)
        # Return the int count verbatim (per-key warning-logging behavior is
        # preserved by StorageService).
        return await self._storage.delete_many(keys)

    async def commit(
        self, *, reason: str = "", notes_consumed: Optional[list[str]] = None
    ) -> None:
        # Eager sink: every put/delete already wrote through. The flush hook is a
        # no-op writing NOTHING, so a tool that calls commit() route-blind keeps
        # the non-Hivemind byte footprint identical.
        return None


class _StagedBankOp:
    """A buffered durable op awaiting the single atomic commit.

    ``kind`` in ``{put, put_json}`` (deletes fail closed, see the sink). Mono-op
    container — only the fields its kind needs are set.
    """

    __slots__ = ("kind", "key", "content", "content_type", "data")

    def __init__(
        self,
        kind: str,
        *,
        key: str,
        content: str = "",
        content_type: str = DEFAULT_CONTENT_TYPE,
        data: Optional[dict] = None,
    ) -> None:
        self.kind = kind
        self.key = key
        self.content = content
        self.content_type = content_type
        self.data = data


class StagedHivemindWriteSink(WriteSink):
    """P5-8 (#16) CAPSTONE staged-commit sink for Hivemind spaces.

    The real body. ``put`` / ``put_json`` of a ``{space}/bank/*`` key BUFFER (no
    storage write); a single :meth:`commit` reads the current live bank, replays
    the buffer onto an in-memory snapshot, stages the resulting bank under
    ``staging/{commit_id}/`` and drives ONE atomic ``CommitRuntime.apply_commit``
    whose G0 gate ``assert_commit_allowed`` (ADR-0011) is the SINGLE
    authorization point. No per-op method ever performs a direct S3 write.

    FAIL-CLOSED legs (what the current ``CommitRuntime`` cannot express, deferred
    to the #9 follow-up — never a silent no-op, never a direct write):

    - :meth:`delete` / :meth:`delete_many` raise: ``apply_commit`` is put-only
      (no live-bank deletion), so a delete cannot be a forward commit.
    - a buffered write to a key OUTSIDE ``{space}/bank/`` raises at
      :meth:`commit`: the promote loop only lands under ``{space}/bank/``.

    PROPAGATES fail-closed: ``CommitNotAuthorized`` (NOT_HOLDER / STALE_TERM /
    FENCED / VERSION_CONFLICT / BLOCKED), ``CommitApplyError`` and
    ``CorruptedStateError`` raise out to the tool's ``except Exception:
    safe_error``.
    """

    def __init__(
        self,
        space_id: str,
        storage: "StorageService",
        *,
        store: "HivemindStateStore",
        commit_runtime: "CommitRuntime",
        lease: "LeaseRuntime",
        local_node_id: str,
        fencing_token: int,
        membership_epoch: int,
        commit_id_factory: Callable[[], str] = _new_commit_id,
        clock: "Callable[[], datetime]" = _now_utc,
    ) -> None:
        self.space_id = space_id
        self._storage = storage
        self._store = store
        self._crt = commit_runtime
        # The SINGLE authorization point (ADR-0011). The sink calls it READ-ONLY
        # BEFORE staging so an unauthorized/fenced/stale caller leaves ZERO
        # durable Hivemind state (apply_commit re-runs the SAME gate live, so
        # there is no term/fencing TOCTOU between this pre-stage check and the
        # apply). The orthogonal membership-currency window is closed separately
        # by running the membership re-check + stage + apply under
        # ``_membership_lock`` (see ``commit`` step 6b-7).
        self._lease = lease
        self._local_node_id = local_node_id
        self._fencing_token = fencing_token
        self._membership_epoch = membership_epoch
        self._new_commit_id = commit_id_factory
        self._clock = clock
        self._buffer: list[_StagedBankOp] = []

    # ──────────────────────────────────────────────────────────────────
    # Per-op methods — BUFFER ONLY for put/put_json; deletes fail closed.
    # ──────────────────────────────────────────────────────────────────

    async def put(
        self, key: str, content: str, content_type: str = DEFAULT_CONTENT_TYPE
    ) -> None:
        self._buffer.append(
            _StagedBankOp("put", key=key, content=content, content_type=content_type)
        )

    async def put_json(self, key: str, data: dict) -> None:
        self._buffer.append(_StagedBankOp("put_json", key=key, data=data))

    async def delete(self, key: str) -> None:
        # FAIL CLOSED: apply_commit is put-only (no live-bank deletion), so a
        # delete cannot be expressed as a forward commit. Refuse loudly rather
        # than silently NOT delete or bypass the single-writer path.
        raise StagedWriteNotImplemented(op="delete", key=key)

    async def delete_many(self, keys: list[str]) -> int:
        raise StagedWriteNotImplemented(op="delete_many")

    # ──────────────────────────────────────────────────────────────────
    # Membership-currency re-validation (TOCTOU close) — see commit() step 6b.
    # ──────────────────────────────────────────────────────────────────

    async def _assert_local_membership_current(self) -> None:
        """Re-validate, LIVE at commit time, that the LOCAL node is still an
        ACTIVE member at the CURRENT membership epoch and that the HELD token's
        ``membership_epoch`` matches that current epoch — fail closed otherwise.

        WHY (TOCTOU close): the identical ACTIVE-member / current-token-epoch gate
        runs once at sink RESOLUTION (``EngineRegistry.resolve_sink``), but the
        sink then captures ``fencing_token`` / ``membership_epoch`` and applies
        them later. Membership can change between resolve and ``commit()`` —
        ``lifecycle.evict_member`` bumps the membership epoch and flips the local
        member to ``EVICTED`` WITHOUT necessarily changing token/term/pointer.
        ``assert_commit_allowed`` is DELIBERATELY NOT a membership gate
        (lease_runtime: "AUCUN contrôle de membership/permission") — it only
        checks token/term/pointer — so an evicted / stale-epoch local holder whose
        token still matches would otherwise produce a fresh commit under a stale
        membership view, violating the ACTIVE-membership / all-ACK model.

        This re-reads node / token / membership from the SAME store and re-applies
        the resolve-time gate. It is READ-ONLY and runs BEFORE any durable staging
        write, so a membership change between resolve and commit leaves ZERO
        durable Hivemind state. ``assert_commit_allowed`` remains the SINGLE auth
        for term/fencing; this adds only the membership-currency check.

        Load-bearing, not best-effort: ``commit()`` calls this and the subsequent
        ``stage_commit`` / ``apply_commit`` UNDER ``_membership_lock(space_id)`` —
        the same lock ``evict_member`` / ``add_member`` hold — so no local
        membership mutation can land between this check and the pointer flip. The
        only residual is a cross-node eviction reflected straight into storage
        (no local lock can fence another process); this re-read catches it once
        visible, the rest is the documented S3-no-CAS residual.

        Raises :class:`RegistryRefused` (the same typed fail-closed refusal
        ``resolve_sink`` raises) on any failure. Imported lazily to avoid the
        ``engines -> write_sink`` import cycle (``engines`` imports this module at
        module top level).
        """
        from .engines import RegistryRefused
        from .hivemind.lifecycle import WriteRoute
        from .hivemind.models import MemberStatus, TokenState

        node = await self._store.get_node_identity()
        token = await self._store.get_token()
        membership = await self._store.get_membership()

        local_is_active_member = (
            membership is not None
            and node is not None
            and any(
                m.node_id == node.node_id
                and m.status == MemberStatus.ACTIVE.value
                for m in membership.members
            )
        )
        if (
            node is None
            or token is None
            or token.state != TokenState.HELD.value
            or token.holder_node_id != node.node_id
            or node.node_id != self._local_node_id
            or membership is None
            or not local_is_active_member
            or token.membership_epoch != membership.epoch
            or self._membership_epoch != membership.epoch
        ):
            raise RegistryRefused(self.space_id, WriteRoute.STAGED)

    # ──────────────────────────────────────────────────────────────────
    # commit — the SINGLE atomic flush (assert_commit_allowed is the only auth).
    # ──────────────────────────────────────────────────────────────────

    async def commit(
        self, *, reason: str = "consolidation", notes_consumed: Optional[list[str]] = None
    ) -> "BankVersionPointer | None":
        """Flush the buffered bank writes as ONE atomic commit.

        Empty buffer -> no-op (returns ``None``; idempotent). Otherwise the WHOLE
        body (steps 1-7) runs under ``lifecycle._membership_lock(space_id)`` — the
        SAME per-(loop, space) lock ``MembershipService.add_member`` /
        ``evict_member`` hold — serializing the commit against both membership
        mutations AND other commits on the space:

        1. Read the current live bank into ``snapshot`` (rel-to-``bank/`` -> text),
           inside the lock so a follow-up commit snapshots AFTER any just-landed
           commit (no stale overwrite).
        2. Derive each buffered key's rel path via ``bank_relpath``; a key NOT
           under ``{space}/bank/`` FAILS CLOSED (``StagedWriteNotImplemented``).
        3. Replay the buffer onto ``snapshot`` in submission order (last write
           wins per rel path).
        4. ``proposed_bank`` = sorted snapshot items.
        5. Read parent pointer + term (inside the lock, so the CAS parent reflects
           any commit that landed before we took the lock); ``bank_version =
           parent + 1``.
        6. AUTHORIZE FIRST (mutation guard): build the ``CommitIntent`` and call the
           lease ``assert_commit_allowed`` — the SINGLE auth point, READ-ONLY —
           BEFORE any durable staging write. Because it runs inside the serialized
           section on the freshly re-read token/pointer, a non-holder / fenced /
           stale-term / stale-version caller (e.g. a concurrent commit landed and
           released the token / bumped the pointer) is rejected HERE, leaving ZERO
           durable Hivemind state (no ``staging/{commit_id}/`` tree, no
           MANIFEST.json, no pointer flip — never an orphan staging tree).
        6b. RE-VALIDATE MEMBERSHIP CURRENCY: re-read node / token / membership LIVE
           and confirm the LOCAL node is still ACTIVE at the CURRENT epoch and the
           token's ``membership_epoch`` matches — fail closed (``RegistryRefused``,
           ZERO durable staging) otherwise. ``assert_commit_allowed`` is NOT a
           membership gate and the resolve-time gate can be stale (membership can
           change WITHOUT touching token/term/pointer); the lock makes this check
           load-bearing — no local ``evict_member`` / ``add_member`` can interleave
           before the flip, so the staged ``membership_epoch`` is provably current
           at the linearization point.
        7. ``stage_commit`` -> ``apply_commit``; ``apply_commit`` re-runs the SAME
           gate live (G0) against freshly re-read token/term/pointer, so a lease
           expiring inside the section is still caught FENCED. Errors propagate.
        8. Release the lock; clear buffer; return the new pointer.

        Cross-node membership / commit changes written straight into shared storage
        are the documented S3-no-CAS residual — a local lock cannot fence another
        process; the live re-reads catch them only once visible.
        """
        if not self._buffer:
            return None

        bank_prefix = f"{self.space_id}/bank/"

        # WHOLE-COMMIT SERIALIZED CRITICAL SECTION. Snapshot -> proposed bank ->
        # parent/term -> assert_commit_allowed (auth prefilter) -> membership
        # re-check -> stage -> apply ALL run under ``lifecycle._membership_lock(
        # space_id)`` — the SAME per-(loop, space) lock ``MembershipService.
        # add_member`` / ``evict_member`` hold. This single section is load-bearing
        # two ways:
        #
        #  (A) MEMBERSHIP currency: no local ``evict_member`` / ``add_member`` can
        #      interleave between the membership re-check (6b) and the pointer flip,
        #      so the ``membership_epoch`` staged into the commit is provably current
        #      at the linearization point (an evicted / stale-epoch local holder
        #      cannot slip a commit through while staging).
        #  (B) COMMIT serialization: the READ-ONLY ``assert_commit_allowed``
        #      prefilter AND the live snapshot / parent / term reads run inside the
        #      SAME serialized section as the apply. So a caller whose authorization
        #      went stale after a concurrent commit landed (the winner flips the
        #      pointer and releases the token in ``apply_commit``) is refused at the
        #      prefilter — re-reading the CURRENT token/pointer — BEFORE any
        #      ``stage_commit`` write. A refused commit therefore leaves ZERO durable
        #      staging (no orphan ``staging/{commit_id}/`` tree, no MANIFEST.json),
        #      and a legitimate follow-up re-reads the CURRENT bank so it never
        #      overwrites a just-landed commit with a stale snapshot.
        #
        # Without this section the prefilter ran on a snapshot taken BEFORE the lock,
        # so two same-holder commits could both pass it, the loser would block on the
        # lock, then stage, then be rejected by ``apply_commit``'s live G0 —
        # publishing an orphan staging tree (the "zero durable state on refuse"
        # guard violated). ``apply_commit`` still re-runs the SAME G0 gate live, so a
        # lease expiring inside the section is caught FENCED. Cross-node membership /
        # commit changes written straight into shared storage are the documented
        # S3-no-CAS residual — a local lock cannot fence another process; the live
        # re-reads catch them once visible.
        from .hivemind.lease_runtime import CommitIntent
        from .hivemind.lifecycle import _membership_lock

        async with _membership_lock(self.space_id):
            # 1. Current live bank snapshot (rel-to-bank/ -> text). READ stays on
            #    storage; never a mutation. Inside the lock so a follow-up commit
            #    snapshots the bank AFTER any just-landed commit (no stale overwrite).
            snapshot: dict[str, str] = {}
            for obj in await self._storage.list_objects(bank_prefix):
                key = obj["Key"]
                if key.endswith(".keep"):
                    continue
                rel = bank_relpath(key, self.space_id)
                text = await self._storage.get(key)
                if text is not None:
                    snapshot[rel] = text

            # 2-3. Replay the buffer onto the snapshot, fail-closed on non-bank keys.
            for op in self._buffer:
                if not op.key.startswith(bank_prefix):
                    # Only {space}/bank/* can be promoted (apply_commit lands under
                    # {space}/bank/). A non-bank file (e.g. top-level _synthesis.md /
                    # _rules.md / _meta.json) cannot be staged today -> fail closed.
                    raise StagedWriteNotImplemented(op=op.kind, key=op.key)
                rel = bank_relpath(op.key, self.space_id)
                if op.kind == "put_json":
                    # Defense in depth (ADR-0012): a buffered _meta.json projects via
                    # staged_meta_text so the graph_memory block can never enter the
                    # manifest. Any other JSON is canonical-serialized to bytes.
                    from .hivemind.commit_runtime import staged_meta_text

                    if rel == "_meta.json":
                        snapshot[rel] = staged_meta_text(op.data)
                    else:
                        import json as _json

                        snapshot[rel] = _json.dumps(
                            op.data, indent=2, ensure_ascii=False
                        )
                else:
                    snapshot[rel] = op.content

            # 4. Full proposed bank (sorted for a byte-stable manifest).
            proposed_bank = sorted(snapshot.items())

            # 5. Parent pointer + term (READ, inside the lock; CorruptedStateError
            #    propagates). Re-read here so the CAS parent reflects any commit that
            #    landed before we took the lock.
            pointer = await self._store.get_bank_version_pointer()
            parent = pointer.bank_version if pointer is not None else -1
            bank_version = parent + 1
            term_state = await self._store.get_term()
            term = term_state.term if term_state is not None else 0

            # 6. AUTHORIZE FIRST — the mutation guard. assert_commit_allowed is the
            #    SINGLE auth point (ADR-0011) and is READ-ONLY; calling it here, BEFORE
            #    stage_commit AND inside the serialized section, means a non-holder /
            #    fenced / stale-term / version-conflict caller writes NO durable
            #    Hivemind state (no staging tree, no MANIFEST.json, no pointer). The
            #    intent is built from the SAME (term, bank_version, parent) read above
            #    under the lock; the CAS source is the commit parent
            #    (build_commit_intent's contract), never re-derived.
            #    CommitNotAuthorized / CorruptedStateError propagate to safe_error.
            commit_id = self._new_commit_id()
            intent = CommitIntent(
                holder_node_id=self._local_node_id,
                term=term,
                fencing_token=self._fencing_token,
                bank_version=bank_version,
                previous_bank_version=parent,
                commit_id=commit_id,
            )
            await self._lease.assert_commit_allowed(intent)

            # 6b. RE-VALIDATE MEMBERSHIP CURRENCY (live) BEFORE any stage_commit
            #     write. Fail closed (RegistryRefused, ZERO durable staging) if the
            #     local node is no longer ACTIVE at the current epoch or the token's
            #     membership_epoch no longer matches. assert_commit_allowed is NOT a
            #     membership gate; this adds the membership-currency check the commit
            #     path needs, load-bearing because the whole section is serialized.
            await self._assert_local_membership_current()

            # 7. Stage + apply, still under the lock. apply_commit re-runs the SAME
            #    G0 gate live (no term/fencing TOCTOU: a lease expiring after the
            #    pre-check is caught FENCED at apply), then promotes atomically.
            #    CommitApplyError / CorruptedStateError propagate.
            commit = await self._crt.stage_commit(
                commit_id=commit_id,
                proposed_bank=proposed_bank,
                bank_version=bank_version,
                parent_bank_version=parent,
                term=term,
                membership_epoch=self._membership_epoch,
                committed_by_node_id=self._local_node_id,
                event_id=commit_id,
                notes_consumed=list(notes_consumed or []),
            )
            new_pointer = await self._crt.apply_commit(
                commit,
                intent,
                local_node_id=self._local_node_id,
                fencing_token=self._fencing_token,
                reason=reason,
            )

        # 8. Buffer consumed.
        self._buffer = []
        return new_pointer
