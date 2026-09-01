# -*- coding: utf-8 -*-
"""
Runtime de staging + commit atomique de bank + tombstones/watermarks Hivemind
(issue #14 / P5-6). Mirror de ``lease_runtime`` / ``queue_runtime`` : deux
couches pure/async.

Ce module porte la **choreographie BANK_COMMIT** de HIVEMIND.md §5.3 (steps
5-9) côté apply. Il ne RÉ-implémente JAMAIS l'autorisation : le SEUL point
d'autorisation est ``LeaseRuntime.assert_commit_allowed`` (ADR-0011), appelé en
G0 AVANT toute mutation (et donc AVANT ``append_commit`` — ce qui ferme le trou
« append_commit contourne le fencing »).

ADRs portés :

- **ADR-0011** : ``assert_commit_allowed`` est le point d'autorisation unique.
  ``commit_runtime`` n'ajoute AUCUNE logique term/fencing/holder ; il ajoute un
  contrôle d'INTÉGRITÉ distinct (``CommitApplyError`` : manifest/checksum/
  parenté/exclusion), jamais un second contrôle d'autorisation.
- **ADR-0007** : le chemin STAGED du WriteSink split. Le holder stage sous
  ``staging/{commit_id}/`` (MANIFEST.json écrit en dernier = marqueur publish),
  puis l'apply promeut atomiquement vers le bank vivant.
- **ADR-0013** : events append-only ; identité tombstone par ``note_id`` (PAS de
  second ``origin_note_id``) ; DISTINCTION des deux watermarks — ``bank_version``
  (progrès APPLIQUÉ, gate GC/commit) vs ``last_event_id``/``last_event_ts``
  (curseur de POSITION d'event, gate replication uniquement). Le commit n'écrit
  JAMAIS le curseur d'event ; il le PORTE inchangé.
- **ADR-0012** : le bloc ``graph_memory`` de ``_meta.json`` est local-only et
  EXCLU du manifest de commit. La seule porte d'entrée d'un ``_meta.json`` dans
  le staging est ``staged_meta_text`` (projection ``meta_shared_projection``) ;
  un re-check ``assert_no_graph_memory_in_manifest`` ré-asserte l'absence.

Invariants protocole portés :

- full-mesh all-ACK (pas de quorum) : le plancher de GC est le MIN cross-peer
  des watermarks ``bank_version`` + une garde ``expected_node_ids.issubset`` —
  un seul peer en retard/absent bloque la GC (jamais un compte) ;
- pas de mémoire longue/graph dans le chemin de commit (ce module n'importe ni
  ``graph_push`` ni ``consolidation_queue``/``consolidator``/``long`` — vérifié
  par scan AST) ;
- pas de timer ;
- fail-closed : ``CorruptedStateError`` propage (aucun ``try/except`` autour des
  lectures de store) ; un mismatch de checksum / stage partiel / manifest
  incomplet / graph_memory dans le manifest FERME le commit (aucun apply partiel)
  AVANT toute mutation ;
- atomicité : le flip du pointeur ``bank_version.json`` est le POINT DE
  LINÉARISATION (avancé en DERNIER parmi l'état, monotone) ; JOURNAL-FIRST (le
  record durable ``commits/{N}`` précède TOUTE mutation du bank vivant, donc la
  promotion live est une matérialisation idempotente complétée par roll-forward) ;
  journal-avant-tombstones ; chaque étape est idempotente-par-clé → un crash à
  n'importe quelle frontière laisse un état recouvrable par roll-forward (jamais
  de pointeur orphelin nommant N sur un bank N-1, jamais de set de tombstones à
  demi-appliqué). RISQUE RÉSIDUEL borné et documenté dans ``apply_commit`` : le
  bank vivant étant UNVERSIONNÉ, une lecture concurrente entre le 1er put live et
  le flip peut voir un mélange N-1/N transitoire (guéri par roll-forward) ;
  l'élimination complète exige un bank immuable-par-version, hors scope ici.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..models import meta_shared_projection
from ..reservation_guard import assert_no_pairing_activation, assert_space_not_reserved
from ..storage import StorageService
from . import layout

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .note_replication import NoteReplicationRuntime
from .lease_runtime import (
    Clock,
    CommitIntent,
    CommitNotAuthorized,
    LeaseRuntime,
)
from .lifecycle import _sha256_bytes
from .models import (
    BankCommit,
    BankCommitManifestEntry,
    BankVersionPointer,
    CorruptedStateError,
    EventEnvelope,
    EventType,
    TokenState,
    Tombstone,
    Watermark,
)
from .state import HivemindStateStore


# =============================================================================
# Horloge injectable (même seam que lease_runtime / queue_runtime)
# =============================================================================


def _now_utc() -> datetime:
    """Instant courant UTC (aware). Seule lecture d'horloge murale du module,
    injectable via le seam ``clock=`` de ``CommitRuntime``."""
    return datetime.now(timezone.utc)


# =============================================================================
# Couche pure — exception d'intégrité + raisons stables
# =============================================================================


class CommitApplyReason(str, Enum):
    """
    Codes de refus stables d'intégrité d'un commit (manifest/checksum/parenté/
    exclusion). DISTINCT de ``CommitDenyReason`` (autorisation term/fencing).

    ``str``-Enum pour rester aligné sur la convention du codebase
    (``CommitDenyReason`` / ``TokenState``) : sérialisable et comparable à sa
    chaîne.
    """

    PARENT_MISMATCH = "parent_mismatch"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    MANIFEST_INCOMPLETE = "manifest_incomplete"  # entrée de manifest sans fichier stagé
    PARTIAL_STAGE = "partial_stage"  # fichier stagé hors du manifest
    GRAPH_MEMORY_IN_MANIFEST = "graph_memory_in_manifest"
    INTENT_PARENT_MISMATCH = "intent_parent_mismatch"  # intent.previous != commit.parent
    INTENT_TERM_MISMATCH = "intent_term_mismatch"  # intent.term != commit.term
    INTENT_COMMIT_ID_MISMATCH = "intent_commit_id_mismatch"  # intent.commit_id != commit.commit_id
    INTENT_HOLDER_MISMATCH = "intent_holder_mismatch"  # intent.holder_node_id != commit.committed_by_node_id
    FENCING_TOKEN_MISMATCH = "fencing_token_mismatch"  # fencing_token arg != intent.fencing_token
    RESUME_COMMIT_DIVERGENT = "resume_commit_divergent"  # commit fourni != commits/{N} durable
    STAGING_MANIFEST_MISSING = "staging_manifest_missing"  # MANIFEST.json publish marker absent
    STAGING_MANIFEST_DIVERGENT = "staging_manifest_divergent"  # MANIFEST.json != commit fourni


class CommitApplyError(RuntimeError):
    """
    Levée quand l'INTÉGRITÉ d'un commit est rompue (manifest/checksum/parenté/
    exclusion). FERME le commit : aucun apply, aucune mutation.

    DISTINCT de ``CommitNotAuthorized`` (ADR-0011, term/fencing/holder/CAS, porté
    par ``LeaseRuntime``). Tests et callers ne doivent JAMAIS les confondre :
    ``CommitNotAuthorized`` = « tu n'as pas le droit de committer » ;
    ``CommitApplyError`` = « tes octets/ta forme sont malformés ».

    Forme à code de raison, mirror de ``CommitNotAuthorized.to_dict()``.
    """

    def __init__(
        self,
        reason: CommitApplyReason,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "reason": self.reason.value,
            "message": str(self),
            "details": self.details,
        }


# =============================================================================
# Couche pure — projection meta, manifest, checksums (sans I/O)
# =============================================================================


def staged_meta_text(raw_meta: dict | None) -> str:
    """
    Texte canonique de la PROJECTION PARTAGÉE de ``_meta.json``.

    ``graph_memory`` et tout champ non whitelisté sont STRUCTURELLEMENT absents
    (``meta_shared_projection``, core/models.py — ADR-0012). C'est la SEULE porte
    d'entrée d'un ``_meta.json`` dans le staging : un peer réplique les compteurs
    partagés (``consolidation_count``, etc.) sans jamais voir le bloc graph.

    Sérialisation déterministe (``sort_keys`` + séparateurs compacts) pour un
    checksum byte-stable cross-host.
    """
    projected = meta_shared_projection(raw_meta) or {}
    return json.dumps(projected, sort_keys=True, separators=(",", ":"))


def manifest_entry_for(rel_path: str, content: str) -> BankCommitManifestEntry:
    """Une entrée de manifest pour ``(rel_path, content)``. Le checksum réutilise
    ``lifecycle._sha256_bytes`` sur les octets UTF-8 EXACTS — byte-identique au
    checksum d'un snapshot de bootstrap pour le même contenu."""
    raw = content.encode("utf-8")
    return BankCommitManifestEntry(
        path=rel_path, sha256=_sha256_bytes(raw), size=len(raw)
    )


