# -*- coding: utf-8 -*-
"""
Tests pour issue #13 / P5-5 — runtime de lease/term/fencing + le point
d'autorisation UNIQUE ``assert_commit_allowed`` (ADR-0011 + ADR-0009).

Deux familles :

- **couche pure** : helpers de lease (``compute_lease_until`` /
  ``is_lease_expired`` / ``lease_is_active``) et le prédicat
  ``evaluate_commit_authorization`` (les 5 codes de refus ADR-0011) ;
- **machine à états** ``LeaseRuntime`` : acquire (G1 all-ACK / G2 head / G3
  exclusion mutuelle), renew, release, reconcile, et ``assert_commit_allowed``.

Fakes déterministes (``FakeStorage`` + ``DeterministicClock``), aucun transport.
Chaque test nomme la mutation RED-sans-laquelle il échouerait (pas de test
vacant). L'isolation graph/long est vérifiée par un scan AST (L22).
"""

from __future__ import annotations

import asyncio
import ast
import inspect

import pytest

from live_mem.core.hivemind import (
    BankVersionPointer,
    CommitDenyReason,
    CommitIntent,
    CommitNotAuthorized,
    CorruptedStateError,
    HivemindStateStore,
    LeaseRuntime,
    QueueEntry,
    QueueEntryStatus,
    QueueRuntime,
    TermState,
    TokenLeaseState,
    TokenState,
    compute_lease_until,
    evaluate_commit_authorization,
    is_lease_expired,
    layout,
    lease_is_active,
)
from live_mem.core.hivemind import lease_runtime as lease_runtime_module
from tests.hivemind_harness import (
    DeterministicClock,
    assert_at_most_one_valid_holder,
)
from tests.test_hivemind_state import FakeStorage


# =============================================================================
# Helpers partagés
# =============================================================================


def make_store(storage: FakeStorage, space_id: str = "alpha") -> HivemindStateStore:
    return HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]


def membership(epoch: int, *active_ids: str):
    from live_mem.core.hivemind import Member, MemberStatus, MembershipView

    members = [Member(node_id=nid, status=MemberStatus.ACTIVE) for nid in active_ids]
    return MembershipView(epoch=epoch, members=members)


async def seed_term(store: HivemindStateStore, term: int, by: str = "seed") -> None:
    await store.bump_term(term, updated_by_node_id=by)


async def seed_pointer(store: HivemindStateStore, bank_version: int) -> None:
    await store.set_bank_version_pointer(
        BankVersionPointer(bank_version=bank_version, commit_id=f"c{bank_version}")
    )


async def submit_pending(
    queue: QueueRuntime,
    *,
    event_id: str,
    requester: str,
    sequence: int,
    term: int = 1,
    epoch: int = 1,
) -> QueueEntry:
    return await queue.submit(
        event_id=event_id,
        requester_node_id=requester,
        term=term,
        membership_epoch=epoch,
        sequence=sequence,
    )


async def ack_all(
    queue: QueueRuntime, event_id: str, ackers: list[str], *, epoch: int = 1
) -> None:
    from live_mem.core.hivemind import Ack

    for nid in ackers:
        await queue.record_ack(
            Ack(event_id=event_id, ack_by_node_id=nid, membership_epoch=epoch)
        )


def held_token(
    *,
    holder: str,
    term: int,
    lease_until: str | None,
    epoch: int = 1,
    event_id: str = "evt",
) -> TokenLeaseState:
    return TokenLeaseState(
        state=TokenState.HELD,
        holder_node_id=holder,
        term=term,
        fencing_token=term,
        granted_at="2026-01-01T00:00:00+00:00",
        lease_until=lease_until,
        membership_epoch=epoch,
        event_id=event_id,
    )


def good_intent(
    *,
    holder: str = "nodeA",
    term: int = 2,
    bank_version: int = 1,
    previous_bank_version: int = 0,
) -> CommitIntent:
    return CommitIntent(
        holder_node_id=holder,
        term=term,
        fencing_token=term,
        bank_version=bank_version,
        previous_bank_version=previous_bank_version,
        commit_id="commit-x",
    )


# =============================================================================
# L1-L3 — couche pure : helpers de lease
# =============================================================================


def test_compute_lease_until_adds_ttl_and_truncates_microseconds() -> None:
    """L1 — RED si les microsecondes fuient (casse la ré-écriture byte-stable)
    ou si le TTL n'est pas ajouté ; ValueError pour ttl <= 0."""
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)
    out = compute_lease_until(now, 300)
    assert out == "2026-01-01T12:05:00+00:00"  # +300s, microseconde tronquée
    with pytest.raises(ValueError):
        compute_lease_until(now, 0)
    with pytest.raises(ValueError):
        compute_lease_until(now, -1)


def test_is_lease_expired_boundary() -> None:
    """L2 — RED si ``>`` est implémenté comme ``>=`` (off-by-one à la borne)."""
    from datetime import datetime, timedelta, timezone

    until = datetime(2026, 1, 1, 12, 5, 0, tzinfo=timezone.utc)
    token = held_token(holder="a", term=1, lease_until=until.isoformat())
    # exactement à lease_until -> ENCORE valide (demi-ouvert)
    assert is_lease_expired(token, until) is False
    # 1s après -> expirée
    assert is_lease_expired(token, until + timedelta(seconds=1)) is True
    # 1s avant -> valide
    assert is_lease_expired(token, until - timedelta(seconds=1)) is False
    # lease_until None -> jamais expirée
    none_token = TokenLeaseState(state=TokenState.FREE, lease_until=None)
    assert is_lease_expired(none_token, until) is False


def test_lease_is_active_states_and_expiry() -> None:
    """L3 — RED si ``lease_is_active`` ignore l'état ou l'expiration."""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    future = (now + timedelta(seconds=300)).isoformat()
    past = (now - timedelta(seconds=1)).isoformat()

    held = held_token(holder="a", term=1, lease_until=future)
    releasing = TokenLeaseState(
        state=TokenState.RELEASING,
        holder_node_id="a",
        term=1,
        fencing_token=1,
        lease_until=future,
    )
    free = TokenLeaseState(state=TokenState.FREE, term=1, fencing_token=1)
    held_expired = held_token(holder="a", term=1, lease_until=past)

    assert lease_is_active(held, now) is True
    assert lease_is_active(releasing, now) is True
    assert lease_is_active(free, now) is False
    assert lease_is_active(held_expired, now) is False
    assert lease_is_active(None, now) is False


def test_is_lease_expired_active_token_without_lease_until_is_corrupt() -> None:
    """L2b (fail-closed sur état critique incomplet, Codex BLOCKING) — un token
    ACTIF (HELD/RELEASING) sans ``lease_until`` n'est PAS « jamais expiré » : il
    lève ``CorruptedStateError``. RED sans le fix : ``is_lease_expired`` retournait
    ``False`` (valide à vie) pour un HELD ``lease_until=None``, ouvrant un
    fail-open. Un FREE sans ``lease_until`` reste bénin (rien à expirer)."""
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    held_no_lease = held_token(holder="a", term=1, lease_until=None)
    with pytest.raises(CorruptedStateError):
        is_lease_expired(held_no_lease, now)

    releasing_no_lease = TokenLeaseState(
        state=TokenState.RELEASING,
        holder_node_id="a",
        term=1,
        fencing_token=1,
        lease_until=None,
    )
    with pytest.raises(CorruptedStateError):
        is_lease_expired(releasing_no_lease, now)

    # FREE sans lease_until : bénin (pas une lease vivante).
    free_no_lease = TokenLeaseState(state=TokenState.FREE, lease_until=None)
    assert is_lease_expired(free_no_lease, now) is False


def test_is_lease_expired_malformed_lease_until_is_corrupt() -> None:
    """L2c (MINOR Codex — taxonomie d'erreur critique) — un ``lease_until``
    malformé (chaîne non ISO-8601) sort en ``CorruptedStateError``, pas en
    ``ValueError``/``TypeError`` nu. RED sans le fix : ``_parse_iso`` laissait
    fuir un ``ValueError`` qu'un caller gérant la corruption Hivemind ne
    reconnaîtrait pas comme unsafe/resync."""
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    malformed = held_token(holder="a", term=1, lease_until="pas-une-date")
    with pytest.raises(CorruptedStateError):
        is_lease_expired(malformed, now)


def test_lease_is_active_propagates_corrupt_active_without_lease() -> None:
    """L3b (fail-closed) — ``lease_is_active`` sur un HELD sans ``lease_until``
    PROPAGE ``CorruptedStateError`` (via ``is_lease_expired``), il ne le traite
    pas comme une lease vivante valide-à-vie. C'est la garde G3 d'``acquire`` qui
    en hérite : un token actif corrompu BLOQUE plutôt que d'autoriser un second
    grant silencieux."""
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    held_no_lease = held_token(holder="a", term=1, lease_until=None)
    with pytest.raises(CorruptedStateError):
        lease_is_active(held_no_lease, now)


# =============================================================================
# L4-L8 — acquire : machine à états (G1/G2/G3 + effets)
# =============================================================================


def _runtime(storage: FakeStorage, clock: DeterministicClock, *, ttl: int = 300):
    store = make_store(storage)
    queue = QueueRuntime(store, "alpha")
    lease = LeaseRuntime(store, "alpha", queue, clock=clock.now, ttl_seconds=ttl)
    return store, queue, lease


async def test_acquire_grants_held_token_at_bumped_term() -> None:
    """L4 — RED sans le bloc d'effet bump_term + set_token."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    await seed_term(store, 1)
    m = membership(1, "nodeA")
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-a", ["nodeA"])

    token = await lease.acquire(
        membership=m, holder_node_id="nodeA", event_id="evt-a"
    )

    assert token.state == TokenState.HELD.value
    assert token.holder_node_id == "nodeA"
    assert token.term == 2 == token.fencing_token  # bumped from get_term()+1
    assert token.event_id == "evt-a"
    assert token.lease_until is not None
    # term.json a bien été bumpé.
    term_state = await store.get_term()
    assert term_state is not None and term_state.term == 2
    # l'entrée queue est consommée (GRANTED) -> plus de head.
    assert await queue.head(m) is None


async def test_acquire_blocks_when_not_all_acked() -> None:
    """L5 — RED si G1 est lâché (progrès silencieux type quorum)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    await seed_term(store, 1)
    m = membership(1, "nodeA", "nodeB")  # 2 actifs
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-a", ["nodeA"])  # nodeB manque

    with pytest.raises(CommitNotAuthorized) as err:
        await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")
    assert err.value.reason == CommitDenyReason.BLOCKED
    # aucun effet : pas de token, term inchangé, entrée toujours PENDING.
    assert await store.get_token() is None
    assert (await store.get_term()).term == 1
    head = await queue.head(m)
    assert head is not None and head.event_id == "evt-a"


async def test_acquire_blocks_non_head_requester() -> None:
    """L6 — RED si la lease ré-dérive l'ordre au lieu de demander queue.head
    (garde ADR-0009)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    await seed_term(store, 1)
    m = membership(1, "nodeA", "nodeB")
    # Deux entrées PENDING : seq 0 (nodeA) est le head ; seq 1 (nodeB) ne l'est pas.
    await submit_pending(queue, event_id="evt-head", requester="nodeA", sequence=0)
    await submit_pending(queue, event_id="evt-tail", requester="nodeB", sequence=1)
    await ack_all(queue, "evt-tail", ["nodeA", "nodeB"])

    with pytest.raises(CommitNotAuthorized) as err:
        await lease.acquire(
            membership=m, holder_node_id="nodeB", event_id="evt-tail"
        )
    assert err.value.reason == CommitDenyReason.BLOCKED
    assert await store.get_token() is None


async def test_acquire_blocks_own_non_head_entry_head_identity_subcheck() -> None:
    """L6b — un node qui tente d'acquérir SA PROPRE entrée plus tardive alors que
    SA PROPRE entrée antérieure est le head (``requester == holder`` mais
    ``event_id != head.event_id``). Isole le sous-check d'identité du head
    (``head.event_id != event_id``) du sous-check requester : ici le holder EST
    le requester du head, donc une garde qui ne vérifierait QUE
    ``head.requester != holder`` autoriserait à tort ce grant hors-ordre. RED si
    le sous-check ``head.event_id`` était retiré."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    await seed_term(store, 1)
    m = membership(1, "nodeA")  # nodeA seul actif -> head.requester == holder
    # Deux entrées PENDING de nodeA : seq 0 (evt-1) est le head ; seq 1 (evt-2)
    # est SA propre entrée plus tardive.
    await submit_pending(queue, event_id="evt-1", requester="nodeA", sequence=0)
    await submit_pending(queue, event_id="evt-2", requester="nodeA", sequence=1)
    await ack_all(queue, "evt-2", ["nodeA"])  # all-ACK satisfait pour evt-2 (G1 passe)

    with pytest.raises(CommitNotAuthorized) as err:
        await lease.acquire(
            membership=m, holder_node_id="nodeA", event_id="evt-2"
        )
    assert err.value.reason == CommitDenyReason.BLOCKED
    # aucun grant : le head (evt-1) doit être traité d'abord.
    assert await store.get_token() is None


