# -*- coding: utf-8 -*-
"""
Invariants protocole V1 — assertions centralisées cross-nœud (issue #11).

Ces helpers SCANNENT TOUS les stores du cluster (pas un seul) et lèvent une
``AssertionError`` nommant explicitement le nœud / term / bank_version fautif.
C'est ce qui fait de « pas deux holders valides pour un même (space, term) »
une vraie assertion distribuée et non une tautologie mono-nœud.

Distinction importante :

- un ``RuntimeError`` levé par un garde-fou du store (monotonicité refusée,
  conflit de commit) est un comportement ATTENDU et correct — les tests
  l'attrapent via ``pytest.raises`` ;
- une ``AssertionError`` levée ici signale une VIOLATION d'invariant (double
  effet observable), c'est-à-dire un vrai bug du protocole.

``assert_invariants(cluster)`` agrège tous les checks et doit être appelé après
chaque pas de scénario et après chaque pas du property test.
"""

from __future__ import annotations

from live_mem.core.hivemind import (
    BankCommit,
    TokenLeaseState,
    TokenState,
)

from .cluster import ClusterHarness


# =============================================================================
# Holder unique par (space, term)
# =============================================================================


async def assert_at_most_one_valid_holder(cluster: ClusterHarness) -> None:
    """
    À travers TOUS les stores, l'état des holders doit être cohérent avec le
    fencing par term (HIVEMIND.md §6.2/§6.3) :

    1. au plus UN holder valide (token HELD/RELEASING) au term MAXIMUM observé
       parmi les holders actifs (jamais deux holders au même term max) ;
    2. AUCUN holder ne reste HELD/RELEASING à un term STRICTEMENT inférieur à ce
       maximum — un holder superseded/expiré qui n'a pas été réconcilié hors de
       l'état actif est un split-holder silencieux (false negative que cette
       assertion attrape). Un tel holder doit avoir été réconcilié (FREE/blocked)
       quand il a appris le term supérieur.

    Un holder « valide » a ``state in {HELD, RELEASING}`` ; le modèle Pydantic
    garantit déjà ``fencing_token == term`` à la construction. Ici on contrôle
    l'unicité cross-nœud AU TERM MAX et l'absence de holder actif stale.
    """
    # (nid, holder, term) pour chaque token actif observé.
    active_holders: list[tuple[str, str, int]] = []
    for nid in cluster.node_ids():
        token: TokenLeaseState | None = await cluster.nodes[nid].store.get_token()
        if token is None:
            continue
        if token.state in {TokenState.HELD.value, TokenState.RELEASING.value}:
            holder = token.holder_node_id
            if holder is None:
                raise AssertionError(
                    f"Invariant holder: nœud {nid!r} a token state={token.state} "
                    f"term={token.term} SANS holder_node_id"
                )
            active_holders.append((nid, holder, token.term))

    if not active_holders:
        return

    max_term = max(term for _, _, term in active_holders)

    # (2) Aucun holder actif sous le term max (holder stale non réconcilié).
    stale = [
        (nid, holder, term)
        for nid, holder, term in active_holders
        if term < max_term
    ]
    if stale:
        nid, holder, term = stale[0]
        raise AssertionError(
            f"Invariant STALE-HOLDER: nœud {nid!r} tient encore un token valide "
            f"(holder={holder!r}, term={term}) STRICTEMENT sous le term max "
            f"observé {max_term} — holder superseded/expiré non réconcilié "
            f"(doit devenir FREE/blocked, cf. §6.2/§6.3)"
        )

    # (1) Unicité du holder au term max (le même-term double-holder échoue ici).
    holders_at_max = {holder for _, holder, term in active_holders if term == max_term}
    if len(holders_at_max) > 1:
        raise AssertionError(
            f"Invariant SPLIT-BRAIN: term={max_term} a plusieurs holders "
            f"valides {sorted(holders_at_max)} (un seul autorisé par (space, term))"
        )


# =============================================================================
# Monotonie term / epoch / bank_version
# =============================================================================


async def assert_term_monotone(cluster: ClusterHarness) -> None:
    """
    Le term d'un token ne peut jamais dépasser le term courant du même store
    (un grant ne peut pas exister à un term supérieur au term persisté).
    """
    for nid in cluster.node_ids():
        store = cluster.nodes[nid].store
        term_state = await store.get_term()
        token = await store.get_token()
        if term_state is None or token is None:
            continue
        if token.term > term_state.term:
            raise AssertionError(
                f"Invariant term: nœud {nid!r} a token.term={token.term} > "
                f"term courant={term_state.term} (grant orphelin d'un term futur)"
            )