def build_manifest(staged: list[tuple[str, str]]) -> list[BankCommitManifestEntry]:
    """
    Construit le manifest à partir de ``[(rel_path, content_text), ...]`` (déjà
    projeté/exclu côté caller). Trié par ``path`` pour un ordre byte-stable
    (mirror de la construction de manifest de bootstrap dans lifecycle).
    """
    return [
        manifest_entry_for(p, c) for p, c in sorted(staged, key=lambda x: x[0])
    ]


def build_commit_intent(
    commit: BankCommit, *, holder_node_id: str, fencing_token: int
) -> CommitIntent:
    """
    Construit le ``CommitIntent`` (input du gate ADR-0011) à partir d'un
    ``BankCommit``. La source du CAS (``previous_bank_version``) est le
    ``parent_bank_version`` DU COMMIT lui-même — jamais re-dérivé indépendamment
    en ``bank_version-1`` (sinon les deux parents pourraient diverger
    silencieusement ; cf. ``assert_intent_matches_commit``).
    """
    return CommitIntent(
        holder_node_id=holder_node_id,
        term=commit.term,
        fencing_token=fencing_token,
        bank_version=commit.bank_version,
        previous_bank_version=commit.parent_bank_version,
        commit_id=commit.commit_id,
    )


def assert_parent_contiguous(commit: BankCommit) -> None:
    """G1 — forme interne : ``parent_bank_version == bank_version - 1``. DISTINCT
    du CAS contre le pointeur vivant (porté par le gate ADR-0011, étape 4)."""
    expected = commit.bank_version - 1
    if commit.parent_bank_version != expected:
        raise CommitApplyError(
            CommitApplyReason.PARENT_MISMATCH,
            f"commit closed: non-contiguous parent "
            f"(bank_version={commit.bank_version}, "
            f"parent={commit.parent_bank_version}, expected={expected})",
            {
                "bank_version": commit.bank_version,
                "parent": commit.parent_bank_version,
                "expected": expected,
            },
        )


def assert_intent_matches_commit(commit: BankCommit, intent: CommitIntent) -> None:
    """G2 — accord intent/commit : le ``CommitIntent`` AUTORISÉ par le gate ADR-0011
    (G0) DOIT LIER le ``BankCommit`` réellement appliqué sur TOUS ses champs porteurs
    d'autorisation/identité, pas seulement les versions du CAS.

    ``assert_commit_allowed(intent)`` n'autorise QUE l'``intent`` ; sans G2 complet,
    un holder vivant pourrait présenter un intent valide (term/holder/version
    courants) tout en appliquant un ``BankCommit`` STAGÉ dont le ``term`` est stale,
    le ``commit_id`` divergent, ou le ``committed_by_node_id`` mal attribué — ce qui
    enregistrerait/émettrait de l'état protocole (journal/tombstones/watermark/release)
    depuis des champs NON authentifiés, violant l'invariant single-holder/stale-term.
    On lie donc :

    - ``intent.bank_version`` == ``commit.bank_version`` (version CIBLE du CAS) ;
    - ``intent.previous_bank_version`` == ``commit.parent_bank_version`` (parent du CAS) ;
    - ``intent.term`` == ``commit.term`` (le commit ne peut pas être à un term ≠ de
      l'autorisation) ;
    - ``intent.commit_id`` == ``commit.commit_id`` (le commit appliqué EST celui autorisé) ;
    - ``intent.holder_node_id`` == ``commit.committed_by_node_id`` (le committer EST le
      holder autorisé).

    Toute divergence FERME le commit AVANT ``append_commit`` (aucune mutation)."""
    if intent.bank_version != commit.bank_version:
        raise CommitApplyError(
            CommitApplyReason.INTENT_PARENT_MISMATCH,
            f"commit closed: CAS target version diverges from commit "
            f"(intent.bank_version={intent.bank_version}, "
            f"commit.bank_version={commit.bank_version})",
            {
                "intent_bank_version": intent.bank_version,
                "commit_bank_version": commit.bank_version,
            },
        )
    if intent.previous_bank_version != commit.parent_bank_version:
        raise CommitApplyError(
            CommitApplyReason.INTENT_PARENT_MISMATCH,
            f"commit closed: CAS parent diverges from commit parent "
            f"(intent.previous={intent.previous_bank_version}, "
            f"commit.parent={commit.parent_bank_version})",
            {
                "intent_previous_bank_version": intent.previous_bank_version,
                "commit_parent_bank_version": commit.parent_bank_version,
            },
        )
    if intent.term != commit.term:
        raise CommitApplyError(
            CommitApplyReason.INTENT_TERM_MISMATCH,
            f"commit closed: authorized term diverges from commit "
            f"(intent.term={intent.term}, commit.term={commit.term})",
            {"intent_term": intent.term, "commit_term": commit.term},
        )
    if intent.commit_id != commit.commit_id:
        raise CommitApplyError(
            CommitApplyReason.INTENT_COMMIT_ID_MISMATCH,
            f"commit closed: authorized commit_id diverges from applied commit "
            f"(intent.commit_id={intent.commit_id!r}, "
            f"commit.commit_id={commit.commit_id!r})",
            {
                "intent_commit_id": intent.commit_id,
                "commit_commit_id": commit.commit_id,
            },
        )
    if intent.holder_node_id != commit.committed_by_node_id:
        raise CommitApplyError(
            CommitApplyReason.INTENT_HOLDER_MISMATCH,
            f"commit closed: authorized holder diverges from commit committer "
            f"(intent.holder_node_id={intent.holder_node_id!r}, "
            f"commit.committed_by_node_id={commit.committed_by_node_id!r})",
            {
                "intent_holder_node_id": intent.holder_node_id,
                "commit_committed_by_node_id": commit.committed_by_node_id,
            },
        )


def assert_durable_commit_matches(
    commit: BankCommit, durable: BankCommit
) -> None:
    """
    Resume-path : le ``BankCommit`` fourni par le caller DOIT être byte-pour-byte
    cohérent avec le record DURABLE ``commits/{bank_version}`` (la seule source de
    vérité, écrite AVANT le flip du pointeur dans le chemin nominal).

    On compare les champs qui DÉFINISSENT le commit et déterminent toutes les
    mutations post-pointeur (tombstones/watermark/release) : ``commit_id``,
    ``parent_bank_version``, ``term``, ``membership_epoch``, ``notes_consumed`` et
    le manifest (path + sha256 + size). Toute divergence -> ``RESUME_COMMIT_DIVERGENT``
    (fail-closed) : un payload in-memory/réseau divergent qui partage seulement
    ``(bank_version, commit_id)`` avec le pointeur NE DOIT JAMAIS muter l'état.

    Pré-condition : ``durable.bank_version == commit.bank_version`` (le caller
    charge ``get_commit(commit.bank_version)``). On ne re-vérifie pas l'égalité de
    ``bank_version`` ici (c'est la clé de lecture du record durable).
    """
    mismatches: list[str] = []
    if durable.commit_id != commit.commit_id:
        mismatches.append("commit_id")
    if durable.parent_bank_version != commit.parent_bank_version:
        mismatches.append("parent_bank_version")
    if durable.term != commit.term:
        mismatches.append("term")
    if durable.membership_epoch != commit.membership_epoch:
        mismatches.append("membership_epoch")
    if list(durable.notes_consumed) != list(commit.notes_consumed):
        mismatches.append("notes_consumed")
    durable_manifest = [(e.path, e.sha256, e.size) for e in durable.manifest]
    commit_manifest = [(e.path, e.sha256, e.size) for e in commit.manifest]
    if durable_manifest != commit_manifest:
        mismatches.append("manifest")
    if mismatches:
        raise CommitApplyError(
            CommitApplyReason.RESUME_COMMIT_DIVERGENT,
            f"resume closed: supplied commit diverges from durable record "
            f"commits/{commit.bank_version} on {sorted(mismatches)} "
            f"(the pointer names this version but the payload differs)",
            {
                "bank_version": commit.bank_version,
                "commit_id": commit.commit_id,
                "durable_commit_id": durable.commit_id,
                "fields": sorted(mismatches),
            },
        )