async def test_acquire_blocked_by_live_lease_second_claimer_fenced() -> None:
    """L7 — RED si l'exclusion mutuelle (G3) est retirée -> split-brain au
    term+1."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    await seed_term(store, 1)
    m = membership(1, "nodeA", "nodeB")
    # A acquiert d'abord.
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-a", ["nodeA", "nodeB"])
    token_a = await lease.acquire(
        membership=m, holder_node_id="nodeA", event_id="evt-a"
    )
    assert token_a.holder_node_id == "nodeA"

    # B soumet et all-ACK ; sa tentative est BLOQUÉE par la lease vivante de A.
    await submit_pending(queue, event_id="evt-b", requester="nodeB", sequence=1)
    await ack_all(queue, "evt-b", ["nodeA", "nodeB"])
    with pytest.raises(CommitNotAuthorized) as err:
        await lease.acquire(membership=m, holder_node_id="nodeB", event_id="evt-b")
    assert err.value.reason == CommitDenyReason.BLOCKED
    # le token reste celui de A (un seul HELD).
    cur = await store.get_token()
    assert cur is not None and cur.holder_node_id == "nodeA" and cur.term == 2


async def test_acquire_proceeds_after_lease_expiry() -> None:
    """L8 — RED si G3 traite une lease expirée comme encore active."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock, ttl=300)
    await seed_term(store, 1)
    m = membership(1, "nodeA", "nodeB")
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-a", ["nodeA", "nodeB"])
    await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")

    # La lease de A expire (horloge avancée au-delà du TTL).
    clock.tick(seconds=301)
    await submit_pending(queue, event_id="evt-b", requester="nodeB", sequence=1)
    await ack_all(queue, "evt-b", ["nodeA", "nodeB"])
    token_b = await lease.acquire(
        membership=m, holder_node_id="nodeB", event_id="evt-b"
    )
    assert token_b.holder_node_id == "nodeB"
    assert token_b.term == 3 == token_b.fencing_token  # term a re-bumpé


# =============================================================================
# L9-L13 — renew / release / reconcile
# =============================================================================


async def test_renew_extends_without_bumping_term() -> None:
    """L9 — RED si renew bumpe le term."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    await seed_term(store, 1)
    m = membership(1, "nodeA")
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-a", ["nodeA"])
    token = await lease.acquire(
        membership=m, holder_node_id="nodeA", event_id="evt-a"
    )
    first_until = token.lease_until

    clock.tick(seconds=100)
    renewed = await lease.renew(holder_node_id="nodeA")
    assert renewed.term == token.term == 2  # term inchangé
    assert renewed.fencing_token == token.fencing_token
    assert renewed.holder_node_id == "nodeA"
    assert renewed.lease_until is not None and renewed.lease_until != first_until
    assert (await store.get_term()).term == 2  # term.json inchangé


async def test_renew_expired_lease_is_fenced() -> None:
    """L10 — RED si une lease expirée peut être renouvelée en place."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock, ttl=300)
    await seed_term(store, 1)
    m = membership(1, "nodeA")
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-a", ["nodeA"])
    token = await lease.acquire(
        membership=m, holder_node_id="nodeA", event_id="evt-a"
    )

    clock.tick(seconds=301)  # lease expirée
    with pytest.raises(CommitNotAuthorized) as err:
        await lease.renew(holder_node_id="nodeA")
    assert err.value.reason == CommitDenyReason.FENCED
    # token inchangé.
    cur = await store.get_token()
    assert cur is not None and cur.lease_until == token.lease_until


async def test_renew_non_holder_denied() -> None:
    """L11 — RED si renew saute le contrôle du holder."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    await seed_term(store, 1)
    m = membership(1, "nodeA")
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-a", ["nodeA"])
    await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")

    with pytest.raises(CommitNotAuthorized) as err:
        await lease.renew(holder_node_id="nodeB")
    assert err.value.reason == CommitDenyReason.NOT_HOLDER


async def test_release_frees_preserving_term_and_fencing() -> None:
    """L12 — RED si release remet term/fencing à zéro (rejet monotone /
    régression de fencing)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    await seed_term(store, 1)
    m = membership(1, "nodeA")
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-a", ["nodeA"])
    token = await lease.acquire(
        membership=m, holder_node_id="nodeA", event_id="evt-a"
    )

    freed = await lease.release(holder_node_id="nodeA")
    assert freed.state == TokenState.FREE.value
    assert freed.holder_node_id is None
    assert freed.term == token.term == 2  # term/fencing préservés
    assert freed.fencing_token == token.fencing_token == 2

    # re-release idempotent (byte-stable no-op, pas de rejet monotone).
    before = storage.snapshot()
    freed2 = await lease.release(holder_node_id="nodeA")
    assert freed2.state == TokenState.FREE.value
    assert storage.snapshot()[layout.token_key("alpha")] == before[
        layout.token_key("alpha")
    ]


async def test_reconcile_stale_holder_demotes_superseded_held() -> None:
    """L13 — RED si un HELD superseded reste actif (STALE-HOLDER de l'oracle).

    Le holder superseded porte une lease STRUCTURELLEMENT VALIDE (``lease_until``
    parsable) : la démotion se fait par TERM (supersession), pas par corruption.
    Un actif au ``lease_until`` corrompu est un cas SÉPARÉ (fail-closed) couvert
    par ``test_reconcile_stale_holder_corrupt_active_fails_closed`` ci-dessous —
    on ne le mélange pas avec le chemin de démotion légitime.
    """
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    valid_until = clock.iso()
    # HELD au term 2, mais term.json a avancé à 4 (un autre a pris la main).
    await seed_term(store, 2)
    await store.set_token(
        held_token(holder="nodeA", term=2, lease_until=valid_until)
    )
    await store.bump_term(4, updated_by_node_id="nodeB")

    reconciled = await lease.reconcile_stale_holder()
    assert reconciled is not None
    assert reconciled.state == TokenState.FREE.value
    assert reconciled.holder_node_id is None
    assert reconciled.term == 4 == reconciled.fencing_token  # remonté au courant

    # no-op quand déjà FREE.
    again = await lease.reconcile_stale_holder()
    assert again is not None and again.state == TokenState.FREE.value

    # no-op quand absent (nouveau store vierge).
    s2 = FakeStorage()
    store2, queue2, lease2 = _runtime(s2, clock)
    assert await lease2.reconcile_stale_holder() is None

    # no-op quand HELD au term courant (légitime) — lease VALIDE.
    s3 = FakeStorage()
    store3, queue3, lease3 = _runtime(s3, clock)
    await seed_term(store3, 3)
    await store3.set_token(
        held_token(holder="nodeC", term=3, lease_until=clock.iso())
    )
    kept = await lease3.reconcile_stale_holder()
    assert kept is not None and kept.state == TokenState.HELD.value
    assert kept.holder_node_id == "nodeC"


async def test_release_corrupt_active_without_lease_until_fails_closed() -> None:
    """L13b — un token ACTIF (HELD) au ``lease_until=None`` (état critique
    incomplet) ne peut PAS être libéré en FREE : ``release()`` doit FAIL-CLOSED
    en ``CorruptedStateError`` (mêmes garanties que les gates acquire/assert),
    jamais réparer/effacer silencieusement la corruption.

    RED sans le fix : ``release()`` ne validait que l'appartenance du holder pour
    les états actifs puis écrivait un FREE, transformant un
    ``HELD(term=2, lease_until=None)`` corrompu en FREE (corruption silencieuse).
    """
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    await seed_term(store, 2)
    # HELD corrompu : actif mais sans borne de lease.
    await store.set_token(held_token(holder="nodeA", term=2, lease_until=None))

    with pytest.raises(CorruptedStateError):
        await lease.release(holder_node_id="nodeA")
    # Aucune réparation : le token corrompu reste tel quel, jamais transformé en
    # FREE.
    after = await store.get_token()
    assert after is not None
    assert after.state == TokenState.HELD.value
    assert after.lease_until is None


async def test_release_corrupt_active_malformed_lease_until_fails_closed() -> None:
    """L13c — variante : un ``lease_until`` malformé (non ISO-8601) sur un actif
    sort aussi en ``CorruptedStateError`` à la release (via la MÊME validation de
    lease que les gates), jamais silencieusement libéré.

    RED sans le fix : ``release()`` ne parsait jamais ``lease_until`` et écrivait
    un FREE pour un holder qui matche.
    """
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    await seed_term(store, 2)
    await store.set_token(
        held_token(holder="nodeA", term=2, lease_until="pas-une-date")
    )

    with pytest.raises(CorruptedStateError):
        await lease.release(holder_node_id="nodeA")
    after = await store.get_token()
    assert after is not None and after.state == TokenState.HELD.value


async def test_reconcile_stale_holder_corrupt_active_fails_closed() -> None:
    """L13d — un token ACTIF (HELD) au ``lease_until=None`` n'est NI un holder
    courant légitime à préserver, NI un stale à effacer en FREE :
    ``reconcile_stale_holder()`` doit FAIL-CLOSED en ``CorruptedStateError``,
    PAS retourner un FREE/HELD silencieux.

    RED sans le fix : ``reconcile_stale_holder()`` traitait un HELD au term
    courant comme légitime et le retournait inchangé (no-op silencieux) — la
    corruption ``lease_until=None`` n'était jamais détectée. Couvre les deux
    branches (term courant légitime ET superseded).
    """
    clock = DeterministicClock()

    # Branche 1 : HELD au term COURANT (autrefois « légitime », no-op silencieux).
    s_keep = FakeStorage()
    store_keep, _q, lease_keep = _runtime(s_keep, clock)
    await seed_term(store_keep, 3)
    await store_keep.set_token(
        held_token(holder="nodeC", term=3, lease_until=None)
    )
    with pytest.raises(CorruptedStateError):
        await lease_keep.reconcile_stale_holder()
    # Jamais effacé/réparé : le HELD corrompu reste, ne devient pas FREE.
    after_keep = await store_keep.get_token()
    assert after_keep is not None
    assert after_keep.state == TokenState.HELD.value
    assert after_keep.lease_until is None

    # Branche 2 : HELD SUPERSEDED (autrefois démoté SILENCIEUSEMENT en FREE).
    s_demote = FakeStorage()
    store_demote, _q2, lease_demote = _runtime(s_demote, clock)
    await seed_term(store_demote, 2)
    await store_demote.set_token(
        held_token(holder="nodeA", term=2, lease_until=None)
    )
    await store_demote.bump_term(4, updated_by_node_id="nodeB")
    with pytest.raises(CorruptedStateError):
        await lease_demote.reconcile_stale_holder()
    # La corruption remonte AVANT toute démotion : pas de FREE silencieux.
    after_demote = await store_demote.get_token()
    assert after_demote is not None
    assert after_demote.state == TokenState.HELD.value


async def test_reconcile_stale_holder_corrupt_malformed_lease_fails_closed() -> None:
    """L13e — variante malformée : un ``lease_until`` non ISO-8601 sur un actif
    fait FAIL-CLOSED ``reconcile_stale_holder()`` en ``CorruptedStateError``.

    RED sans le fix : ``reconcile_stale_holder()`` ne parsait jamais
    ``lease_until``.
    """
    clock = DeterministicClock()
    storage = FakeStorage()
    store, _q, lease = _runtime(storage, clock)
    await seed_term(store, 3)
    await store.set_token(
        held_token(holder="nodeC", term=3, lease_until="pas-une-date")
    )
    with pytest.raises(CorruptedStateError):
        await lease.reconcile_stale_holder()