async def assert_bank_version_monotone(cluster: ClusterHarness) -> None:
    """
    Sur chaque store, la chaîne de commits de bank doit être CONTIGUË et bien
    parentée (HIVEMIND.md ; models.BankCommit) :

    1. le pointeur bank_version pointe vers un commit présent et ne dépasse pas
       le dernier commit matérialisé (pas de pointeur en avance) ;
    2. les ``bank_version`` committés sont CONTIGUS à partir de 0, sans trou :
       ``[0, 1, 2, …]``. Une chaîne ``[0, 2]`` (trou à 1) viole l'invariant —
       le simple tri croissant la laissait passer (false negative) ;
    3. chaque commit porte ``parent_bank_version == bank_version - 1`` (le
       commit 0 a un parent à -1). Un parent erroné casse le chaînage du DAG de
       bank même si les versions sont triées.

    Lève une ``AssertionError`` nommant le nœud / la version fautive.
    """
    for nid in cluster.node_ids():
        store = cluster.nodes[nid].store
        pointer = await store.get_bank_version_pointer()
        commits = await store.list_commits()
        if pointer is None or pointer.bank_version < 0:
            continue
        max_commit = commits[-1].bank_version if commits else -1
        if pointer.bank_version > max_commit:
            raise AssertionError(
                f"Invariant bank_version: nœud {nid!r} pointeur="
                f"{pointer.bank_version} > dernier commit={max_commit} "
                f"(pointeur en avance sur les commits)"
            )
        # (2) Contiguïté : les bank_versions doivent former [0, 1, …, N], sans
        # trou. ``list_commits`` retourne déjà l'ordre croissant de version.
        versions = [c.bank_version for c in commits]
        if versions != sorted(versions):
            raise AssertionError(
                f"Invariant bank_version: nœud {nid!r} commits non triés "
                f"{versions}"
            )
        expected = list(range(len(versions)))
        if versions != expected:
            raise AssertionError(
                f"Invariant bank_version: nœud {nid!r} chaîne de commits NON "
                f"CONTIGUË {versions} (attendu {expected}) — trou dans "
                f"l'historique de bank (chaque version doit suivre la précédente)"
            )
        # (3) Parenté : chaque commit chaîne sur bank_version - 1.
        for commit in commits:
            expected_parent = commit.bank_version - 1
            if commit.parent_bank_version != expected_parent:
                raise AssertionError(
                    f"Invariant bank_version: nœud {nid!r} commit "
                    f"bank_version={commit.bank_version} a "
                    f"parent_bank_version={commit.parent_bank_version} "
                    f"(attendu {expected_parent}) — chaînage de parenté rompu"
                )


async def assert_membership_epoch_monotone(
    cluster: ClusterHarness, *, minimum: int | None = None
) -> None:
    """
    L'epoch de membership ne descend jamais. Si ``minimum`` est fourni, chaque
    nœud doit être >= ce plancher (utile après une éviction qui bump l'epoch).
    """
    floor = 0 if minimum is None else minimum
    for nid in cluster.node_ids():
        view = await cluster.nodes[nid].store.get_membership()
        if view is None:
            continue
        if view.epoch < floor:
            raise AssertionError(
                f"Invariant epoch: nœud {nid!r} epoch={view.epoch} < "
                f"plancher attendu={floor}"
            )


# =============================================================================
# Pas de commit en term stale
# =============================================================================


async def assert_no_stale_term_commit(cluster: ClusterHarness) -> None:
    """
    Aucun commit matérialisé ne doit porter un ``term`` inférieur au term porté
    par un commit de bank_version ANTÉRIEURE sur le même store (le term des
    commits doit être non-décroissant le long de la chaîne bank_version), ni
    excéder le term courant du store.

    C'est le garde-fou de fencing à l'application : le modèle de référence doit
    rejeter un commit stale AVANT ``append_commit`` ; si malgré tout un commit
    stale apparaît dans ``commits/``, cette assertion le révèle.
    """
    for nid in cluster.node_ids():
        store = cluster.nodes[nid].store
        term_state = await store.get_term()
        current_term = term_state.term if term_state else 0
        commits: list[BankCommit] = await store.list_commits()
        prev_term = -1
        for commit in commits:
            if commit.term < prev_term:
                raise AssertionError(
                    f"Invariant fencing: nœud {nid!r} commit "
                    f"bank_version={commit.bank_version} term={commit.term} < "
                    f"term d'un commit antérieur ({prev_term}) — commit stale "
                    f"matérialisé"
                )
            if commit.term > current_term:
                raise AssertionError(
                    f"Invariant fencing: nœud {nid!r} commit "
                    f"bank_version={commit.bank_version} term={commit.term} > "
                    f"term courant ({current_term}) — commit d'un term inexistant"
                )
            prev_term = commit.term


# =============================================================================
# Cohérence des commits (même bank_version => même commit_id partout)
# =============================================================================


async def assert_commits_consistent(cluster: ClusterHarness) -> None:
    """
    Pour une même ``bank_version`` observée sur deux nœuds, le ``commit_id``
    doit être identique : deux commits divergents à la même version = split de
    l'historique de bank.
    """
    by_version: dict[int, tuple[str, str]] = {}  # bank_version -> (commit_id, node)
    for nid in cluster.node_ids():
        for commit in await cluster.nodes[nid].store.list_commits():
            existing = by_version.get(commit.bank_version)
            if existing is None:
                by_version[commit.bank_version] = (commit.commit_id, nid)
            elif existing[0] != commit.commit_id:
                raise AssertionError(
                    f"Invariant commit divergent: bank_version="
                    f"{commit.bank_version} a commit_id={existing[0]!r} sur "
                    f"{existing[1]!r} mais commit_id={commit.commit_id!r} sur "
                    f"{nid!r}"
                )