def assert_staging_manifest_matches(
    commit: BankCommit, staged_manifest: BankCommit | None
) -> None:
    """
    G3-publish : vérifie le MARQUEUR DE PUBLICATION ``staging/{commit_id}/MANIFEST.json``
    que ``stage_commit`` écrit EN DERNIER (ADR-0007). C'est la SEULE preuve qu'un
    stage a été INTÉGRALEMENT publié : un crash de ``stage_commit`` avant ce put
    laisse un arbre stagé sans MANIFEST.json, donc jamais autoritaire.

    Sans cette garde, ``apply_commit`` valide le ``BankCommit`` FOURNI par le caller
    contre les fichiers bank stagés, mais NE confirme JAMAIS que le stage a été
    publié : un caller qui (re-)présente un ``BankCommit`` cohérent avec un arbre
    PARTIELLEMENT stagé (manifest non encore écrit) ferait apply-OPEN sur un stage
    inachevé. On FERME donc :

    - marqueur ABSENT (``staged_manifest is None``)   -> ``STAGING_MANIFEST_MISSING``
      (stage jamais publié, ou corrompu : ``get_json`` -> ``None`` ; un JSON
      malformé propage ``CorruptedStateError`` en amont, fail-closed) ;
    - marqueur DIVERGENT du commit fourni             -> ``STAGING_MANIFEST_DIVERGENT``.

    L'égalité réutilise EXACTEMENT les champs DÉFINISSANTS de
    ``assert_durable_commit_matches`` (commit_id/parent/term/membership_epoch/
    notes_consumed/manifest) PLUS ``bank_version`` — car ici la clé de lecture est
    le ``commit_id`` (et non ``bank_version`` comme pour le record durable), donc
    ``bank_version`` DOIT être comparé explicitement. ``committed_at`` est EXCLU
    (horodatage non déterministe, jamais un champ d'identité — mirror exact de
    ``assert_durable_commit_matches``).
    """
    if staged_manifest is None:
        raise CommitApplyError(
            CommitApplyReason.STAGING_MANIFEST_MISSING,
            f"commit closed: publication marker "
            f"staging/{commit.commit_id}/MANIFEST.json is ABSENT "
            f"(stage was never published or is incomplete — manifest-last was not reached)",
            {"commit_id": commit.commit_id, "bank_version": commit.bank_version},
        )
    mismatches: list[str] = []
    if staged_manifest.bank_version != commit.bank_version:
        mismatches.append("bank_version")
    if staged_manifest.commit_id != commit.commit_id:
        mismatches.append("commit_id")
    if staged_manifest.parent_bank_version != commit.parent_bank_version:
        mismatches.append("parent_bank_version")
    if staged_manifest.term != commit.term:
        mismatches.append("term")
    if staged_manifest.membership_epoch != commit.membership_epoch:
        mismatches.append("membership_epoch")
    if list(staged_manifest.notes_consumed) != list(commit.notes_consumed):
        mismatches.append("notes_consumed")
    staged_entries = [(e.path, e.sha256, e.size) for e in staged_manifest.manifest]
    commit_entries = [(e.path, e.sha256, e.size) for e in commit.manifest]
    if staged_entries != commit_entries:
        mismatches.append("manifest")
    if mismatches:
        raise CommitApplyError(
            CommitApplyReason.STAGING_MANIFEST_DIVERGENT,
            f"commit closed: staging marker staging/{commit.commit_id}/MANIFEST.json "
            f"diverges from supplied commit on {sorted(mismatches)} "
            f"(stage was published for another commit/shape)",
            {
                "commit_id": commit.commit_id,
                "bank_version": commit.bank_version,
                "staged_commit_id": staged_manifest.commit_id,
                "fields": sorted(mismatches),
            },
        )


def verify_manifest_against_staged(
    commit: BankCommit,
    staged: dict[str, str],
    *,
    actual_staged_paths: set[str] | None = None,
) -> None:
    """
    Vérifie l'intégrité du set stagé contre le manifest. FAIL CLOSED, aucun apply
    partiel. Égalité EXACTE des ensembles de paths + sha256/size par fichier :

    - path du manifest absent du stagé        -> ``MANIFEST_INCOMPLETE`` ;
    - path stagé hors du manifest             -> ``PARTIAL_STAGE`` ;
    - sha256/size divergent                   -> ``CHECKSUM_MISMATCH``.

    ``actual_staged_paths`` (optionnel) = l'ensemble des ``rel_path`` RÉELLEMENT
    présents sous le préfixe bank stagé (LIST du store, via
    ``list_staged_bank_paths``). Quand il est fourni, l'union avec ``staged``
    sert au calcul des extras : ainsi un objet stagé HORS manifest — donc jamais
    relu par ``load_staged`` (qui ne lit que les paths du manifest) — est tout de
    même détecté et FERME le commit en ``PARTIAL_STAGE``. Sans lui, seul le dict
    ``staged`` (forme manifest) est comparé (mode pur historique).
    """
    manifest_paths = {e.path for e in commit.manifest}
    staged_paths = set(staged)
    if actual_staged_paths is not None:
        staged_paths = staged_paths | actual_staged_paths
    missing = manifest_paths - staged_paths
    if missing:
        raise CommitApplyError(
            CommitApplyReason.MANIFEST_INCOMPLETE,
            f"commit closed: incomplete stage, manifest paths are missing "
            f"{sorted(missing)} (partial stage was never published)",
            {"missing_paths": sorted(missing)},
        )
    extra = staged_paths - manifest_paths
    if extra:
        raise CommitApplyError(
            CommitApplyReason.PARTIAL_STAGE,
            f"commit closed: staged files outside manifest {sorted(extra)} "
            f"(inconsistent set)",
            {"extra_paths": sorted(extra)},
        )
    for e in commit.manifest:
        raw = staged[e.path].encode("utf-8")
        if len(raw) != e.size or _sha256_bytes(raw) != e.sha256:
            raise CommitApplyError(
                CommitApplyReason.CHECKSUM_MISMATCH,
                f"commit closed: checksum/size diverges for {e.path!r} "
                f"(expected sha256={e.sha256} size={e.size}, "
                f"observed sha256={_sha256_bytes(raw)} size={len(raw)})",
                {"path": e.path},
            )


def assert_no_graph_memory_in_manifest(
    commit: BankCommit, staged: dict[str, str]
) -> None:
    """
    ADR-0012 (defense in depth). Si une entrée de manifest est ``_meta.json``,
    son texte stagé désérialisé NE DOIT PAS contenir la clé ``graph_memory`` (il
    est passé par ``staged_meta_text`` qui la strippe). Une clé ``graph_memory``
    résiduelle -> ``GRAPH_MEMORY_IN_MANIFEST``.
    """
    for e in commit.manifest:
        if e.path == "_meta.json" and "graph_memory" in json.loads(staged[e.path]):
            raise CommitApplyError(
                CommitApplyReason.GRAPH_MEMORY_IN_MANIFEST,
                "commit closed: staged _meta.json contains a graph_memory block "
                "(local-only, ADR-0012) — never replicated in a commit",
                {"path": e.path},
            )


# =============================================================================
# Couche pure — min cross-peer + éligibilité GC (ADR-0013)
# =============================================================================