# =============================================================================
# L14-L21 — assert_commit_allowed / evaluate_commit_authorization (ADR a-e)
# =============================================================================


async def _seeded_holder_runtime(
    storage: FakeStorage,
    clock: DeterministicClock,
    *,
    term: int = 2,
    bank_version: int = 0,
    lease_seconds_ahead: int = 300,
    holder: str = "nodeA",
):
    """Pose un token HELD vivant par ``holder`` au ``term``, term.json == term,
    pointeur bank_version == ``bank_version``. Retourne (store, lease)."""
    store = make_store(storage)
    queue = QueueRuntime(store, "alpha")
    lease = LeaseRuntime(store, "alpha", queue, clock=clock.now)
    await seed_term(store, term)
    await seed_pointer(store, bank_version)
    from datetime import timedelta

    until = (clock.now() + timedelta(seconds=lease_seconds_ahead)).isoformat()
    await store.set_token(held_token(holder=holder, term=term, lease_until=until))
    return store, lease


async def test_assert_commit_allowed_authorizes_current_holder() -> None:
    """L14 (ADR b) — RED si le prédicat est inversé ou écrit l'état."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, lease = await _seeded_holder_runtime(storage, clock, term=2, bank_version=0)
    intent = good_intent(holder="nodeA", term=2, bank_version=1, previous_bank_version=0)

    before = storage.snapshot()
    assert await lease.assert_commit_allowed(intent) is None
    # rejouable, idempotent, AUCUN write.
    assert await lease.assert_commit_allowed(intent) is None
    assert storage.snapshot() == before


async def test_assert_commit_denied_not_holder() -> None:
    """L15 (ADR a) — FREE puis HELD-par-autre -> NOT_HOLDER."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store = make_store(storage)
    queue = QueueRuntime(store, "alpha")
    lease = LeaseRuntime(store, "alpha", queue, clock=clock.now)
    await seed_term(store, 2)
    await seed_pointer(store, 0)
    intent = good_intent(holder="nodeA", term=2, bank_version=1, previous_bank_version=0)

    # token FREE -> NOT_HOLDER
    await store.set_token(
        TokenLeaseState(state=TokenState.FREE, term=2, fencing_token=2)
    )
    with pytest.raises(CommitNotAuthorized) as err:
        await lease.assert_commit_allowed(intent)
    assert err.value.reason == CommitDenyReason.NOT_HOLDER

    # HELD par un AUTRE node -> NOT_HOLDER
    from datetime import timedelta

    until = (clock.now() + timedelta(seconds=300)).isoformat()
    await store.set_token(held_token(holder="nodeB", term=2, lease_until=until))
    with pytest.raises(CommitNotAuthorized) as err2:
        await lease.assert_commit_allowed(intent)
    assert err2.value.reason == CommitDenyReason.NOT_HOLDER


async def test_assert_commit_denied_stale_term_on_supersession() -> None:
    """L16 (ADR a / §6.2) — HELD au term 2, term.json bumpé à 5, intent term-2
    -> STALE_TERM."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, lease = await _seeded_holder_runtime(storage, clock, term=2, bank_version=0)
    # term.json bumpé à 5 (le holder est désormais superseded).
    await store.bump_term(5, updated_by_node_id="nodeB")
    intent = good_intent(holder="nodeA", term=2, bank_version=1, previous_bank_version=0)

    with pytest.raises(CommitNotAuthorized) as err:
        await lease.assert_commit_allowed(intent)
    assert err.value.reason == CommitDenyReason.STALE_TERM


async def test_assert_commit_denied_fenced_on_expiry() -> None:
    """L17 (§F) — HELD au term COURANT mais lease expirée -> FENCED (l'ordre
    BLOCKED<NOT_HOLDER<STALE_TERM<FENCED<VERSION_CONFLICT est porteur)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, lease = await _seeded_holder_runtime(
        storage, clock, term=2, bank_version=0, lease_seconds_ahead=300
    )
    intent = good_intent(holder="nodeA", term=2, bank_version=1, previous_bank_version=0)

    # term.json reste à 2 (pas de supersession) ; seule l'horloge avance.
    clock.tick(seconds=301)
    with pytest.raises(CommitNotAuthorized) as err:
        await lease.assert_commit_allowed(intent)
    assert err.value.reason == CommitDenyReason.FENCED


async def test_assert_commit_denied_version_conflict() -> None:
    """L18 — tout courant mais previous_bank_version != pointeur -> VERSION_CONFLICT."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, lease = await _seeded_holder_runtime(storage, clock, term=2, bank_version=3)
    # le holder croit committer sur parent 0, mais le pointeur vivant est à 3.
    intent = good_intent(holder="nodeA", term=2, bank_version=4, previous_bank_version=0)

    with pytest.raises(CommitNotAuthorized) as err:
        await lease.assert_commit_allowed(intent)
    assert err.value.reason == CommitDenyReason.VERSION_CONFLICT


@pytest.mark.parametrize("missing", ["token", "term", "pointer"])
async def test_assert_commit_blocked_when_state_absent(missing: str) -> None:
    """L19 (ADR a, fail-closed) — état critique absent -> fail-closed, jamais
    default-allow. ``token`` / ``pointer`` absent -> BLOCKED (état non initialisé).
    ``term`` absent SOUS un token ACTIF -> ``CorruptedStateError`` (Codex MEDIUM
    head a0c51c2) : un token actif IMPLIQUE qu'un term a été bumpé, donc un term.json
    absent est une CORRUPTION, pas un BLOCKED ordinaire — cohérent avec
    acquire/renew/release/reconcile. (Un ``pointer`` absent, lui, n'est PAS impliqué
    par un token actif : pas de commit encore -> BLOCKED légitime.)"""
    storage = FakeStorage()
    clock = DeterministicClock()
    store = make_store(storage)
    queue = QueueRuntime(store, "alpha")
    lease = LeaseRuntime(store, "alpha", queue, clock=clock.now)
    from datetime import timedelta

    until = (clock.now() + timedelta(seconds=300)).isoformat()
    if missing != "token":
        await store.set_token(held_token(holder="nodeA", term=2, lease_until=until))
    if missing != "term":
        await seed_term(store, 2)
    if missing != "pointer":
        await seed_pointer(store, 0)

    intent = good_intent(holder="nodeA", term=2, bank_version=1, previous_bank_version=0)
    if missing == "term":
        # token HELD actif + term.json absent = état critique incomplet (corruption).
        with pytest.raises(CorruptedStateError):
            await lease.assert_commit_allowed(intent)
    else:
        with pytest.raises(CommitNotAuthorized) as err:
            await lease.assert_commit_allowed(intent)
        assert err.value.reason == CommitDenyReason.BLOCKED


@pytest.mark.parametrize("target", ["token", "term", "pointer"])
async def test_assert_commit_corruption_propagates(target: str) -> None:
    """L20 (ADR c) — un objet corrompu -> CorruptedStateError PROPAGE, jamais
    CommitNotAuthorized, jamais default-allow."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, lease = await _seeded_holder_runtime(storage, clock, term=2, bank_version=0)
    intent = good_intent(holder="nodeA", term=2, bank_version=1, previous_bank_version=0)

    key = {
        "token": layout.token_key("alpha"),
        "term": layout.term_key("alpha"),
        "pointer": layout.bank_version_key("alpha"),
    }[target]
    storage.objects[key] = "{not valid json"

    with pytest.raises(CorruptedStateError):
        await lease.assert_commit_allowed(intent)


async def test_assert_commit_denied_held_without_lease_until_fail_closed() -> None:
    """L20b (Codex BLOCKING) — un token HELD au term/fencing/pointeur COURANTS,
    tenu par l'asserter, mais SANS ``lease_until`` (état critique incomplet) est
    REFUSÉ fail-closed par le point d'autorisation unique : ``CorruptedStateError``
    propage, jamais ``None`` (autorisé).

    RED sans le fix : ``is_lease_expired`` renvoyait ``False`` pour
    ``lease_until=None`` -> l'étape FENCED passait et ``assert_commit_allowed``
    AUTORISAIT le commit indéfiniment (fail-open sur token actif sans borne de
    lease). Le reste de la chaîne (NOT_HOLDER / STALE_TERM / VERSION_CONFLICT) est
    délibérément satisfait pour isoler EXACTEMENT le trou de la lease."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store = make_store(storage)
    queue = QueueRuntime(store, "alpha")
    lease = LeaseRuntime(store, "alpha", queue, clock=clock.now)
    await seed_term(store, 2)
    await seed_pointer(store, 0)
    # HELD courant (holder/term/fencing/pointeur alignés) MAIS lease_until=None.
    await store.set_token(held_token(holder="nodeA", term=2, lease_until=None))
    intent = good_intent(holder="nodeA", term=2, bank_version=1, previous_bank_version=0)

    with pytest.raises(CorruptedStateError):
        await lease.assert_commit_allowed(intent)


async def test_assert_commit_denied_held_with_malformed_lease_until() -> None:
    """L20c (MINOR Codex) — un HELD courant avec un ``lease_until`` malformé
    (non ISO-8601) est REFUSÉ fail-closed via ``CorruptedStateError`` au point
    d'autorisation unique, pas via un ``ValueError`` nu. RED sans le fix : le
    parse laissait fuir un ``ValueError`` hors de la taxonomie de corruption."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store = make_store(storage)
    queue = QueueRuntime(store, "alpha")
    lease = LeaseRuntime(store, "alpha", queue, clock=clock.now)
    await seed_term(store, 2)
    await seed_pointer(store, 0)
    await store.set_token(
        held_token(holder="nodeA", term=2, lease_until="pas-une-date")
    )
    intent = good_intent(holder="nodeA", term=2, bank_version=1, previous_bank_version=0)

    with pytest.raises(CorruptedStateError):
        await lease.assert_commit_allowed(intent)


def test_evaluate_corrupt_active_wrong_holder_surfaces_corruption_not_not_holder() -> None:
    """L20d (Codex BLOCKING #1) — un token ACTIF CORROMPU (``lease_until=None``)
    tenu par ``nodeB``, face à un intent de ``nodeA``, DOIT remonter en
    ``CorruptedStateError`` — JAMAIS être classé en refus ordinaire
    ``CommitNotAuthorized(NOT_HOLDER)``.

    RED sans le fix : la garde holder (``token.holder_node_id != intent.holder``)
    tournait AVANT la validation structurelle, donc un HELD corrompu tenu par un
    autre tombait en ``NOT_HOLDER`` et la corruption ``lease_until=None`` n'était
    jamais inspectée (taxonomie fail-closed violée — corruption masquée en déni
    de routine). Le fix exécute ``assert_active_lease_structural`` en TÊTE.
    """
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Actif corrompu (lease_until=None) tenu par nodeB ; intent de nodeA.
    corrupt = held_token(holder="nodeB", term=2, lease_until=None)
    intent = good_intent(holder="nodeA", term=2, bank_version=1, previous_bank_version=0)

    with pytest.raises(CorruptedStateError):
        evaluate_commit_authorization(
            token=corrupt,
            term=TermState(term=2),
            pointer=BankVersionPointer(bank_version=0, commit_id="c0"),
            intent=intent,
            now=now,
        )


async def test_assert_commit_corrupt_active_wrong_holder_not_masked_as_not_holder() -> None:
    """L20e (Codex BLOCKING #1, bout-en-bout) — même trou au point d'autorisation
    unique : un ``token.json`` HELD corrompu (``lease_until=None``) tenu par
    ``nodeB`` face à un intent de ``nodeA`` propage ``CorruptedStateError``, pas
    un ``CommitNotAuthorized(NOT_HOLDER)``.

    RED sans le fix : ``assert_commit_allowed`` déléguait à un prédicat qui
    refusait NOT_HOLDER avant d'inspecter la lease corrompue.
    """
    storage = FakeStorage()
    clock = DeterministicClock()
    store = make_store(storage)
    queue = QueueRuntime(store, "alpha")
    lease = LeaseRuntime(store, "alpha", queue, clock=clock.now)
    await seed_term(store, 2)
    await seed_pointer(store, 0)
    await store.set_token(held_token(holder="nodeB", term=2, lease_until=None))
    intent = good_intent(holder="nodeA", term=2, bank_version=1, previous_bank_version=0)

    with pytest.raises(CorruptedStateError):
        await lease.assert_commit_allowed(intent)