# =============================================================================
# Anti-résurrection cross-store (P5-2 #3 / P5-7 — ADR-0013 §5.2)
# =============================================================================


async def assert_no_tombstone_resurrection(cluster: ClusterHarness) -> None:
    """
    Oracle anti-résurrection cross-store (ADR-0013 §5.2 ; P5-2 #3 manquant).

    Pour CHAQUE nœud : scanne ses live notes (objets sous ``{space}/live/``)
    contre ses tombstones (``list_tombstones``, MÊME ``note_id`` —
    ``origin_note_id`` est l'alias, jamais une 2e clé). FAIL si un ``note_id``
    tombstoné est présent comme objet live sur CE nœud. Per-node (souveraineté du
    store).

    Identité : pour un objet ``{stem}.md``, ``note_id`` == stem (basename moins
    ``.md``), EXACTEMENT comme ``note_id_from_filename`` du runtime — sinon
    l'oracle serait une tautologie. Les skips sont MINIMAUX : SEULEMENT la
    sentinelle bootstrap EXACTE ``live/.keep`` (rel == ``.keep``) et l'arbre
    ``_origin/`` (sidecars de provenance) ; ce ne sont pas des notes. On NE saute
    PAS les ``*.keep`` arbitraires : un objet live d'extension étrangère terminant
    par ``.keep`` (p.ex. ``live/foo.keep``) reste confronté aux tombstones — un
    ``endswith(".keep")`` sur-inclusif laisserait sinon un tel objet tombstoné
    ressusciter silencieusement.

    BALAYAGE EXHAUSTIF (fail-closed) : on ne saute PAS les objets sans extension
    ``.md``. Un objet live extensionless / d'extension étrangère (que
    ``reap_on_tombstone`` ne résoudrait pas via ``{note_id}.md``) doit AUSSI être
    confronté aux tombstones — son ``note_id`` candidat est alors le basename
    BRUT (live/foo -> note_id ``foo``). Sans ce balayage, un objet live tombstoné
    non-``.md`` survivrait silencieusement à son tombstone (le bypass exact que la
    garde ``.md`` de ``note_id_from_filename`` ferme côté écriture).

    Fail-closed : un tombstone corrompu fait remonter ``CorruptedStateError``
    depuis ``list_tombstones`` (NON rattrapé) — la corruption bloque, jamais lue
    comme « absent ».
    """
    for nid in cluster.node_ids():
        store = cluster.nodes[nid].store
        storage = cluster.nodes[nid].storage
        tombstoned = {t.note_id for t in await store.list_tombstones()}
        if not tombstoned:
            continue
        live_prefix = f"{cluster.space_id}/live/"
        for obj in await storage.list_objects(live_prefix):
            rel = obj["Key"][len(live_prefix):]
            # Skips MINIMAUX (fail-closed) : SEULEMENT la sentinelle bootstrap
            # exacte ``live/.keep`` (rel == ``.keep``) et l'arbre des sidecars de
            # provenance ``_origin/``. Un ``rel.endswith(".keep")`` serait
            # SUR-INCLUSIF : un objet live tombstoné d'extension étrangère
            # (p.ex. ``live/foo.keep``) échapperait alors à l'oracle. On confronte
            # donc TOUT autre objet — y compris ``*.keep`` non-sentinelle — aux
            # tombstones.
            if rel == "" or rel == ".keep" or rel.startswith("_origin/"):
                continue  # placeholder bootstrap / sidecar dir, pas une note
            # ``{stem}.md`` -> note_id == stem ; sinon (objet non-``.md`` qui ne
            # devrait jamais exister sous live/) note_id == basename brut. Les
            # DEUX sont confrontés aux tombstones : un objet tombstoné qui aurait
            # échappé au reaper {note_id}.md est ainsi attrapé.
            note_id = rel[: -len(".md")] if rel.endswith(".md") else rel
            if note_id in tombstoned:
                raise AssertionError(
                    f"Invariant ANTI-RESURRECTION: nœud {nid!r} a un objet live "
                    f"{rel!r} (note_id {note_id!r}) alors qu'un tombstone existe "
                    f"pour ce note_id (résurrection — ADR-0013 §5.2)"
                )


# =============================================================================
# Agrégat
# =============================================================================


async def assert_invariants(
    cluster: ClusterHarness, *, min_epoch: int | None = None
) -> None:
    """
    Lance TOUS les invariants V1 cross-nœud. À appeler après chaque pas.
    Lève la première ``AssertionError`` rencontrée (nommant le fautif).
    """
    await assert_at_most_one_valid_holder(cluster)
    await assert_term_monotone(cluster)
    await assert_bank_version_monotone(cluster)
    await assert_membership_epoch_monotone(cluster, minimum=min_epoch)
    await assert_no_stale_term_commit(cluster)
    await assert_commits_consistent(cluster)
    await assert_no_tombstone_resurrection(cluster)  # ← P5-7 / P5-2 #3