def min_applied_bank_version(watermarks: list[Watermark]) -> int:
    """
    MIN cross-peer du watermark de PROGRÈS APPLIQUÉ (``Watermark.bank_version``).

    Liste vide -> ``-1`` (bloque la GC). Un peer à ``-1`` ramène le min à ``-1``.
    Le curseur de POSITION d'event (``last_event_id``/``last_event_ts``) est
    IGNORÉ ici — on ne le lit JAMAIS pour la GC (ADR-0013, distinction des deux
    watermarks).
    """
    if not watermarks:
        return -1
    return min(w.bank_version for w in watermarks)


def gc_eligible(tombstone: Tombstone, min_applied: int) -> bool:
    """Un tombstone est GC-éligible ssi ``0 <= bank_version < min_applied``
    (strict). ``bank_version == -1`` (non associé à un commit) n'est jamais
    évincé. Mirror EXACT de ``state.garbage_collect_tombstones``."""
    return 0 <= tombstone.bank_version < min_applied


# =============================================================================
# Couche async — CommitRuntime (store-facing)
# =============================================================================


class CommitRuntime:
    """
    Wrapper async mince autour de ``HivemindStateStore`` + ``StorageService`` +
    ``LeaseRuntime`` pour le staging + l'apply atomique d'un ``BankCommit``.

    Symétrie writer/peer : ``apply_commit`` est À LA FOIS la primitive atomique
    du writer local ET la primitive d'apply-on-receipt d'un pair. Tous deux
    gatent via ``assert_commit_allowed`` contre LEUR PROPRE état chargé. Le
    release du token (étape 9) est CONVERGENT et non plus holder-only : tout peer
    qui applique le commit fait passer son ``token.json`` local à FREE s'il est
    encore HELD par ``commit.committed_by_node_id`` au ``commit.term`` (le holder
    a libéré en publiant le commit, §5.3 step 9). Cela garde l'état partagé du
    token convergent en full-mesh, sans bloquer le prochain ``acquire`` d'un pair
    jusqu'à l'expiration de lease.

    Fail-closed : aucune méthode ne rattrape ``CorruptedStateError``. L'horloge
    est lue UNE FOIS par méthode publique (``now = self._clock()``) et threadée.
    """

    def __init__(
        self,
        store: HivemindStateStore,
        storage: StorageService,
        space_id: str,
        lease: LeaseRuntime,
        *,
        clock: Clock = _now_utc,
        note_replication: "NoteReplicationRuntime | None" = None,
    ) -> None:
        if space_id != store.space_id:
            raise ValueError(
                f"CommitRuntime space_id={space_id!r} != "
                f"store.space_id={store.space_id!r}"
            )
        if space_id != lease.space_id:
            raise ValueError(
                f"CommitRuntime space_id={space_id!r} != "
                f"lease.space_id={lease.space_id!r}"
            )
        self._store = store
        self._storage = storage
        self._space_id = space_id
        self._lease = lease
        self._clock = clock
        # P5-7 cross-seam (#15 -> #16): OPTIONAL injected note-replication runtime.
        # When present, apply_commit reaps the live copy of each tombstoned note
        # (note-first-reorder window). Optional so the P5-6 commit tests that
        # build CommitRuntime without it stay unchanged (no AttributeError, reap
        # is a no-op seam). NOT imported at module top — commit_runtime stays
        # graph/long/consolidation import-clean; the instance is injected by the
        # engine registry.
        self._note_replication = note_replication

    @property
    def space_id(self) -> str:
        return self._space_id

    # ─────────────────────────────────────────────────────────────────
    # stage_commit — écrire les résultats sous staging/{commit_id}/
    # (crash ici = totalement recouvrable, le bank vivant est intouché)
    # ─────────────────────────────────────────────────────────────────

    async def stage_commit(
        self,
        *,
        commit_id: str,
        proposed_bank: list[tuple[str, str]],
        bank_version: int,
        parent_bank_version: int,
        term: int,
        membership_epoch: int,
        committed_by_node_id: str,
        event_id: str,
        request_id: str = "",
        notes_consumed: list[str],
    ) -> BankCommit:
        """
        Stage les fichiers bank proposés + publie le ``BankCommit`` (manifest).

        ``proposed_bank`` = ``[(rel_path, content_text), ...]`` DÉJÀ projeté/exclu
        (le caller passe ``_meta.json`` via ``staged_meta_text`` s'il l'inclut).

        Ordre (crash-safe) : on construit le ``BankCommit`` + on valide la
        contiguïté interne du parent, on écrit chaque fichier bank, PUIS on écrit
        ``MANIFEST.json`` EN DERNIER. Le manifest-last est le marqueur de
        publication : un crash avant lui laisse un arbre stagé sans manifest, donc
        jamais appliqué ; un re-run au même ``commit_id`` écrase idempotemment.
        Rien dans ``commits/``, ``bank_version.json``, ni le bank vivant ne bouge.
        """
        # Pairing e+1/e+2 is an all-ACK transition, not an interval in which
        # a source may publish unrelated shared state.  Check both target
        # reservation and the source activation fence before creating even a
        # recoverable staging prefix: a later pointer/term/head is not signed
        # authority for the target's retained e+1 import proof.
        await assert_space_not_reserved(self._space_id)
        await assert_no_pairing_activation(self._space_id)
        entries = build_manifest(proposed_bank)
        commit = BankCommit(
            bank_version=bank_version,
            parent_bank_version=parent_bank_version,
            term=term,
            membership_epoch=membership_epoch,
            commit_id=commit_id,
            event_id=event_id,
            request_id=request_id,
            committed_by_node_id=committed_by_node_id,
            manifest=entries,
            notes_consumed=list(notes_consumed),
        )
        assert_parent_contiguous(commit)

        for rel_path, content in proposed_bank:
            await self._storage.put(
                layout.staging_bank_key(self._space_id, commit_id, rel_path),
                content,
                content_type=self._content_type_for(rel_path),
            )
        # EN DERNIER : le manifest = marqueur de publication.
        await self._storage.put_json(
            layout.staging_manifest_key(self._space_id, commit_id),
            commit.model_dump(mode="json"),
        )
        return commit

    @staticmethod
    def _content_type_for(rel_path: str) -> str:
        if rel_path.endswith(".json"):
            return "application/json"
        return "text/plain; charset=utf-8"

    # ─────────────────────────────────────────────────────────────────
    # load_staged — relire les fichiers stagés (clé manifest -> texte)
    # ─────────────────────────────────────────────────────────────────

    async def load_staged(
        self, commit_id: str, manifest: list[BankCommitManifestEntry]
    ) -> dict[str, str]:
        """
        Relit le texte stagé de chaque entrée de manifest. Un fichier manquant
        (``get`` -> ``None``) est EXCLU du dict retourné — la garde
        ``verify_manifest_against_staged`` le diagnostiquera en
        ``MANIFEST_INCOMPLETE`` (fail closed). Ne valide rien lui-même : juste
        l'I/O de lecture.
        """
        out: dict[str, str] = {}
        for e in manifest:
            text = await self._storage.get(
                layout.staging_bank_key(self._space_id, commit_id, e.path)
            )
            if text is not None:
                out[e.path] = text
        return out

    async def load_staging_manifest(self, commit_id: str) -> BankCommit | None:
        """
        Relit le MARQUEUR DE PUBLICATION ``staging/{commit_id}/MANIFEST.json``
        (écrit EN DERNIER par ``stage_commit``, ADR-0007) et le désérialise en
        ``BankCommit``.

        Retourne ``None`` si le marqueur est ABSENT (stage jamais publié / crashé
        avant le manifest-last). Un JSON présent mais malformé propage
        ``CorruptedStateError`` (fail-closed, jamais avalé) : on N'enveloppe PAS la
        désérialisation d'un ``try/except``. La garde ``assert_staging_manifest_matches``
        traite l'absence et la divergence.
        """
        raw = await self._storage.get_json(
            layout.staging_manifest_key(self._space_id, commit_id)
        )
        if raw is None:
            return None
        return BankCommit.model_validate(raw)

    async def list_staged_bank_paths(self, commit_id: str) -> set[str]:
        """
        Liste les ``rel_path`` des objets RÉELLEMENT stagés sous
        ``staging/{commit_id}/bank/`` (LIST du store, pas la forme du manifest).

        Sert à fermer le trou « PARTIAL_STAGE invisible » : ``load_staged`` ne lit
        que les paths du manifest, donc un objet stagé EN PLUS (hors manifest)
        resterait silencieusement ignoré et abandonné. Cette liste, croisée avec
        le manifest par ``verify_manifest_against_staged``, fait FERMER l'apply
        (``PARTIAL_STAGE``) sur tout extra. Le ``rel_path`` est reconstruit en
        retirant le préfixe bank du ``Key`` S3.
        """
        prefix = layout.staging_bank_prefix(self._space_id, commit_id)
        objects = await self._storage.list_objects(prefix)
        return {
            obj["Key"][len(prefix):]
            for obj in objects
            if obj["Key"].startswith(prefix) and obj["Key"] != prefix
        }

    # ─────────────────────────────────────────────────────────────────
    # apply_commit — LA choreographie atomique ordonnée (HIVEMIND.md §5.3)
    # ─────────────────────────────────────────────────────────────────

    async def apply_commit(
        self,
        commit: BankCommit,
        intent: CommitIntent,
        *,
        local_node_id: str,
        fencing_token: int,
        reason: str = "consolidation",
    ) -> BankVersionPointer:
        """
        Applique un ``BankCommit`` de façon atomique-par-roll-forward.

        **Roll-forward POST-pointeur (reprise post-crash)** : à l'ENTRÉE, si le
        pointeur vivant nomme DÉJÀ ce commit (``pointer.bank_version ==
        commit.bank_version`` ET ``pointer.commit_id == commit.commit_id``), le POINT
        DE LINÉARISATION est déjà franchi. Ré-entrer G0/G2 lèverait
        ``VERSION_CONFLICT`` (le CAS voit le pointeur à N alors que ``intent.previous``
        est N-1). On RE-ANCRE sur le record DURABLE ``commits/{N}`` (vérifié byte-
        cohérent avec le commit fourni, sinon ``RESUME_COMMIT_DIVERGENT`` ; absent ->
        ``CorruptedStateError``) puis on FINIT idempotemment les étapes POST-pointeur
        (tombstones absents, watermark, release token) et on retourne.

        **Roll-forward PRÉ-pointeur (reprise SANS lease vivant)** : si le pointeur est
        ENCORE derrière (le flip n'a pas eu lieu) MAIS ``commits/{N}`` existe DÉJÀ
        (JOURNAL-FIRST : il n'a pu être écrit qu'APRÈS un G0 réussi), le commit a DÉJÀ
        été autorisé. La reprise FINIT cet apply déjà autorisé — ce n'est PAS un
        commit frais — donc elle NE re-exige PAS un lease vivant (qui peut avoir expiré
        entre le crash et la reprise) : sans cela, un re-run après expiration du lease
        échouerait en ``FENCED`` et laisserait un commit durable ORPHELIN. On vérifie
        le record durable matchant puis on matérialise (G3 + promote + flip + finish)
        SANS G0. GARDE MONOTONE : la reprise pré-pointeur ne se déclenche que si le
        pointeur est STRICTEMENT derrière N (un pointeur déjà au-delà -> commit
        superseded -> no-op, jamais de roll-back). Un commit FRAIS (pas de
        ``commits/{N}``) retombe sur G0 : l'autorisation fraîche reste pleinement
        gardée.

        Gates D'ABORD (chemin FRAIS, toute défaillance lève, ZÉRO mutation) :

        - **G0 — autorisation (point UNIQUE, ADR-0011)** :
          ``await self._lease.assert_commit_allowed(intent)``. Lève
          ``CommitNotAuthorized`` ; ``CorruptedStateError`` propage (fail-closed).
          AVANT ``append_commit`` -> ferme le trou « append_commit contourne le
          fencing ». Aucun contrôle d'autorisation parallèle ailleurs. La reprise
          roll-forward (pré- et post-pointeur) NE repasse PAS par G0.
        - **G1 — forme interne du parent** : ``assert_parent_contiguous``.
        - **G2 — accord intent/commit** : ``assert_intent_matches_commit``.
        - **G3 — load + intégrité du manifest** (dans ``_materialize_commit``, partagé
          par le chemin frais et la reprise pré-pointeur) : G3a vérifie le MARQUEUR DE
          PUBLICATION ``staging/{commit_id}/MANIFEST.json`` (écrit EN DERNIER par
          ``stage_commit``, ADR-0007) — absent -> ``STAGING_MANIFEST_MISSING``,
          divergent -> ``STAGING_MANIFEST_DIVERGENT`` (un stage partiellement publié
          ne s'applique JAMAIS) ; puis ``load_staged`` + ``list_staged_bank_paths``
          (LIST réel du store) + ``verify_manifest_against_staged`` +
          ``assert_no_graph_memory_in_manifest``. Fail closed, aucun apply partiel. La
          LIST réelle ferme le trou « objet stagé hors manifest invisible » : tout
          extra -> ``PARTIAL_STAGE``.

        Étapes d'APPLY (ordonnées pour qu'un crash à chaque frontière laisse un
        état recouvrable par roll-forward ; toutes idempotentes-par-clé) :

        1. ``append_commit`` (journal ; idempotent par ``commit_id``) — **JOURNAL-
           FIRST**, AVANT toute mutation du bank vivant. Le journal porte le manifest
           complet + nomme l'arbre stagé, donc il SUFFIT à re-matérialiser le bank.
        2. Promote staged -> bank vivant (``put`` par entrée ; octets déjà vérifiés
           en G3 ; idempotent par clé, re-matérialisable depuis le staging).
        3. Tombstones pour chaque ``note_id`` de ``notes_consumed`` (clé
           ``note_id``, ADR-0013).
        4. ``TOMBSTONE_RECORDED`` (uniquement si ``notes_consumed`` non vide).
        5. **Flip du pointeur ``bank_version.json`` — POINT DE LINÉARISATION**,
           écrit en DERNIER parmi l'état, monotone.
        6. Watermark de progrès APPLIQUÉ local (``bank_version`` ; le curseur
           d'event est PORTÉ inchangé, jamais écrit ici — ADR-0013).
        7. ``WATERMARK_UPDATED``.
        8. ``BANK_COMMITTED`` (audit).
        9. **Convergence du token** : sur N'IMPORTE QUEL peer, si le token local
           est HELD par ``commit.committed_by_node_id`` au ``commit.term``, on le
           passe FREE (le holder a libéré DANS le commit — step 9 de §5.3 fait
           partie de « l'application complète » pour tous, pas seulement le
           holder). Idempotent/monotone : un token déjà FREE, ou tenu à un term
           plus récent, est un no-op.
        10. Cleanup best-effort du staging (post-pointeur, jamais load-bearing).
        11. Retourne le nouveau ``BankVersionPointer``.

        Pourquoi JOURNAL-FIRST puis pointeur-EN-DERNIER.

        Le bank vivant est UNVERSIONNÉ (clés mutables ``{space}/bank/{rel_path}``).
        On ne peut donc PAS rendre le flip du pointeur atomique vis-à-vis du CONTENU
        live (cela exigerait des clés bank immuables-par-version ou une indirection
        visible côté lecteur — hors scope de cette PR). On implémente la PLUS FORTE
        garantie TRACTABLE dans ce design :

        - le JOURNAL durable (``commits/{N}``) est écrit AVANT toute mutation live →
          il est la LINÉARISATION durable de « la version N existe » ;
        - la promotion live devient une MATÉRIALISATION idempotente, complétée par
          ROLL-FORWARD : à chaque frontière de promote, le journal est déjà présent
          et l'arbre stagé est intact, donc un re-run d'``apply_commit`` recharge le
          staging et re-promeut chaque entrée idempotemment (même octets vérifiés)
          avant de flipper le pointeur ;
        - le pointeur reste EN DERNIER : il ne nomme JAMAIS N tant que la promotion
          n'a pas été tentée intégralement, donc « pointeur=N ⟹ bank live = N »
          (jamais de pointeur orphelin nommant N sur un bank N-1).

        RISQUE RÉSIDUEL (documenté, borné). Entre le 1er ``put`` live et le flip du
        pointeur, le pointeur dit ENCORE N-1 alors que le bank live contient déjà
        DES (pas tous les) fichiers de N : une lecture concurrente du bank live PEUT
        observer un mélange N-1/N transitoire INCOHÉRENT avec le pointeur. Ce n'est
        PAS une perte ni un pointeur orphelin : c'est un état transitoire que tout
        re-run d'``apply_commit`` (même version, même staging) GUÉRIT par roll-
        forward jusqu'à un état cohérent pointeur+bank. La SUPPRESSION complète de
        cette fenêtre exige un bank immuable-par-version (matérialiser sous
        ``bank/{N}/...`` nommé par le pointeur, lecteurs résolvant via le pointeur) —
        redesign explicitement HORS SCOPE de cette PR. Aucun rollback compensatoire :
        la récupération est TOUJOURS un roll-forward par replay idempotent.
        """
        # Recheck at the actual durable mutation boundary.  A commit may have
        # been staged before a pairing admitted its e+1 pending target; it must
        # not roll forward into the fixed activation snapshot afterwards.
        # ``stage_commit`` carries the same gate so a fresh refused commit leaves
        # no staging residue; this second gate closes pre-staged/retry paths.
        await assert_space_not_reserved(self._space_id)
        await assert_no_pairing_activation(self._space_id)

        now = self._clock()

        # --- Roll-forward : reprise post-crash après le POINT DE LINÉARISATION ---
        # Si le pointeur vivant nomme DÉJÀ ce commit, le flip est franchi. Ré-entrer
        # G0/G2 lèverait VERSION_CONFLICT (le CAS verrait le pointeur à N alors que
        # intent.previous est N-1). On FINIT idempotemment les étapes post-pointeur
        # (tombstones absents, watermark, release token) et on retourne. Ceci
        # réalise la reprise « roll-forward » : un crash entre le flip et la fin de
        # l'apply est rejouable sans rollback ni conflit de CAS.
        # --- Cohérence du fencing_token explicite avec l'intent (fail-closed) ---
        # Le paramètre ``fencing_token`` est redondant avec ``intent.fencing_token``
        # (la source d'autorisation, ADR-0011). On le rend LOAD-BEARING : une
        # divergence est un appel mal formé -> on ferme AVANT toute mutation, plutôt
        # que d'accepter un argument silencieusement ignoré (surface trompeuse).
        if fencing_token != intent.fencing_token:
            raise CommitApplyError(
                CommitApplyReason.FENCING_TOKEN_MISMATCH,
                f"commit refused: explicit fencing_token differs from intent "
                f"(fencing_token={fencing_token}, "
                f"intent.fencing_token={intent.fencing_token})",
                {
                    "fencing_token": fencing_token,
                    "intent_fencing_token": intent.fencing_token,
                },
            )

        existing_pointer = await self._store.get_bank_version_pointer()
        if (
            existing_pointer is not None
            and existing_pointer.bank_version == commit.bank_version
            and existing_pointer.commit_id == commit.commit_id
        ):
            # Resume post-pointeur : le POINT DE LINÉARISATION est franchi, mais le
            # chemin nominal SAUTE ici G0 (autorisation) + G1/G2/G3 (manifest). Un
            # ``BankCommit`` fourni qui partage seulement (bank_version, commit_id)
            # avec le pointeur ne doit JAMAIS muter l'état post-pointeur sur la base
            # de ses propres octets. On RE-ANCRE donc sur la source de vérité
            # DURABLE : ``append_commit`` est garanti AVANT le flip du pointeur dans
            # le chemin nominal, donc ``commits/{bank_version}`` DOIT exister quand le
            # pointeur nomme ce commit. On le charge et on FERME closed si :
            #   - il est ABSENT (pointeur sans journal = état critique incohérent) ->
            #     ``CorruptedStateError`` (fail-closed, jamais réparé en silence) ;
            #   - il est corrompu                         -> ``CorruptedStateError``
            #     (propage depuis ``get_commit``) ;
            #   - il DIVERGE du commit fourni             -> ``RESUME_COMMIT_DIVERGENT``.
            # ``_finish_post_pointer`` n'est appelé QU'AVEC le record durable vérifié.
            durable = await self._store.get_commit(commit.bank_version)
            if durable is None:
                raise CorruptedStateError(
                    f"resume closed: pointer names bank_version="
                    f"{commit.bank_version} commit_id={commit.commit_id!r} but durable "
                    f"record commits/{commit.bank_version} is ABSENT "
                    f"(inconsistent critical state; never repaired silently)"
                )
            assert_durable_commit_matches(commit, durable)
            # On finit sur le record DURABLE (pas le payload fourni) : même après la
            # vérif d'égalité, c'est la source de vérité qui pilote les mutations.
            await self._finish_post_pointer(
                durable, local_node_id=local_node_id, reason=reason, now=now
            )
            return existing_pointer

        # --- Roll-forward PRÉ-pointeur (reprise d'un commit DÉJÀ AUTORISÉ) ---
        # Le pointeur est ENCORE derrière (le flip n'a pas eu lieu) MAIS le record
        # durable ``commits/{commit.bank_version}`` existe DÉJÀ. JOURNAL-FIRST garantit
        # qu'``append_commit`` ne s'écrit qu'APRÈS un G0 (assert_commit_allowed) réussi :
        # ce commit a donc DÉJÀ été autorisé quand il a été journalisé. La reprise
        # FINIT un apply déjà autorisé — ce n'est PAS un commit frais — donc elle ne
        # doit PAS re-exiger un lease VIVANT (qui peut avoir expiré entre le crash et
        # la reprise). On RE-ANCRE sur le record DURABLE (source de vérité), on vérifie
        # qu'il matche le commit fourni (sinon RESUME_COMMIT_DIVERGENT, fail-closed),
        # puis on matérialise (promote + flip + finish) idempotemment SANS G0.
        #
        # POINTEUR ABSENT = FAIL-CLOSED (jamais traité comme « en retard »). Un
        # ``bank_version.json`` ABSENT n'est PAS un pointeur en retard : c'est un état
        # critique incomplet. Le gate frais le rejetterait (BLOCKED : le CAS
        # previous==pointeur est impossible sans pointeur). Matérialiser un roll-forward
        # SANS lease sur pointeur absent serait donc FAIL-OPEN — un record durable +
        # un marqueur de stage suffiraient à muter le bank vivant ET à CRÉER un pointeur
        # sans aucune autorisation lease/term. On FERME en ``CorruptedStateError`` : la
        # reprise no-lease n'est permise QUE si un pointeur EXISTE et est STRICTEMENT
        # DERRIÈRE ``commit.bank_version``. Un pointeur perdu exige une recovery
        # EXPLICITE hors d'``apply_commit``, jamais une matérialisation silencieuse.
        #
        # GARDE MONOTONE : si le pointeur (présent) ÉGALE ou DÉPASSE cette version (re-
        # apply stale d'un commit superseded), re-promouvoir ses octets écraserait un
        # bank live plus récent et le flip violerait la garde monotone : on retourne le
        # pointeur courant SANS mutation (no-op, jamais de roll-back).
        #
        # Un commit FRAIS (pas de ``commits/{N}`` durable) tombe au travers vers G0 +
        # assert_commit_allowed : l'autorisation fraîche reste pleinement gardée (un
        # pointeur absent y est rejeté BLOCKED par le gate).
        durable_existing = await self._store.get_commit(commit.bank_version)
        if (
            durable_existing is not None
            and durable_existing.commit_id == commit.commit_id
        ):
            if existing_pointer is None:
                raise CorruptedStateError(
                    f"roll-forward closed: durable record "
                    f"commits/{commit.bank_version} (commit_id={commit.commit_id!r}) "
                    f"exists but bank_version.json is ABSENT — lost pointer, "
                    f"inconsistent critical state. Never materialized without "
                    f"authorization (fail-closed): explicit recovery is required outside apply_commit."
                )
            if existing_pointer.bank_version >= commit.bank_version:
                # Pointeur présent au-delà ou égal : commit superseded, reprise no-op
                # (monotone, jamais de roll-back).
                return existing_pointer
            # Pointeur PRÉSENT et STRICTEMENT derrière -> roll-forward autorisé SANS G0.
            assert_durable_commit_matches(commit, durable_existing)
            return await self._materialize_commit(
                durable_existing,
                local_node_id=local_node_id,
                reason=reason,
                now=now,
            )

        # --- G0 : autorisation (point UNIQUE, AVANT append_commit) ---
        # SEULEMENT pour un commit FRAIS (pas de record durable matchant ci-dessus).
        # La reprise pré-pointeur d'un commit déjà journalisé NE repasse PAS ici.
        await self._lease.assert_commit_allowed(intent)

        # --- G1 : forme interne du parent ---
        assert_parent_contiguous(commit)

        # --- G2 : accord intent/commit ---
        assert_intent_matches_commit(commit, intent)

        # --- G3 + APPLY : matérialisation atomique-par-roll-forward ---
        return await self._materialize_commit(
            commit, local_node_id=local_node_id, reason=reason, now=now
        )

    async def _materialize_commit(
        self,
        commit: BankCommit,
        *,
        local_node_id: str,
        reason: str,
        now: datetime,
    ) -> BankVersionPointer:
        """
        G3 (intégrité du stage) + étapes d'APPLY 1-5 (journal-first, promote,
        tombstones, flip du pointeur) + ``_finish_post_pointer``. Appelé À LA FOIS
        par le chemin nominal (après G0/G1/G2 sur un commit frais) ET par la reprise
        roll-forward PRÉ-pointeur (sur le record DURABLE, SANS G0 — l'autorisation a
        déjà eu lieu quand le commit a été journalisé). N'EFFECTUE AUCUNE
        autorisation : c'est volontaire (la reprise ne doit pas re-exiger un lease).

        Toutes les étapes sont idempotentes-par-clé : ``append_commit`` est no-op si
        le record existe déjà (cas roll-forward), le promote re-met les MÊMES octets,
        le flip est monotone, ``_finish_post_pointer`` comble les étapes manquantes.
        """
        # --- G3 : load + intégrité du manifest (fail closed) ---
        # G3a — MARQUEUR DE PUBLICATION : le MANIFEST.json stagé (écrit EN DERNIER
        # par stage_commit, ADR-0007) DOIT exister et matcher le commit. Sans lui, le
        # stage n'est pas prouvé publié : un arbre PARTIELLEMENT stagé (crash avant le
        # manifest-last) ferait apply-OPEN. Absent -> STAGING_MANIFEST_MISSING ;
        # divergent -> STAGING_MANIFEST_DIVERGENT ; JSON corrompu -> CorruptedStateError.
        staged_manifest = await self.load_staging_manifest(commit.commit_id)
        assert_staging_manifest_matches(commit, staged_manifest)
        # On LIST aussi les objets RÉELLEMENT stagés (pas seulement ceux du
        # manifest) : un fichier stagé hors manifest, invisible de load_staged,
        # ferme alors le commit en PARTIAL_STAGE au lieu d'être ignoré/abandonné.
        staged = await self.load_staged(commit.commit_id, commit.manifest)
        actual_staged_paths = await self.list_staged_bank_paths(commit.commit_id)
        verify_manifest_against_staged(
            commit, staged, actual_staged_paths=actual_staged_paths
        )
        assert_no_graph_memory_in_manifest(commit, staged)

        # === APPLY (toutes les gardes ont passé) ===

        # 1. Journal des commits — JOURNAL-FIRST, AVANT toute mutation du bank
        #    vivant (idempotent par commit_id ; ne fence pas — G0 l'a fait sur le
        #    chemin nominal, ou a déjà eu lieu au moment de la 1ʳᵉ journalisation sur
        #    le chemin roll-forward).
        #
        #    Le journal ``commits/{bank_version}`` est la SOURCE DE VÉRITÉ durable :
        #    il porte le manifest complet (path + sha256 + size) et nomme l'arbre
        #    stagé (``commit_id``), donc il est SUFFISANT pour RE-MATÉRIALISER le bank
        #    vivant par roll-forward. En l'écrivant AVANT le promote, un crash en
        #    plein milieu du promote (un put live réussi, le suivant échoué) laisse :
        #      - le journal présent à N (récupérable) ;
        #      - le pointeur encore à N-1 (le flip est en étape 5) ;
        #      - l'arbre stagé intact (cleanup post-pointeur) ;
        #    => un re-run d'``apply_commit`` (pointeur=N-1, record durable matchant ->
        #    chemin roll-forward pré-pointeur, SANS lease) RE-CHARGE le staging et
        #    RE-PROMEUT idempotemment chaque entrée, puis flippe le pointeur. La
        #    matérialisation du bank vivant est donc une étape ROLL-FORWARD-complétée
        #    et idempotente-par-clé, jamais un point de non-retour.
        await self._store.append_commit(commit)

        # 2. Promote staged -> bank vivant (octets déjà vérifiés en G3 ; put
        #    idempotent par clé, re-matérialisable depuis le staging tant que le
        #    pointeur ne nomme pas encore N). C'est la seule étape qui touche le bank
        #    vivant UNVERSIONNÉ : voir la note de RISQUE RÉSIDUEL dans la docstring
        #    (fenêtre crash mid-promote où pointeur=N-1 mais bank partiellement N).
        for e in commit.manifest:
            await self._storage.put(
                self._bank_live_key(e.path),
                staged[e.path],
                content_type=self._content_type_for(e.path),
            )

        # 3. Tombstones (clé note_id — ADR-0013, jamais de second origin_note_id).
        for note_id in commit.notes_consumed:
            await self._store.add_tombstone(
                Tombstone(
                    note_id=note_id,
                    deleted_by_node_id=commit.committed_by_node_id,
                    term=commit.term,
                    membership_epoch=commit.membership_epoch,
                    bank_version=commit.bank_version,
                    event_id=commit.event_id,
                    request_id=commit.request_id,
                    reason=reason,
                )
            )
            # P5-7 cross-seam (#15 -> #16) : reap la copie live du note tombstoné
            # pour que ``assert_no_tombstone_resurrection`` reste vert. APRÈS le
            # marqueur tombstone (autoritaire), AVANT le flip du pointeur (point de
            # linéarisation) : un crash entre les deux laisse le tombstone et le
            # roll-forward suivant re-reap. ``reap_on_tombstone`` est
            # delete-of-absent-is-no-op (idempotent) : writer-side (copie déjà
            # supprimée) c'est un no-op ; peer-side il retire la copie survivante +
            # le sidecar. Dépendance optionnelle -> les commits P5-6 sans reaper
            # sont inchangés.
            if self._note_replication is not None:
                await self._note_replication.reap_on_tombstone(note_id)

        # 4. TOMBSTONE_RECORDED (uniquement si des notes ont été consommées).
        if commit.notes_consumed:
            await self._store.append_event(
                EventEnvelope(
                    # Id de dédup synthétique dérivé du commit_id (TOUJOURS présent
                    # + unique + validé no-slash), PAS de commit.event_id qui peut
                    # défauter "" et faire collisionner deux commits dans le journal.
                    event_id=f"{commit.commit_id}:tombstone",
                    type=EventType.TOMBSTONE_RECORDED,
                    origin_node_id=local_node_id,
                    term=commit.term,
                    membership_epoch=commit.membership_epoch,
                    bank_version=commit.bank_version,
                    created_at=now.isoformat(),
                    payload={
                        "note_ids": list(commit.notes_consumed),
                        "bank_version": commit.bank_version,
                    },
                )
            )

        # 5. POINT DE LINÉARISATION : flip du pointeur (monotone, en dernier).
        pointer = await self._store.set_bank_version_pointer(
            BankVersionPointer(
                bank_version=commit.bank_version,
                commit_id=commit.commit_id,
                updated_at=now.isoformat(),
            )
        )

        # 6-10. Étapes POST-pointeur (watermark, events, convergence token,
        #       cleanup). Mêmes étapes que la reprise roll-forward post-pointeur,
        #       toutes idempotentes-par-clé.
        await self._finish_post_pointer(
            commit, local_node_id=local_node_id, reason=reason, now=now
        )

        # 11.
        return pointer

    async def _finish_post_pointer(
        self,
        commit: BankCommit,
        *,
        local_node_id: str,
        reason: str,
        now: datetime,
    ) -> None:
        """
        Finit les étapes POST-pointeur d'un apply, en aval du POINT DE
        LINÉARISATION (flip de ``bank_version.json``). Appelé À LA FOIS par le
        chemin nominal ET par la reprise roll-forward (réentrée après crash
        post-flip). Toutes les étapes sont idempotentes-par-clé : un re-run ne
        duplique rien et n'écrit rien de non-monotone.

        Étapes : tombstones absents (clé ``note_id``), ``TOMBSTONE_RECORDED``,
        watermark de progrès appliqué (curseur d'event PORTÉ inchangé, ADR-0013),
        ``WATERMARK_UPDATED``, ``BANK_COMMITTED``, convergence du token (step 9 de
        §5.3), cleanup best-effort du staging.

        Note : sur la reprise roll-forward, les tombstones du commit ont pu déjà
        être écrits avant le crash. ``add_tombstone`` est idempotent par clé
        ``note_id`` (même octets), donc on les ré-écrit sans danger ; le re-run
        comble simplement ceux qui manqueraient (crash entre flip et tombstones
        n'arrive pas — ils précèdent le flip — mais on reste défensif et
        idempotent).
        """
        # 3'. Tombstones (idempotent par note_id ; comble un éventuel manquant).
        for note_id in commit.notes_consumed:
            if await self._store.get_tombstone(note_id) is None:
                await self._store.add_tombstone(
                    Tombstone(
                        note_id=note_id,
                        deleted_by_node_id=commit.committed_by_node_id,
                        term=commit.term,
                        membership_epoch=commit.membership_epoch,
                        bank_version=commit.bank_version,
                        event_id=commit.event_id,
                        request_id=commit.request_id,
                        reason=reason,
                    )
                )

        # 4'. TOMBSTONE_RECORDED (uniquement si des notes ont été consommées).
        if commit.notes_consumed:
            await self._store.append_event(
                EventEnvelope(
                    event_id=f"{commit.commit_id}:tombstone",
                    type=EventType.TOMBSTONE_RECORDED,
                    origin_node_id=local_node_id,
                    term=commit.term,
                    membership_epoch=commit.membership_epoch,
                    bank_version=commit.bank_version,
                    created_at=now.isoformat(),
                    payload={
                        "note_ids": list(commit.notes_consumed),
                        "bank_version": commit.bank_version,
                    },
                )
            )

        # 6. Watermark de progrès APPLIQUÉ local. Le curseur d'event est PORTÉ
        #    inchangé (jamais écrit ici — ADR-0013, deux watermarks distincts).
        prev = await self._store.get_watermark(local_node_id)
        await self._store.set_watermark(
            Watermark(
                node_id=local_node_id,
                bank_version=commit.bank_version,
                last_event_id=prev.last_event_id if prev else "",
                last_event_ts=prev.last_event_ts if prev else "",
                term=commit.term,
                membership_epoch=commit.membership_epoch,
                event_id=commit.event_id,
                request_id=commit.request_id,
                updated_at=now.isoformat(),
            )
        )

        # 7. WATERMARK_UPDATED.
        await self._store.append_event(
            EventEnvelope(
                event_id=f"{commit.commit_id}:watermark:{local_node_id}",
                type=EventType.WATERMARK_UPDATED,
                origin_node_id=local_node_id,
                term=commit.term,
                membership_epoch=commit.membership_epoch,
                bank_version=commit.bank_version,
                created_at=now.isoformat(),
                payload={
                    "node_id": local_node_id,
                    "bank_version": commit.bank_version,
                },
            )
        )

        # 8. BANK_COMMITTED (audit).
        await self._store.append_event(
            EventEnvelope(
                event_id=f"{commit.commit_id}:committed",
                type=EventType.BANK_COMMITTED,
                origin_node_id=local_node_id,
                term=commit.term,
                membership_epoch=commit.membership_epoch,
                bank_version=commit.bank_version,
                created_at=now.isoformat(),
                payload={
                    "commit_id": commit.commit_id,
                    "bank_version": commit.bank_version,
                },
            )
        )

        # 9. Convergence du token (TOUS les peers, pas seulement le holder).
        await self._converge_token_release(commit)

        # 10. Cleanup best-effort du staging (post-pointeur, jamais load-bearing).
        await self._cleanup_staging(commit)

    async def _converge_token_release(self, commit: BankCommit) -> None:
        """
        Fait CONVERGER l'état local du token vers FREE après l'apply d'un commit.

        Step 9 de HIVEMIND.md §5.3 — « release du token » — fait partie de
        « l'application complète » pour CHAQUE peer, pas seulement le holder : le
        holder a libéré le token EN PUBLIANT le commit, donc tout peer qui applique
        ce ``BANK_COMMIT`` doit refléter ce FREE dans son ``token.json`` local.
        Sinon le pair laisse son token HELD par le holder distant (divergence) et
        bloque le prochain ``acquire`` jusqu'à l'expiration de lease.

        Garde de convergence (fail-safe, monotone) : on ne libère QUE si le token
        local est HELD par ``commit.committed_by_node_id`` au ``commit.term`` même.
        Cela évite d'écraser :

        - un token déjà FREE (no-op) ;
        - un token re-acquis depuis (term plus récent, autre holder) — on ne
          dégrade jamais un grant postérieur.

        Le release lui-même (``LeaseRuntime.release``) compare le holder STOCKÉ à
        l'argument et préserve term/fencing : appelé avec
        ``holder_node_id=commit.committed_by_node_id``, il passe la garde
        ``NOT_HOLDER`` sur tout peer (le holder stocké EST bien
        ``committed_by_node_id``). Idempotent : re-converger un FREE est un no-op.
        """
        token = await self._store.get_token()
        if (
            token is not None
            and token.state == TokenState.HELD.value
            and token.holder_node_id == commit.committed_by_node_id
            and token.term == commit.term
        ):
            await self._lease.release(holder_node_id=commit.committed_by_node_id)

    def _bank_live_key(self, rel_path: str) -> str:
        """Clé S3 d'un fichier bank VIVANT (relatif à ``{space}/bank/``)."""
        return f"{self._space_id}/bank/{rel_path}"

    async def _cleanup_staging(self, commit: BankCommit) -> None:
        """Supprime les objets stagés du commit (best-effort, per-key : le
        ``FakeStorage`` partagé n'a pas de ``delete_many``). Jamais load-bearing :
        un crash avant le cleanup laisse un arbre réclamable, jamais lu comme
        autoritaire sans manifest complet + re-vérification."""
        for e in commit.manifest:
            await self._storage.delete(
                layout.staging_bank_key(self._space_id, commit.commit_id, e.path)
            )
        await self._storage.delete(
            layout.staging_manifest_key(self._space_id, commit.commit_id)
        )

    # ─────────────────────────────────────────────────────────────────
    # min cross-peer + GC des tombstones (séparé de l'apply)
    # ─────────────────────────────────────────────────────────────────

    async def cross_peer_min_watermark(self) -> int:
        """MIN cross-peer du watermark de progrès appliqué (``bank_version``)."""
        return min_applied_bank_version(await self._store.list_watermarks())

    async def gc_tombstones(self, *, expected_node_ids: set[str]) -> int:
        """
        GC des tombstones SOUS le MIN cross-peer, all-ACK strict (pas de quorum).

        Garde : si un peer ATTENDU n'a pas (encore) reporté de watermark, on
        BLOQUE (retourne 0) — un peer en retard/absent retient le tombstone.
        ``expected_node_ids`` est passé par le caller (découplé de lifecycle ;
        ``commit_runtime`` ne hard-wire pas la membership).

        ``garbage_collect_tombstones`` supprime les ``0 <= bv < floor`` STRICTS et
        garde les ``bv == -1`` (state.py). Clé sur ``bank_version``, JAMAIS le
        curseur d'event (ADR-0013).
        """
        wms = await self._store.list_watermarks()
        if not expected_node_ids.issubset({w.node_id for w in wms}):
            return 0
        return await self._store.garbage_collect_tombstones(
            min_applied_bank_version(wms)
        )