def test_active_token_without_holder_fails_closed() -> None:
    """L13f (Codex BLOCKING #2) — un token ACTIF (HELD) au ``lease_until`` VALIDE
    mais SANS ``holder_node_id`` (``None`` ou chaîne vide) est un état critique
    incomplet : la validation structurelle fail-closed lève
    ``CorruptedStateError``, jamais une lease vivante anonyme.

    RED sans le fix : ``assert_active_lease_structural`` ne validait QUE
    ``lease_until`` ; un HELD holderless avec une borne de lease valide passait
    silencieusement, et ``reconcile_stale_holder``/``release`` pouvaient l'effacer
    en FREE (corruption silencieuse). Couvre ``holder=None`` ET ``holder=""``.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    until = (now + timedelta(seconds=300)).isoformat()
    for holder in (None, ""):
        holderless = TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id=holder,
            term=2,
            fencing_token=2,
            lease_until=until,
        )
        with pytest.raises(CorruptedStateError):
            lease_runtime_module.assert_active_lease_structural(holderless, now)
        # Et via le helper de lease lui-même (source unique de validité).
        with pytest.raises(CorruptedStateError):
            is_lease_expired(holderless, now)


async def test_assert_commit_active_token_without_holder_fails_closed() -> None:
    """L20f (Codex BLOCKING #2, bout-en-bout) — un HELD holderless (lease valide)
    au point d'autorisation unique propage ``CorruptedStateError``, jamais
    ``None`` (autorisé) ni un refus à code de raison.

    RED sans le fix : le prédicat n'inspectait pas l'identité du holder sur un
    actif structurellement, donc un holder absent passait la chaîne d'égalité du
    term puis l'expiry sans jamais remonter la corruption.
    """
    storage = FakeStorage()
    clock = DeterministicClock()
    store = make_store(storage)
    queue = QueueRuntime(store, "alpha")
    lease = LeaseRuntime(store, "alpha", queue, clock=clock.now)
    await seed_term(store, 2)
    await seed_pointer(store, 0)
    from datetime import timedelta

    until = (clock.now() + timedelta(seconds=300)).isoformat()
    await store.set_token(
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id=None,
            term=2,
            fencing_token=2,
            lease_until=until,
        )
    )
    # intent.holder == "" matcherait token.holder=None ? Non : on garde un intent
    # plausible ; la corruption remonte AVANT toute comparaison de holder.
    intent = good_intent(holder="nodeA", term=2, bank_version=1, previous_bank_version=0)

    with pytest.raises(CorruptedStateError):
        await lease.assert_commit_allowed(intent)


def test_peer_rederivation_rejects_self_authorized_stale_commit() -> None:
    """L21 (ADR e) — un intent valide chez l'émetteur (term bas) est rejeté
    STALE_TERM par le pair re-dérivant contre SON propre term plus haut."""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    until = (now + timedelta(seconds=300)).isoformat()
    # L'émetteur s'est auto-autorisé au term 2 (état stale).
    sender_intent = CommitIntent(
        holder_node_id="nodeA",
        term=2,
        fencing_token=2,
        bank_version=1,
        previous_bank_version=0,
        commit_id="stale",
    )
    # Le pair charge SON propre état : term vivant 5, son token HELD au term 5.
    peer_token = held_token(holder="nodeB", term=5, lease_until=until)
    peer_term = TermState(term=5)
    peer_pointer = BankVersionPointer(bank_version=0, commit_id="c0")

    with pytest.raises(CommitNotAuthorized) as err:
        evaluate_commit_authorization(
            token=peer_token,
            term=peer_term,
            pointer=peer_pointer,
            intent=sender_intent,
            now=now,
        )
    # NOT_HOLDER serait aussi un refus (holder mismatch), mais l'important est
    # que ce ne soit JAMAIS autorisé ; le term vivant supérieur garantit le rejet.
    assert err.value.reason in (
        CommitDenyReason.NOT_HOLDER,
        CommitDenyReason.STALE_TERM,
    )

    # Cas isolé du term seul : même holder, même tout SAUF le term vivant.
    peer_token_same_holder = held_token(holder="nodeA", term=5, lease_until=until)
    with pytest.raises(CommitNotAuthorized) as err2:
        evaluate_commit_authorization(
            token=peer_token_same_holder,
            term=peer_term,
            pointer=peer_pointer,
            intent=sender_intent,
            now=now,
        )
    assert err2.value.reason == CommitDenyReason.STALE_TERM


# =============================================================================
# L22-L23 — isolation (scan AST) + gardes constructeur
# =============================================================================


def test_lease_runtime_does_not_import_graph_or_consolidation() -> None:
    """L22 — RED si un import graph/long entre dans le chemin de validité du
    commit (invariant ADR-0011). Scan AST des imports, plus robuste qu'un
    substring de docstring."""
    source = inspect.getsource(lease_runtime_module)
    forbidden = ("graph_push", "consolidation_queue", "consolidator", "long")

    imported_modules: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported_modules.append(node.module or "")
            imported_modules += [alias.name for alias in node.names]
    for mod in imported_modules:
        for needle in forbidden:
            assert needle not in mod, (
                f"lease_runtime importe interdit: {mod!r} (contient {needle!r})"
            )


def test_space_id_mismatch_rejected() -> None:
    """L23 — RED si les gardes constructeur sont lâchées."""
    storage = FakeStorage()
    store = make_store(storage, space_id="alpha")
    queue = QueueRuntime(store, "alpha")
    # space_id != store.space_id
    with pytest.raises(ValueError):
        LeaseRuntime(store, "beta", queue)
    # space_id != queue.space_id
    store_beta = make_store(FakeStorage(), space_id="beta")
    queue_beta = QueueRuntime(store_beta, "beta")
    with pytest.raises(ValueError):
        LeaseRuntime(store, "alpha", queue_beta)
    # ttl_seconds <= 0
    with pytest.raises(ValueError):
        LeaseRuntime(store, "alpha", queue, ttl_seconds=0)


# =============================================================================
# L24 — atomicité d'acquire sous concurrence (issue #13 : verrou par-space)
# =============================================================================


class _GatedStorage:
    """
    Décorateur de ``FakeStorage`` qui (a) cède le contrôle à la boucle sur
    CHAQUE opération (``await asyncio.sleep(0)``) pour que deux coroutines
    s'entrelacent réellement, et (b) PARQUE la 1ʳᵉ écriture de ``token.json`` sur
    un ``asyncio.Event`` jusqu'à libération explicite.

    C'est l'instrumentation déterministe du trou check-then-act d'``acquire`` :
    le 1ᵉʳ ``acquire`` à atteindre ``set_token`` (write token.json) se bloque
    APRÈS avoir bumpé le term mais AVANT d'écrire son HELD — et, depuis le
    réordonnancement never-orphan, AVANT de consommer la head (``mark_granted``
    est désormais le DERNIER effet). Le verrou par-space rend toute la section
    critique atomique : le 2ᵉ ``acquire`` n'entre pas tant que le 1ᵉʳ n'a pas
    relâché, voit alors la lease HELD vivante et est refusé G3 (BLOCKED). Sans le
    verrou, le 2ᵉ resterait de toute façon bloqué par G2 (la head n'est pas
    consommée tant que le grant n'est pas committé) — le verrou est la barrière
    in-process explicite et la défense-en-profondeur.
    """

    def __init__(self, inner: FakeStorage, *, armed: bool = True) -> None:
        self._inner = inner
        self.objects = inner.objects
        self._gate = asyncio.Event()  # libère le writer parqué
        self.parked = asyncio.Event()  # signale qu'un writer est parqué
        # ne parque QUE la 1ʳᵉ écriture token.json une fois armé. armed=False
        # permet d'écrire le seeding (token.json initial) sans parking, puis
        # d'armer juste avant la séquence concurrente sous test.
        self._armed = armed

    def arm(self) -> None:
        self._armed = True

    async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
        await asyncio.sleep(0)
        if self._armed and key.endswith("/token.json"):
            self._armed = False
            self.parked.set()
            await self._gate.wait()  # parqué dans le trou check-then-act
        await self._inner.put(key, content, content_type)

    def release(self) -> None:
        self._gate.set()

    async def put_json(self, key: str, data: dict) -> None:
        import json

        await self.put(key, json.dumps(data, indent=2, ensure_ascii=False))

    async def get(self, key: str) -> str | None:
        await asyncio.sleep(0)
        return await self._inner.get(key)

    async def get_json(self, key: str) -> dict | None:
        raw = await self.get(key)
        return None if raw is None else __import__("json").loads(raw)

    async def delete(self, key: str) -> None:
        await asyncio.sleep(0)
        await self._inner.delete(key)

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        await asyncio.sleep(0)
        return await self._inner.list_objects(prefix, max_keys)

    async def exists(self, key: str) -> bool:
        await asyncio.sleep(0)
        return await self._inner.exists(key)


class _SingleNodeCluster:
    """Shim cluster mono-nœud pour l'oracle ``assert_at_most_one_valid_holder``
    (qui ne lit que ``node_ids()`` + ``nodes[nid].store.get_token()``)."""

    def __init__(self, store: HivemindStateStore) -> None:
        from types import SimpleNamespace

        self.nodes = {"nodeA": SimpleNamespace(store=store)}

    def node_ids(self) -> list[str]:
        return sorted(self.nodes.keys())


async def test_acquire_is_atomic_under_concurrent_calls_single_holder() -> None:
    """L24 — atomicité IN-PROCESS sous concurrence : deux ``acquire`` concurrents
    sur le MÊME store/runtime, pour deux entrées PENDING all-ACKées. Le 1ᵉʳ
    ``acquire`` (A) entre dans la section critique sous le verrou par-space, bumpe
    le term, PUIS se parque au write de token.json (AVANT son HELD et AVANT
    ``mark_granted``, désormais le dernier effet). Tant qu'A est parqué il TIENT le
    verrou : le 2ᵉ ``acquire`` (B) ne peut pas entrer. À la libération, A écrit son
    HELD puis consomme sa head ; B entre alors, voit la lease vivante et est refusé
    G3 (BLOCKED). Invariant vérifié : exactement UN grant, term bumpé UNE fois, UNE
    entrée GRANTED, au plus un holder valide (oracle).

    RED sans le verrou : deux ``acquire`` s'entrelaceraient dans la section
    critique. (Depuis le réordonnancement ``mark_granted``-en-dernier, G2 — head
    non consommée tant que le grant n'est pas committé — bloquerait aussi B ; le
    verrou reste la barrière in-process explicite testée ici.)

    Déterminisme : ``_GatedStorage`` parque le 1ᵉʳ writer de token.json ; le 2ᵉ
    ``acquire`` n'est LANCÉ qu'une fois ce parking atteint (``parked``). Aucune
    horloge murale."""
    gated = _GatedStorage(FakeStorage())
    clock = DeterministicClock()
    store = make_store(gated)  # type: ignore[arg-type]
    queue = QueueRuntime(store, "alpha")
    lease = LeaseRuntime(store, "alpha", queue, clock=clock.now, ttl_seconds=300)

    await seed_term(store, 1)
    m = membership(1, "nodeA", "nodeB")
    # Deux entrées head-éligibles, toutes deux all-ACKées : A (seq 0), B (seq 1).
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await submit_pending(queue, event_id="evt-b", requester="nodeB", sequence=1)
    await ack_all(queue, "evt-a", ["nodeA", "nodeB"])
    await ack_all(queue, "evt-b", ["nodeA", "nodeB"])

    # 1ᵉʳ acquire (A) : lancé seul ; il bumpe le term PUIS se parque au write de
    # token.json (AVANT son HELD et AVANT mark_granted, désormais le dernier effet).
    task_a = asyncio.ensure_future(
        lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")
    )
    await gated.parked.wait()  # A parqué : term bumpé, token pas écrit, evt-a ENCORE PENDING

    # 2ᵉ acquire (B) : lancé pendant qu'A tient le verrou (parqué dans la section
    # critique). AVEC verrou il attend qu'A relâche, puis voit la lease vivante
    # -> BLOCKED. On le laisse atteindre son point de blocage (le verrou) avant de
    # libérer A.
    task_b = asyncio.ensure_future(
        lease.acquire(membership=m, holder_node_id="nodeB", event_id="evt-b")
    )
    for _ in range(50):
        await asyncio.sleep(0)

    gated.release()  # A écrit enfin son token HELD
    results = await asyncio.gather(task_a, task_b, return_exceptions=True)

    grants = [r for r in results if isinstance(r, TokenLeaseState)]
    denials = [r for r in results if isinstance(r, CommitNotAuthorized)]

    # Exactement UN grant ; l'autre acquire est refusé BLOCKED (exclusion
    # mutuelle G3), jamais un second HELD ni une RuntimeError de monotonie.
    assert len(grants) == 1, f"attendu 1 grant, obtenu {len(grants)} ({results!r})"
    assert len(denials) == 1, f"attendu 1 refus, obtenu {len(denials)} ({results!r})"
    assert denials[0].reason == CommitDenyReason.BLOCKED

    # Oracle cross-nœud (mono-store) : au plus un holder valide.
    await assert_at_most_one_valid_holder(_SingleNodeCluster(store))

    # Le term n'a été bumpé qu'UNE fois (un seul grant consommé) : 1 -> 2.
    term_state = await store.get_term()
    assert term_state is not None and term_state.term == 2

    # Une SEULE entrée de queue est sortie de l'éligibilité (GRANTED) : l'autre
    # reste PENDING (le second acquire a été bloqué avant tout effet).
    entries = await store.list_queue()
    granted = [e for e in entries if e.status == QueueEntryStatus.GRANTED.value]
    assert len(granted) == 1, f"attendu 1 entrée GRANTED, obtenu {granted!r}"


# =============================================================================
# L25-L28 — recouvrabilité durable d'acquire (never-drop/never-orphan) +
# reprise idempotente + renew structural-first (Codex BLOCKING+MINOR head
# 62e71dbc : grant à trois écritures durables non transactionnelles).
# =============================================================================


class _InjectedFault(RuntimeError):
    """Panne d'écriture durable injectée (crash / erreur store EN COURS de
    séquence ``acquire``). Sert à prouver la recouvrabilité never-orphan : aucune
    panne entre les trois écritures durables (term.json / token.json / entrée
    queue) ne doit consommer la requête sans établir de holder."""


class _FaultStorage:
    """Décorateur de ``FakeStorage`` qui, une fois ARMÉ, lève ``_InjectedFault``
    sur la PREMIÈRE écriture (``put``) dont la clé matche ``key_suffix`` /
    ``key_contains`` ET dont le contenu contient ``content_needle`` (chaque
    critère optionnel), puis se DÉSARME (le retry réussit). Démarre DÉSARMÉ pour
    que le seeding (term / queue / acks) s'écrive sans panne ; on arme juste avant
    l'``acquire`` sous test. Tous les writes du store passent par
    ``_put_model`` -> ``put_json`` -> ``put`` : intercepter ``put`` suffit."""

    def __init__(self, inner: FakeStorage) -> None:
        self._inner = inner
        self.objects = inner.objects
        self._armed = False
        self._key_suffix: str | None = None
        self._key_contains: str | None = None
        self._content_needle: str | None = None
        self.injected = 0

    def arm(
        self,
        *,
        key_suffix: str | None = None,
        key_contains: str | None = None,
        content_needle: str | None = None,
    ) -> None:
        self._armed = True
        self._key_suffix = key_suffix
        self._key_contains = key_contains
        self._content_needle = content_needle

    def _hits(self, key: str, content: str) -> bool:
        if self._key_suffix is not None and not key.endswith(self._key_suffix):
            return False
        if self._key_contains is not None and self._key_contains not in key:
            return False
        if self._content_needle is not None and self._content_needle not in content:
            return False
        return True

    async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
        if self._armed and self._hits(key, content):
            self._armed = False
            self.injected += 1
            raise _InjectedFault(f"panne durable injectée sur put({key})")
        await self._inner.put(key, content, content_type)

    async def put_json(self, key: str, data: dict) -> None:
        import json

        await self.put(key, json.dumps(data, indent=2, ensure_ascii=False))

    async def get(self, key: str) -> str | None:
        return await self._inner.get(key)

    async def get_json(self, key: str) -> dict | None:
        import json

        raw = await self.get(key)
        return None if raw is None else json.loads(raw)

    async def delete(self, key: str) -> None:
        await self._inner.delete(key)

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        return await self._inner.list_objects(prefix, max_keys)

    async def exists(self, key: str) -> bool:
        return await self._inner.exists(key)


async def test_acquire_fault_after_bump_term_before_set_token_leaves_head_pending() -> None:
    """L25 (Codex BLOCKING head 62e71dbc, never-orphan) — une panne durable APRÈS
    ``bump_term`` mais AVANT ``set_token`` (write token.json) ne doit JAMAIS
    consommer la head.

    RED sur l'ordre pré-fix (``mark_granted`` EN PREMIER) : la head serait déjà
    GRANTED au moment de la panne -> orpheline (requête consommée, aucun holder,
    plus retryable car ``head()`` skippe les non-PENDING). GREEN avec
    ``mark_granted`` EN DERNIER : la head reste PENDING (retryable), le retry
    converge vers un holder unique. Le term peut s'inflater (monotone, bénin)."""
    inner = FakeStorage()
    fault = _FaultStorage(inner)
    clock = DeterministicClock()
    store, queue, lease = _runtime(fault, clock)  # type: ignore[arg-type]
    await seed_term(store, 1)
    m = membership(1, "nodeA")
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-a", ["nodeA"])

    # Panne sur l'écriture token.json (= set_token, juste après bump_term).
    fault.arm(key_suffix="/token.json")
    with pytest.raises(_InjectedFault):
        await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")

    # term bumpé (term.json écrit avant la panne) mais AUCUN token écrit.
    assert (await store.get_term()).term == 2
    assert await store.get_token() is None
    # NEVER-ORPHAN : la head est ENCORE PENDING (mark_granted n'a pas tourné).
    head = await queue.head(m)
    assert head is not None and head.event_id == "evt-a"
    entries = await store.list_queue()
    assert all(e.status == QueueEntryStatus.PENDING.value for e in entries)

    # RETRY (panne désarmée) : converge vers un holder unique, head consommée.
    token = await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")
    assert token.state == TokenState.HELD.value and token.holder_node_id == "nodeA"
    assert token.event_id == "evt-a"
    # Le term 2 est BRÛLÉ par la 1ʳᵉ tentative (bump_term réussit avant la panne
    # set_token) ; le retry re-bumpe à 3 : inflation monotone bénigne (un term sans
    # holder est inoffensif), jamais une régression ni un second grant au même term.
    assert token.term == 3
    assert await queue.head(m) is None  # head consommée APRÈS holder établi
    await assert_at_most_one_valid_holder(_SingleNodeCluster(store))


async def test_acquire_fault_on_bump_term_leaves_nothing_durable_head_pending() -> None:
    """L25bis (Codex BLOCKING head 62e71dbc, never-orphan — le prefix le PLUS
    simple) — une panne durable sur la TOUTE PREMIÈRE écriture du grant
    (``bump_term`` -> term.json) ne laisse RIEN de durable et la head ENCORE
    PENDING.

    Symétrie avec L25 (panne sur token.json, prefix plus dur où le term est déjà
    écrit) : ici aucune des trois écritures durables n'a abouti. RED sur l'ordre
    pré-fix (``mark_granted`` EN PREMIER) : la head serait déjà GRANTED avant même
    le bump -> orpheline (requête consommée, aucun holder, plus retryable). GREEN
    avec ``mark_granted`` EN DERNIER : rien d'écrit, head PENDING, le retry accorde
    proprement un holder unique."""
    inner = FakeStorage()
    fault = _FaultStorage(inner)
    clock = DeterministicClock()
    store, queue, lease = _runtime(fault, clock)  # type: ignore[arg-type]
    await seed_term(store, 1)
    m = membership(1, "nodeA")
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-a", ["nodeA"])

    # Panne sur l'écriture term.json (= bump_term, le PREMIER effet durable).
    fault.arm(key_suffix="/term.json")
    with pytest.raises(_InjectedFault):
        await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")

    # RIEN de durable : term inchangé (1), aucun token, head ENCORE PENDING.
    assert (await store.get_term()).term == 1
    assert await store.get_token() is None
    head = await queue.head(m)
    assert head is not None and head.event_id == "evt-a"
    entries = await store.list_queue()
    assert all(e.status == QueueEntryStatus.PENDING.value for e in entries)

    # RETRY (panne désarmée) : accorde proprement (term 1 -> 2), head consommée.
    token = await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")
    assert token.state == TokenState.HELD.value and token.holder_node_id == "nodeA"
    assert token.term == 2  # aucun term brûlé (bump_term n'avait pas abouti)
    assert await queue.head(m) is None
    await assert_at_most_one_valid_holder(_SingleNodeCluster(store))


async def test_acquire_fault_after_set_token_before_mark_granted_resumes_idempotently() -> None:
    """L26 (Codex BLOCKING head 62e71dbc, recouvrabilité) — une panne durable
    APRÈS ``set_token`` mais AVANT ``mark_granted`` laisse un token HELD durable
    pour CE ``event_id`` + une head ENCORE PENDING (recouvrable, jamais orphelin).

    RED sur l'ordre pré-fix (``mark_granted`` EN PREMIER) : le token ne serait
    jamais écrit avant la panne -> aucun état de grant à reprendre. GREEN avec le
    fast-path de reprise idempotente : le retry RECONNAÎT le grant déjà établi,
    finalise ``mark_granted`` et retourne le MÊME token SANS re-bumper le term ni
    buter sur G3 (qui verrait la lease vivante tenue par soi-même)."""
    inner = FakeStorage()
    fault = _FaultStorage(inner)
    clock = DeterministicClock()
    store, queue, lease = _runtime(fault, clock)  # type: ignore[arg-type]
    await seed_term(store, 1)
    m = membership(1, "nodeA")
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-a", ["nodeA"])

    # Panne sur l'écriture de l'entrée queue passant à GRANTED (= mark_granted).
    fault.arm(key_contains="/queue/", content_needle='"granted"')
    with pytest.raises(_InjectedFault):
        await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")

    # Le grant est DÉJÀ établi durablement : token HELD(nodeA, evt-a, term 2, lease
    # vivante)...
    held = await store.get_token()
    assert held is not None and held.state == TokenState.HELD.value
    assert held.holder_node_id == "nodeA" and held.event_id == "evt-a"
    assert held.term == 2
    # ... mais la head est ENCORE PENDING (mark_granted a échoué) : recouvrable.
    head = await queue.head(m)
    assert head is not None and head.event_id == "evt-a"

    # RETRY (panne désarmée) : le fast-path de reprise reconnaît le grant établi,
    # retourne le MÊME token (term INCHANGÉ : pas de ré-bump) et finalise la head.
    resumed = await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")
    assert resumed.term == 2  # PAS de ré-bump : reprise idempotente, pas un grant neuf
    assert resumed.holder_node_id == "nodeA" and resumed.event_id == "evt-a"
    assert resumed.granted_at == held.granted_at  # exactement le même token
    assert await queue.head(m) is None  # head consommée (mark_granted finalisé)
    await assert_at_most_one_valid_holder(_SingleNodeCluster(store))


async def test_acquire_resumes_own_half_applied_grant_idempotently() -> None:
    """L27 — fast-path de reprise EN ISOLATION (sans injection) : on PRÉPARE
    l'état d'un grant à moitié appliqué (token HELD(nodeA, evt-a, term courant,
    lease vivante) + head evt-a ENCORE PENDING) puis on ré-appelle
    ``acquire(evt-a)``.

    RED sans le fast-path : G3 verrait la lease vivante (tenue par nodeA) et
    refuserait BLOCKED — la head resterait PENDING à jamais alors qu'un holder
    existe. GREEN : ``acquire`` RECONNAÎT son propre grant, finalise
    ``mark_granted`` et retourne le token existant SANS re-bumper le term."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    m = membership(1, "nodeA")
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-a", ["nodeA"])
    # État « moitié appliqué » : term 2 bumpé + token HELD écrit, mais head PENDING.
    await seed_term(store, 2)
    await store.set_token(
        held_token(
            holder="nodeA",
            term=2,
            lease_until=compute_lease_until(clock.now(), 300),
            event_id="evt-a",
        )
    )
    assert (await queue.head(m)).event_id == "evt-a"  # head ENCORE PENDING

    token = await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")

    assert token.term == 2  # PAS de ré-bump
    assert token.holder_node_id == "nodeA" and token.event_id == "evt-a"
    assert (await store.get_term()).term == 2  # term inchangé
    assert await queue.head(m) is None  # head finalisée (GRANTED)
    await assert_at_most_one_valid_holder(_SingleNodeCluster(store))


async def test_acquire_resume_rejects_other_holders_live_lease() -> None:
    """L27bis — le fast-path NE déclenche QUE pour son propre grant : si la lease
    vivante est tenue par un AUTRE nœud (nodeB), ``acquire(nodeA)`` ne reprend pas
    et retombe sur G3 (mutual-exclusion) -> BLOCKED. Garde-fou contre un
    élargissement accidentel du fast-path en faille split-brain."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    m = membership(1, "nodeA", "nodeB")
    await submit_pending(queue, event_id="evt-a", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-a", ["nodeA", "nodeB"])
    await seed_term(store, 2)
    # Lease vivante tenue par nodeB pour un AUTRE event.
    await store.set_token(
        held_token(
            holder="nodeB",
            term=2,
            lease_until=compute_lease_until(clock.now(), 300),
            event_id="evt-other",
        )
    )

    with pytest.raises(CommitNotAuthorized) as err:
        await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")
    assert err.value.reason == CommitDenyReason.BLOCKED


async def test_renew_corrupt_active_other_holder_without_lease_until_fails_closed() -> None:
    """L28 (Codex MINOR head 62e71dbc) — ``renew()`` sur un token ACTIF corrompu
    (HELD sans ``lease_until``) tenu par un AUTRE nœud doit remonter
    ``CorruptedStateError`` (fail-closed), JAMAIS être masqué en ``NOT_HOLDER``.

    RED sans le structural-first dans ``renew`` : la garde holder lèverait
    ``NOT_HOLDER`` (nodeB != nodeA) AVANT ``is_lease_expired`` et la corruption
    critique resterait silencieuse — incohérent avec ``evaluate_commit_auth`` /
    ``release`` / ``reconcile_stale_holder``."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    await seed_term(store, 2)
    await store.set_token(
        held_token(holder="nodeB", term=2, lease_until=None, event_id="evt-x")
    )

    with pytest.raises(CorruptedStateError):
        await lease.renew(holder_node_id="nodeA")  # un AUTRE nœud demande le renew


async def test_renew_corrupt_active_other_holder_malformed_lease_until_fails_closed() -> None:
    """L28bis — même garde structural-first pour un ``lease_until`` MALFORMÉ
    (non parsable) sur un actif tenu par un autre nœud : ``CorruptedStateError``,
    jamais ``NOT_HOLDER``."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    await seed_term(store, 2)
    await store.set_token(
        held_token(
            holder="nodeB", term=2, lease_until="pas-un-timestamp", event_id="evt-x"
        )
    )

    with pytest.raises(CorruptedStateError):
        await lease.renew(holder_node_id="nodeA")


async def test_acquire_resume_fails_closed_on_divergent_same_event_head() -> None:
    """L27ter (Codex BLOCKING heads f1345a6 + f371e05) — sur une anomalie de queue
    DIVERGENTE same-event_id (deux entrées ``event_id=evtX`` de requesters
    DISTINCTS), le fast-path de reprise FAIL-CLOSED (BLOCKED) : il NE consomme PAS
    l'entrée d'un autre requester (never-orphan) ET ne retourne PAS un acquire
    « réussi » silencieux par-dessus une queue divergente.

    RED #1 (head f1345a6, sans le check requester) : la head canonique est l'entrée
    de nodeB (seq inférieur) ; le resume de nodeA la marquerait GRANTED -> on
    consommerait la requête de nodeB sans que nodeB ait jamais tenu le token.
    RED #2 (head f371e05, check requester mais return inconditionnel) : on ne
    consomme plus nodeB, mais ``acquire`` retournait quand même le token de nodeA —
    le holder pouvait alors enchaîner ``assert_commit_allowed()`` (qui ne re-vérifie
    pas la head) et committer par-dessus la queue divergente.
    GREEN : ``acquire`` lève ``CommitNotAuthorized(BLOCKED)``, l'entrée de nodeB ET
    celle de nodeA restent PENDING, et l'anomalie reste visible pour
    ``queue_anomalies()``."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    m = membership(1, "nodeA", "nodeB")
    await seed_term(store, 2)
    # Anomalie divergente : MÊME event_id "evtX", deux requesters, seq distincts.
    # La head canonique = seq 0 (nodeB), PAS l'entrée de nodeA (seq 1).
    await store.enqueue(
        QueueEntry(
            event_id="evtX", requester_node_id="nodeB", sequence=0,
            term=2, membership_epoch=1,
        )
    )
    await store.enqueue(
        QueueEntry(
            event_id="evtX", requester_node_id="nodeA", sequence=1,
            term=2, membership_epoch=1,
        )
    )
    # Grant à moitié appliqué de nodeA pour evtX (token HELD écrit, head pas consommée).
    await store.set_token(
        held_token(
            holder="nodeA", term=2,
            lease_until=compute_lease_until(clock.now(), 300),
            event_id="evtX",
        )
    )
    # Sanity : la head canonique est bien l'entrée de nodeB (seq 0).
    head = await queue.head(m)
    assert head is not None and head.requester_node_id == "nodeB" and head.sequence == 0

    # FAIL-CLOSED : acquire lève BLOCKED, jamais un grant silencieux.
    with pytest.raises(CommitNotAuthorized) as err:
        await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evtX")
    assert err.value.reason == CommitDenyReason.BLOCKED

    # Le fail-closed n'a RIEN consommé : nodeB ET nodeA restent PENDING (never-orphan).
    entries = await store.list_queue()
    nodeB_entries = [e for e in entries if e.requester_node_id == "nodeB"]
    assert len(nodeB_entries) == 1
    assert nodeB_entries[0].status == QueueEntryStatus.PENDING.value
    assert all(e.status == QueueEntryStatus.PENDING.value for e in entries)
    # L'anomalie divergente same-event_id reste détectable (non silencieusement avalée).
    assert len(await queue.queue_anomalies()) >= 1


async def test_acquire_resume_fails_closed_when_own_entry_pending_but_not_head() -> None:
    """L27quater (Codex BLOCKING head fb5e486) — grant à moitié appliqué de nodeA
    pour evt-a, mais une entrée d'ordre INFÉRIEUR (evt-b) est la head canonique
    pendant que evt-a reste PENDING (arrivée out-of-order côté queue répliquée).
    « head = un autre event » N'EST PAS une preuve que notre entrée a été consommée.

    RED sans le scan own_pending (head.event_id != event -> return held) : acquire
    retournerait un succès pour evt-a alors qu'evt-b est la head courante, laissant
    le holder committer PAR-DESSUS via assert_commit_allowed (qui ne re-vérifie pas
    la position). GREEN : fail-closed BLOCKED tant que notre entrée evt-a est encore
    PENDING sans être la head."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    m = membership(1, "nodeA", "nodeB")
    await seed_term(store, 2)
    # evt-b (nodeB) seq 0 = head canonique ; evt-a (nodeA) seq 1 ENCORE PENDING.
    await store.enqueue(
        QueueEntry(event_id="evt-b", requester_node_id="nodeB", sequence=0,
                   term=2, membership_epoch=1)
    )
    await store.enqueue(
        QueueEntry(event_id="evt-a", requester_node_id="nodeA", sequence=1,
                   term=2, membership_epoch=1)
    )
    # Grant à moitié appliqué de nodeA pour evt-a (token écrit, entrée non consommée).
    await store.set_token(
        held_token(holder="nodeA", term=2,
                   lease_until=compute_lease_until(clock.now(), 300),
                   event_id="evt-a")
    )
    # Sanity : la head canonique est evt-b, et notre evt-a est encore PENDING.
    head = await queue.head(m)
    assert head is not None and head.event_id == "evt-b"

    with pytest.raises(CommitNotAuthorized) as err:
        await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")
    assert err.value.reason == CommitDenyReason.BLOCKED
    # Fail-closed : rien consommé, evt-a ET evt-b restent PENDING.
    entries = await store.list_queue()
    assert all(e.status == QueueEntryStatus.PENDING.value for e in entries)


async def test_acquire_resume_idempotent_returns_when_own_entry_already_consumed() -> None:
    """L27quinquies — sous-cas (2) : le grant a été PLEINEMENT appliqué (mark_granted
    réussi : l'entrée de nodeA pour evt-a est GRANTED, plus aucune PENDING). Un retry
    d'acquire(evt-a) est un pur retry idempotent : aucune entrée PENDING pour
    (evt-a, nodeA) -> on retourne le token existant SANS erreur ni ré-bump (preuve
    positive de consommation). Garde-fou que le fail-closed ajouté ne casse pas le
    retry idempotent légitime."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    m = membership(1, "nodeA")
    await seed_term(store, 2)
    # Notre entrée evt-a déjà CONSOMMÉE (GRANTED) : plus aucune PENDING.
    await store.enqueue(
        QueueEntry(event_id="evt-a", requester_node_id="nodeA", sequence=0,
                   term=2, membership_epoch=1, status=QueueEntryStatus.GRANTED)
    )
    await store.set_token(
        held_token(holder="nodeA", term=2,
                   lease_until=compute_lease_until(clock.now(), 300),
                   event_id="evt-a")
    )
    assert await queue.head(m) is None  # plus de PENDING -> pas de head

    token = await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-a")

    assert token.holder_node_id == "nodeA" and token.event_id == "evt-a"
    assert token.term == 2  # pas de ré-bump : retry idempotent
    assert (await store.get_term()).term == 2


async def test_acquire_resume_blocks_own_canonical_plus_divergent_duplicate() -> None:
    """L27sexies (Codex BLOCKING head 5225303) — notre entrée EST la head canonique
    (nodeA/evtX seq0) MAIS un doublon divergent same-event reste PENDING
    (nodeB/evtX seq1). Finaliser notre entrée la consommerait et MASQUERAIT le
    doublon (detect_event_id_duplicates ne groupe que les PENDING : après grant de
    seq0, seq1 seul -> plus détecté). RED sans le check d'ensemble PENDING
    same-event : acquire consommerait seq0, retournerait un succès, et le doublon
    deviendrait invisible. GREEN : fail-closed BLOCKED, rien consommé, doublon
    préservé."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    m = membership(1, "nodeA", "nodeB")
    await seed_term(store, 2)
    # Notre entrée canonique (seq 0) + un doublon divergent (nodeB, seq 1).
    await store.enqueue(QueueEntry(event_id="evtX", requester_node_id="nodeA", sequence=0, term=2, membership_epoch=1))
    await store.enqueue(QueueEntry(event_id="evtX", requester_node_id="nodeB", sequence=1, term=2, membership_epoch=1))
    await store.set_token(held_token(holder="nodeA", term=2, lease_until=compute_lease_until(clock.now(), 300), event_id="evtX"))
    # Sanity : notre entrée EST la head canonique.
    head = await queue.head(m)
    assert head is not None and head.requester_node_id == "nodeA" and head.sequence == 0

    with pytest.raises(CommitNotAuthorized) as err:
        await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evtX")
    assert err.value.reason == CommitDenyReason.BLOCKED
    # Rien consommé : les DEUX entrées restent PENDING, doublon toujours détectable.
    entries = await store.list_queue()
    assert all(e.status == QueueEntryStatus.PENDING.value for e in entries)
    assert len(await queue.queue_anomalies()) >= 1


async def test_acquire_resume_blocks_own_duplicate_seqs() -> None:
    """L27septies (Codex BLOCKING head 5225303, « same class ») — deux entrées
    PENDING du MÊME requester pour le même event_id à des seq distinctes
    (nodeA/evtX seq0 + nodeA/evtX seq1). L'ensemble PENDING same-event n'est pas
    réductible à UNE entrée canonique -> fail-closed BLOCKED (jamais consommer l'une
    en masquant l'autre)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    m = membership(1, "nodeA")
    await seed_term(store, 2)
    await store.enqueue(QueueEntry(event_id="evtX", requester_node_id="nodeA", sequence=0, term=2, membership_epoch=1))
    await store.enqueue(QueueEntry(event_id="evtX", requester_node_id="nodeA", sequence=1, term=2, membership_epoch=1))
    await store.set_token(held_token(holder="nodeA", term=2, lease_until=compute_lease_until(clock.now(), 300), event_id="evtX"))

    with pytest.raises(CommitNotAuthorized) as err:
        await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evtX")
    assert err.value.reason == CommitDenyReason.BLOCKED
    entries = await store.list_queue()
    assert all(e.status == QueueEntryStatus.PENDING.value for e in entries)


async def test_renew_stale_term_holder_is_superseded() -> None:
    """L29 (Codex BLOCKING head 5225303) — un holder SUPERSEDED (token.term <
    term.json après un bump) ne peut pas prolonger sa lease obsolète via renew :
    STALE_TERM. RED sans le check term dans renew : la lease stale serait renouvelée
    et G3 la verrait active indéfiniment -> blocage de convergence (HIVEMIND.md
    §6.2 : ancien holder revenu après bump = fencé)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock)
    # term vivant = 3, mais le token de nodeA est HELD au term 2 (superseded).
    await seed_term(store, 3)
    await store.set_token(held_token(holder="nodeA", term=2, lease_until=compute_lease_until(clock.now(), 300), event_id="evt-a"))

    with pytest.raises(CommitNotAuthorized) as err:
        await lease.renew(holder_node_id="nodeA")
    assert err.value.reason == CommitDenyReason.STALE_TERM
    # Aucun write : la lease stale n'a pas été prolongée (term inchangé).
    cur = await store.get_token()
    assert cur is not None and cur.term == 2


# =============================================================================
# L30 — cohérence TERM fail-closed des tokens actifs sur renew/release/reconcile
# (Codex BLOCKING head 20e2e5b : term.json absent OU token au futur = corruption).
# =============================================================================


def _active_token_term(holder: str, term: int) -> TokenLeaseState:
    return held_token(
        holder=holder, term=term,
        lease_until="2999-01-01T00:00:00+00:00",  # lease vivante (loin dans le futur)
        event_id="evt-a",
    )


async def test_renew_future_term_active_token_fails_closed() -> None:
    """L30a — token actif AU FUTUR (token.term=5 > term.json=3) : impossible
    (acquire bumpe le term avant d'écrire le token) -> CorruptedStateError, jamais
    renouvelé. RED sans assert_active_token_term_consistent : 5 != 3 tomberait en
    STALE_TERM (refus normal) au lieu de remonter la corruption."""
    storage = FakeStorage()
    store, queue, lease = _runtime(storage, DeterministicClock())
    await seed_term(store, 3)
    await store.set_token(_active_token_term("nodeA", 5))
    with pytest.raises(CorruptedStateError):
        await lease.renew(holder_node_id="nodeA")


async def test_renew_missing_term_active_token_fails_closed() -> None:
    """L30b — token actif SANS term.json vivant : état incomplet -> CorruptedStateError,
    jamais défaut à 0 (un term-0 actif sans term.json ne doit pas renouveler alors
    qu'evaluate_commit_authorization le BLOQUE)."""
    storage = FakeStorage()
    store, queue, lease = _runtime(storage, DeterministicClock())
    # PAS de seed_term -> term.json absent.
    await store.set_token(_active_token_term("nodeA", 2))
    with pytest.raises(CorruptedStateError):
        await lease.renew(holder_node_id="nodeA")


async def test_release_future_term_active_token_fails_closed() -> None:
    """L30c — release ne peut PAS effacer en FREE un token actif au futur
    (token.term > term.json) : CorruptedStateError avant tout write."""
    storage = FakeStorage()
    store, queue, lease = _runtime(storage, DeterministicClock())
    await seed_term(store, 3)
    await store.set_token(_active_token_term("nodeA", 5))
    with pytest.raises(CorruptedStateError):
        await lease.release(holder_node_id="nodeA")
    cur = await store.get_token()
    assert cur is not None and cur.state == TokenState.HELD.value  # pas effacé


async def test_release_missing_term_active_token_fails_closed() -> None:
    """L30d — release fail-closed sur token actif sans term.json vivant."""
    storage = FakeStorage()
    store, queue, lease = _runtime(storage, DeterministicClock())
    await store.set_token(_active_token_term("nodeA", 2))
    with pytest.raises(CorruptedStateError):
        await lease.release(holder_node_id="nodeA")


async def test_reconcile_future_term_active_token_fails_closed() -> None:
    """L30e — reconcile ne PRÉSERVE pas un token actif au futur (token.term=5 >
    term.json=3) comme « holder légitime » : CorruptedStateError. RED sans la garde :
    token.term >= current_term gardait le token au futur inchangé."""
    storage = FakeStorage()
    store, queue, lease = _runtime(storage, DeterministicClock())
    await seed_term(store, 3)
    await store.set_token(_active_token_term("nodeA", 5))
    with pytest.raises(CorruptedStateError):
        await lease.reconcile_stale_holder()


async def test_reconcile_missing_term_active_token_fails_closed() -> None:
    """L30f — reconcile fail-closed sur token actif sans term.json vivant."""
    storage = FakeStorage()
    store, queue, lease = _runtime(storage, DeterministicClock())
    await store.set_token(_active_token_term("nodeA", 2))
    with pytest.raises(CorruptedStateError):
        await lease.reconcile_stale_holder()


# =============================================================================
# L31 — cohérence TERM appliquée AUSSI au gate de commit et au resume d'acquire
# (Codex BLOCKING head a8dcf65 : la garde doit être PARTOUT où un actif est
# accepté/autorisé/retourné en succès, pas seulement renew/release/reconcile).
# =============================================================================


def test_evaluate_commit_future_term_active_token_is_corruption() -> None:
    """L31a — au gate de commit pur, un token ACTIF au futur (token.term=5 >
    term.json=3) remonte CorruptedStateError, JAMAIS downgradé en STALE_TERM par la
    chaîne d'égalité. RED sans assert_active_token_term_consistent dans
    evaluate_commit_authorization : 5 != 3 -> STALE_TERM ordinaire."""
    token = held_token(holder="nodeA", term=5, lease_until="2999-01-01T00:00:00+00:00", event_id="e")
    term = TermState(term=3, updated_by_node_id="x")
    pointer = BankVersionPointer(bank_version=0, commit_id="c0")
    intent = good_intent(holder="nodeA", term=5, previous_bank_version=0)
    now = DeterministicClock().now()
    with pytest.raises(CorruptedStateError):
        evaluate_commit_authorization(token=token, term=term, pointer=pointer, intent=intent, now=now)


async def test_acquire_resume_missing_term_active_token_fails_closed() -> None:
    """L31b (Codex repro head a8dcf65) — resume d'un HELD vivant SANS term.json
    (term absent) : fail-closed CorruptedStateError, jamais un succès silencieux
    défaltant term.json à 0 et marquant la head GRANTED. RED sans la garde :
    current_term=0, held.term==0 -> finalise -> 'returned held 0 None'."""
    storage = FakeStorage()
    store, queue, lease = _runtime(storage, DeterministicClock())
    m = membership(1, "nodeA")
    # PAS de seed_term -> term.json absent.
    await store.enqueue(QueueEntry(event_id="evt", requester_node_id="nodeA", sequence=0, term=0, membership_epoch=1))
    await ack_all(queue, "evt", ["nodeA"])
    await store.set_token(held_token(holder="nodeA", term=0, lease_until="2999-01-01T00:00:00+00:00", event_id="evt"))

    with pytest.raises(CorruptedStateError):
        await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt")
    # Fail-closed AVANT mark_granted : l'entrée reste PENDING (jamais consommée).
    entries = await store.list_queue()
    assert all(e.status == QueueEntryStatus.PENDING.value for e in entries)


async def test_acquire_resume_future_term_active_token_fails_closed() -> None:
    """L31c — resume d'un HELD vivant AU FUTUR (token.term=5 > term.json=3) :
    fail-closed CorruptedStateError. RED sans la garde : held.term != current_term
    -> fall-through vers G3 -> BLOCKED (refus ordinaire), corruption masquée."""
    storage = FakeStorage()
    store, queue, lease = _runtime(storage, DeterministicClock())
    m = membership(1, "nodeA")
    await seed_term(store, 3)
    await store.enqueue(QueueEntry(event_id="evt", requester_node_id="nodeA", sequence=0, term=5, membership_epoch=1))
    await ack_all(queue, "evt", ["nodeA"])
    await store.set_token(held_token(holder="nodeA", term=5, lease_until="2999-01-01T00:00:00+00:00", event_id="evt"))

    with pytest.raises(CorruptedStateError):
        await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt")


# =============================================================================
# L32 — G3 fail-closed sur token actif corrompu EXPIRÉ (Codex HIGH head a0c51c2 :
# lease_is_active ne voit QUE l'expiration -> un actif corrompu expiré serait
# ÉCRASÉ par acquire au lieu de remonter en corruption).
# =============================================================================


async def test_acquire_g3_expired_future_term_token_fails_closed() -> None:
    """L32a — G3 voit un token corrompu AU FUTUR (token.term=4 > term.json=3) mais
    EXPIRÉ. RED sans la garde term à G3 : lease_is_active=False (expiré) -> acquire
    procède, bump le term, écrit un NOUVEAU holder et marque GRANTED (fail-OPEN :
    l'état corrompu est silencieusement écrasé). GREEN : CorruptedStateError, aucun
    écrasement (token/term/queue inchangés)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock, ttl=300)
    await seed_term(store, 3)
    # Token corrompu (term 4 > term.json 3) tenu par nodeA, lease vivante puis expirée.
    await store.set_token(
        held_token(holder="nodeA", term=4,
                   lease_until=compute_lease_until(clock.now(), 300), event_id="old")
    )
    clock.tick(seconds=301)  # la lease du token corrompu expire
    m = membership(1, "nodeA", "nodeB")
    await submit_pending(queue, event_id="evt-b", requester="nodeB", sequence=0)
    await ack_all(queue, "evt-b", ["nodeA", "nodeB"])

    with pytest.raises(CorruptedStateError):
        await lease.acquire(membership=m, holder_node_id="nodeB", event_id="evt-b")
    # Aucun écrasement : token toujours nodeA/term4, term.json toujours 3, evt-b PENDING.
    cur = await store.get_token()
    assert cur is not None and cur.holder_node_id == "nodeA" and cur.term == 4
    assert (await store.get_term()).term == 3
    entries = await store.list_queue()
    assert all(e.status == QueueEntryStatus.PENDING.value for e in entries)


async def test_acquire_g3_expired_missing_term_token_fails_closed() -> None:
    """L32b — G3 voit un token actif EXPIRÉ sans term.json vivant (état incomplet).
    RED sans la garde : acquire procéderait et écraserait. GREEN :
    CorruptedStateError."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock, ttl=300)
    # PAS de seed_term -> term.json absent ; token actif expiré.
    await store.set_token(
        held_token(holder="nodeA", term=2,
                   lease_until=compute_lease_until(clock.now(), 300), event_id="old")
    )
    clock.tick(seconds=301)
    m = membership(1, "nodeA", "nodeB")
    await submit_pending(queue, event_id="evt-b", requester="nodeB", sequence=0)
    await ack_all(queue, "evt-b", ["nodeA", "nodeB"])

    with pytest.raises(CorruptedStateError):
        await lease.acquire(membership=m, holder_node_id="nodeB", event_id="evt-b")
    # Aucun écrasement.
    cur = await store.get_token()
    assert cur is not None and cur.holder_node_id == "nodeA" and cur.term == 2


# =============================================================================
# L33 — corruption-first en TÊTE d'acquire : un token actif corrompu remonte
# CorruptedStateError AVANT tout BLOCKED ordinaire (G1 all-ACK / G2 head), même
# expiré / ACK manquant (Codex MEDIUM head 2a9fc3d).
# =============================================================================


async def test_acquire_corrupt_active_future_term_fails_closed_before_g1() -> None:
    """L33a — token actif AU FUTUR (token.term=5 > term.json=3), EXPIRÉ : remonte
    CorruptedStateError EN TÊTE d'acquire, AVANT G1 all-ACK. RED sans la garde
    corruption-first en tête : resume sauté (expiré) -> G1 (ACK manquant) -> BLOCKED,
    la corruption critique masquée en simple attente d'ACK."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock, ttl=300)
    await seed_term(store, 3)
    await store.set_token(
        held_token(holder="nodeA", term=5,
                   lease_until=compute_lease_until(clock.now(), 300), event_id="evt-b")
    )
    clock.tick(seconds=301)  # le token corrompu expire
    m = membership(1, "nodeA", "nodeB")
    await submit_pending(queue, event_id="evt-b", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-b", ["nodeA"])  # nodeB manque -> G1 bloquerait

    with pytest.raises(CorruptedStateError):
        await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-b")


async def test_acquire_corrupt_active_missing_term_fails_closed_before_g1() -> None:
    """L33b — token actif SANS term.json (term absent), EXPIRÉ, ACK manquant : remonte
    CorruptedStateError en tête, jamais un BLOCKED de routine."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease = _runtime(storage, clock, ttl=300)
    # PAS de seed_term -> term.json absent.
    await store.set_token(
        held_token(holder="nodeA", term=2,
                   lease_until=compute_lease_until(clock.now(), 300), event_id="evt-b")
    )
    clock.tick(seconds=301)
    m = membership(1, "nodeA", "nodeB")
    await submit_pending(queue, event_id="evt-b", requester="nodeA", sequence=0)
    await ack_all(queue, "evt-b", ["nodeA"])

    with pytest.raises(CorruptedStateError):
        await lease.acquire(membership=m, holder_node_id="nodeA", event_id="evt-b")


async def test_renew_and_acquire_serialized_no_split_brain() -> None:
    """L34 (Codex HIGH head fb6f112) — renew() et acquire() DOIVENT être sérialisés
    sous le MÊME verrou de mutation par-space. Sinon : A renouvelle la lease de nodeA
    (parqué avant set_token) ; l'horloge dépasse l'ANCIENNE expiration mais pas la
    NOUVELLE ; B (acquire nodeB) lit le snapshot PÉRIMÉ d'avant-renew, passe G3
    (lease « expirée ») et s'auto-accorde PAR-DESSUS la lease renouvelée vivante de
    nodeA — split-brain.

    RED sans le verrou sur renew : B n'attend pas, lit le token périmé et s'accorde
    (acquire réussit, term bumpé). GREEN avec sérialisation : B attend que A relâche,
    voit la lease renouvelée VIVANTE et est refusé BLOCKED ; un seul holder (nodeA)."""
    gated = _GatedStorage(FakeStorage(), armed=False)
    clock = DeterministicClock()
    store = make_store(gated)  # type: ignore[arg-type]
    queue = QueueRuntime(store, "alpha")
    lease = LeaseRuntime(store, "alpha", queue, clock=clock.now, ttl_seconds=300)

    await seed_term(store, 2)
    m = membership(2, "nodeA", "nodeB")
    # nodeA tient une lease vivante (term 2), expirant à T0+300.
    await store.set_token(
        held_token(holder="nodeA", term=2,
                   lease_until=compute_lease_until(clock.now(), 300), event_id="evt-a")
    )
    # une entrée head-éligible all-ACKée pour que l'acquire de nodeB passe G1/G2.
    await submit_pending(queue, event_id="evt-b", requester="nodeB", sequence=1, term=2, epoch=2)
    await ack_all(queue, "evt-b", ["nodeA", "nodeB"], epoch=2)

    clock.tick(seconds=250)  # T0+250 : lease originelle ENCORE vivante (50 s restantes)
    gated.arm()  # parque la PROCHAINE écriture token.json (= set_token de renew)

    # A = renew(nodeA) : prend le verrou, lit now=T0+250 -> lease renouvelée T0+550,
    # puis se parque au set_token (SOUS le verrou).
    task_a = asyncio.ensure_future(lease.renew(holder_node_id="nodeA"))
    await gated.parked.wait()  # A parqué au write de la lease renouvelée

    clock.tick(seconds=60)  # T0+310 : ANCIENNE lease (T0+300) expirée, NOUVELLE (T0+550) vivante

    # B = acquire(nodeB) : SANS verrou sur renew il lirait le snapshot périmé et
    # s'accorderait ; AVEC verrou il attend A.
    task_b = asyncio.ensure_future(
        lease.acquire(membership=m, holder_node_id="nodeB", event_id="evt-b")
    )
    for _ in range(50):
        await asyncio.sleep(0)
    gated.release()  # A écrit enfin la lease renouvelée (vivante) et relâche le verrou
    results = await asyncio.gather(task_a, task_b, return_exceptions=True)

    a_result, b_result = results
    # A renew réussit (lease renouvelée vivante, nodeA, term 2 inchangé).
    assert isinstance(a_result, TokenLeaseState), f"renew doit réussir, obtenu {a_result!r}"
    assert a_result.holder_node_id == "nodeA" and a_result.term == 2
    # B acquire est REFUSÉ BLOCKED par la lease renouvelée vivante (jamais un 2ᵉ grant).
    assert isinstance(b_result, CommitNotAuthorized), f"acquire doit être refusé, obtenu {b_result!r}"
    assert b_result.reason == CommitDenyReason.BLOCKED
    # Oracle : au plus un holder valide ; term jamais bumpé (pas de grant à B).
    await assert_at_most_one_valid_holder(_SingleNodeCluster(store))
    assert (await store.get_term()).term == 2


class _GatedPointerRead:
    """Décorateur de ``FakeStorage`` qui PARQUE le premier ``get`` de
    ``bank_version.json`` — le 3ᵉ read d'``assert_commit_allowed`` (après token +
    term) — pour reproduire un ``release()`` qui s'intercale ENTRE la lecture du
    token et celle du pointeur. Démarre désarmé (``armed=False``) pour que le seeding
    (qui lit bank_version.json via set_bank_version_pointer) ne soit pas parqué ; on
    arme juste avant l'``assert_commit_allowed`` sous test."""

    def __init__(self, inner: FakeStorage, *, armed: bool = False) -> None:
        self._inner = inner
        self.objects = inner.objects
        self._gate = asyncio.Event()
        self.parked = asyncio.Event()
        self._armed = armed

    def arm(self) -> None:
        self._armed = True

    def release(self) -> None:
        self._gate.set()

    async def get(self, key: str) -> str | None:
        await asyncio.sleep(0)
        if self._armed and key.endswith("/bank_version.json"):
            self._armed = False
            self.parked.set()
            await self._gate.wait()  # parqué entre le read du token et celui du pointeur
        return await self._inner.get(key)

    async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
        await self._inner.put(key, content, content_type)

    async def put_json(self, key: str, data: dict) -> None:
        import json

        await self.put(key, json.dumps(data, indent=2, ensure_ascii=False))

    async def get_json(self, key: str) -> dict | None:
        import json

        raw = await self.get(key)
        return None if raw is None else json.loads(raw)

    async def delete(self, key: str) -> None:
        await self._inner.delete(key)

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        return await self._inner.list_objects(prefix, max_keys)

    async def exists(self, key: str) -> bool:
        return await self._inner.exists(key)


async def test_assert_commit_allowed_linearizable_with_concurrent_release() -> None:
    """L35 (Codex BLOCKING head a26cd28) — ``assert_commit_allowed()`` lit
    token/term/pointer EN SÉQUENCE ; un ``release()`` concurrent qui libère le token
    (FREE, même term/fencing) ne doit PAS pouvoir s'intercaler ENTRE la lecture du
    token (HELD) et celle du pointeur — sinon le prédicat autorise sur un snapshot HELD
    déjà invalidé (token devenu FREE), permettant un BANK_COMMIT APRÈS release. Le
    snapshot est lu SOUS le verrou de mutation par-space.

    RED sans le verrou sur assert_commit_allowed : release (lui-même sous verrou)
    libère le token pendant qu'assert est parqué au read du pointeur -> le token est
    déjà FREE alors qu'assert va autoriser sur le HELD périmé. GREEN : assert tient le
    verrou pendant la lecture de son snapshot ; release est sérialisé (bloqué) et le
    token reste HELD tout du long."""
    inner = FakeStorage()
    gated = _GatedPointerRead(inner, armed=False)
    clock = DeterministicClock()
    store = make_store(gated)  # type: ignore[arg-type]
    queue = QueueRuntime(store, "alpha")
    lease = LeaseRuntime(store, "alpha", queue, clock=clock.now, ttl_seconds=300)

    await seed_term(store, 2)
    await seed_pointer(store, 0)
    await store.set_token(
        held_token(holder="nodeA", term=2,
                   lease_until=compute_lease_until(clock.now(), 300), event_id="evt-a")
    )
    gated.arm()  # parque le PROCHAIN get bank_version.json (= 3ᵉ read d'assert)

    intent = good_intent(holder="nodeA", term=2, bank_version=1, previous_bank_version=0)
    task_assert = asyncio.ensure_future(lease.assert_commit_allowed(intent))
    await gated.parked.wait()  # assert a lu token=HELD + term, parqué au read du pointeur

    task_release = asyncio.ensure_future(lease.release(holder_node_id="nodeA"))
    for _ in range(50):
        await asyncio.sleep(0)
    # Linéarisabilité : release NE DOIT PAS avoir libéré le token pendant qu'assert lit
    # son snapshot. RED sans verrou : token déjà FREE ici.
    tok = await store.get_token()
    assert tok is not None and tok.state == TokenState.HELD.value, (
        "release a libéré le token PENDANT la lecture d'assert_commit_allowed "
        f"(read torn, autorisation sur snapshot périmé): state={tok.state if tok else None}"
    )
    gated.release()  # débloque le read du pointeur d'assert
    await asyncio.gather(task_assert, task_release, return_exceptions=True)


async def test_assert_commit_allowed_evaluates_expiry_at_snapshot_not_pre_wait() -> None:
    """L36 (Codex HIGH head 4dc7855) — assert_commit_allowed() doit évaluer l'expiry de
    la lease au POINT DE LINÉARISATION du snapshot (``now`` lu DANS le verrou, APRÈS les
    reads), pas à un ``now`` pré-attente périmé. Si l'appelant franchit ``lease_until``
    pendant qu'il attend le verrou / des reads lents, l'autorisation doit FENCER.

    RED sans ``now`` sous verrou : ``now`` capturé AVANT l'attente -> evaluate reçoit un
    ``now`` < ``lease_until`` -> autorise une lease pourtant EXPIRÉE. GREEN : ``now`` lu
    après les reads sous verrou -> ``now`` > ``lease_until`` -> FENCED."""
    inner = FakeStorage()
    gated = _GatedPointerRead(inner, armed=False)
    clock = DeterministicClock()
    store = make_store(gated)  # type: ignore[arg-type]
    queue = QueueRuntime(store, "alpha")
    lease = LeaseRuntime(store, "alpha", queue, clock=clock.now, ttl_seconds=300)

    await seed_term(store, 2)
    await seed_pointer(store, 0)
    # lease vivante jusqu'à T0+300.
    await store.set_token(
        held_token(holder="nodeA", term=2,
                   lease_until=compute_lease_until(clock.now(), 300), event_id="evt-a")
    )
    gated.arm()  # parque le read bank_version.json (AVANT le read de now sous verrou)

    intent = good_intent(holder="nodeA", term=2, bank_version=1, previous_bank_version=0)
    task_assert = asyncio.ensure_future(lease.assert_commit_allowed(intent))
    await gated.parked.wait()  # parqué au read du pointeur, AVANT now = clock()
    clock.tick(seconds=301)  # la lease EXPIRE pendant l'attente
    gated.release()  # débloque -> now = clock() lu APRÈS (T0+301, > lease_until)

    exc: CommitNotAuthorized | None = None
    try:
        await task_assert
    except CommitNotAuthorized as e:
        exc = e
    assert exc is not None and exc.reason == CommitDenyReason.FENCED, (
        f"assert doit FENCER (lease expirée pendant l'attente du verrou/reads), obtenu {exc!r}"
    )
