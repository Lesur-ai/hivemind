# -*- coding: utf-8 -*-
"""
Service Garbage Collector — Nettoyage des notes orphelines.

Les notes live sont normalement consolidées puis supprimées par l'agent.
Si un agent disparaît sans consolider, ses notes restent indéfiniment.

Le GC :
1. Identifie les notes plus vieilles qu'un seuil (défaut 7 jours)
2. En exécution confirmée : CONSOLIDE les vieilles notes dans la bank (via LLM)
   → ajoute une note "GC notice" pour tracer la consolidation forcée
3. Optionnel : supprime les notes sans consolider (delete_only=True)

L'outil public ``admin_gc_notes`` reste un dry-run par défaut.

Architecture :
    tools/admin.py → GCService (ce fichier) → ConsolidatorService + StorageService
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import uuid
from collections.abc import Iterable
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .storage import get_storage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .write_sink import DirectLocalWriteSink

logger = logging.getLogger("live_mem.gc")


_ELIGIBLE_SET_TOKEN_PREFIX = "gc-set-v1:"
_ELIGIBLE_SET_TOKEN_RE = re.compile(r"^gc-set-v1:[0-9a-f]{64}$")
_LIVE_NOTE_FILENAME_RE = re.compile(
    r"^(?P<timestamp>\d{8}T\d{6})_"
    r"(?P<agent>[A-Za-z0-9_-]+)_"
    r"(?P<category>observation|decision|todo|insight|question|progress|issue)_"
    r"(?P<uuid>[0-9a-fA-F]{8})\.md$"
)


def _eligible_keys(scan: dict) -> list[str]:
    """Return the exact, de-duplicated eligible key set from a GC scan."""
    return sorted(
        {
            key
            for space_data in scan.get("spaces", {}).values()
            for key in space_data.get("keys", [])
            if isinstance(key, str)
        }
    )


def _eligible_set_token(
    *,
    space_id: str,
    max_age_days: int,
    keys: Iterable[str],
) -> str:
    """Build the opaque identity token for one reviewed eligible-key set.

    The wall-clock cutoff is deliberately excluded: two scans that select the
    same keys for the same scope and threshold must produce the same token.
    Fully-qualified keys make equal-cardinality substitutions observable while
    the digest keeps those keys out of the public response.
    """
    payload = json.dumps(
        {
            "keys": sorted(set(keys)),
            "max_age_days": max_age_days,
            "space_id": space_id,
            "version": 1,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _ELIGIBLE_SET_TOKEN_PREFIX + hashlib.sha256(payload).hexdigest()


# LM2-10 fix : helper interne pour écrire une note "GC notice" en imitant
# l'identité de l'agent orphelin. live.write_note() auto-détecte le caller
# depuis le token courant (v0.8.1 : Token = Agent) et ne permet plus de
# choisir un agent libre — c'est volontaire pour la sécurité, mais cela
# casse le pattern du GC qui doit attacher la notice à l'agent disparu
# (sinon la consolidation forcée par agent ne les inclura pas).
#
# Solution : écrire le fichier directement sur S3 avec le format standard
# d'une note live (front-matter YAML + corps Markdown), en utilisant le
# nom d'agent passé. Cet appel est protégé par le pipeline (GC = admin only).
async def _write_gc_notice(
    space_id: str,
    agent_name: str,
    content: str,
    *,
    sink: DirectLocalWriteSink,
) -> str:
    """
    Écrit directement une note ``observation`` au nom d'un agent donné.

    Bypass volontaire de ``live.write_note()`` pour conserver l'identité
    de l'agent orphelin (cf. v0.8.1 — Token = Agent).

    Returns:
        La clé S3 pleinement qualifiée créée.
    """
    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%Y%m%dT%H%M%S")
    uuid8 = uuid.uuid4().hex[:8]
    safe_agent = re.sub(r"[^a-zA-Z0-9_-]", "", agent_name) or "agent"
    filename = f"{timestamp_str}_{safe_agent}_observation_{uuid8}.md"

    front_matter = (
        "---\n"
        f'timestamp: "{now.isoformat()}"\n'
        f'agent: "{agent_name}"\n'
        'category: "observation"\n'
        f"tags: {json.dumps(['gc', 'forced-consolidation'])}\n"
        f'space_id: "{space_id}"\n'
        "---\n\n"
    )
    key = f"{space_id}/live/{filename}"
    await sink.put(key, front_matter + content)
    await sink.commit(reason="admin_gc_notes_notice")
    return key


class GCService:
    """
    Service de Garbage Collection des notes live.

    Identifie les notes orphelines et les consolide (ou supprime).
    """

    @staticmethod
    def _route_refusal_reason(error: Exception) -> str | None:
        """Map known fail-closed route errors to stable result reasons.

        Initial preflight errors still propagate with their exact exception
        types.  This mapping is only used after processing has started, where
        an honest partial result must identify why later work stopped.
        """
        from .engines import RegistryRefused
        from .hivemind import CorruptedStateError
        from .write_sink import StagedWriteNotImplemented

        if isinstance(error, StagedWriteNotImplemented):
            return "route_staged_not_implemented"
        if isinstance(error, CorruptedStateError):
            return "state_corrupt"
        if isinstance(error, RegistryRefused):
            return "route_refused"
        return None

    async def _resolve_direct_local_sinks(
        self,
        scan: dict,
        *,
        op: str,
    ) -> dict[str, DirectLocalWriteSink]:
        """Preflight every candidate route before the first durable mutation.

        A global GC must never mutate an earlier direct-local space and only
        then discover that a later space is shared/unsafe/corrupt.  Resolve the
        complete candidate set first, in stable order, and return the proven
        sinks to the immediate mutation step. Healthy shared spaces receive
        the same typed staged-not-implemented refusal as ``bank_consolidate``;
        unsafe/resync and corrupt state propagate their existing typed registry
        errors.
        """
        from .engines import get_engine_registry
        from .write_sink import DirectLocalWriteSink, StagedWriteNotImplemented

        registry = get_engine_registry()
        sinks: dict[str, DirectLocalWriteSink] = {}
        for sid in sorted(scan.get("spaces", {})):
            sink = await registry.resolve_sink(sid)
            if not isinstance(sink, DirectLocalWriteSink):
                raise StagedWriteNotImplemented(op=op, key=f"{sid}/live/")
            sinks[sid] = sink
        return sinks

    async def _cleanup_gc_notice(
        self,
        *,
        space_id: str,
        notice_key: str,
        space_data: dict,
    ) -> tuple[bool, str | None]:
        """Best-effort removal of an unconsumed synthetic notice.

        Cleanup is itself a durable mutation, so it receives a fresh route
        proof and uses the proven direct-local sink.  Errors are returned to
        the caller as detail instead of erasing prior consolidation counts.
        """
        try:
            if not await get_storage().exists(notice_key):
                return False, None
            sinks = await self._resolve_direct_local_sinks(
                {"spaces": {space_id: space_data}}, op="delete"
            )
            sink = sinks[space_id]
            await sink.delete(notice_key)
            await sink.commit(reason="admin_gc_notes_notice_cleanup")
            return True, None
        except Exception as error:
            logger.exception("GC notice cleanup failed for %s", notice_key)
            return (
                False,
                self._route_refusal_reason(error) or "gc_notice_cleanup_failed",
            )

    async def scan_old_notes(
        self,
        space_id: str = "",
        max_age_days: int = 7,
    ) -> dict:
        """
        Scanne les notes orphelines dans un ou tous les espaces.

        Args:
            space_id: Espace cible (vide = tous les espaces)
            max_age_days: Seuil en jours (défaut 7)

        Returns:
            Rapport avec nombre de notes par espace et par agent
        """
        storage = get_storage()
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        cutoff_str = cutoff.strftime("%Y%m%dT%H%M%S")

        # Déterminer les espaces à scanner
        if space_id:
            space_ids = [space_id]
        else:
            prefixes = await storage.list_prefixes("")
            space_ids = [p.rstrip("/") for p in prefixes if not p.startswith("_")]

        result = {
            "status": "ok",
            "max_age_days": max_age_days,
            "cutoff_date": cutoff.isoformat(),
            "spaces": {},
            "total_old_notes": 0,
            "total_old_size": 0,
        }

        for sid in space_ids:
            if not await storage.exists(f"{sid}/_meta.json"):
                continue

            objects = await storage.list_objects(f"{sid}/live/")
            old_notes = []
            by_agent = {}
            oldest = None
            canonical_notes = 0

            for note_obj in objects:
                key = note_obj["Key"]
                live_prefix = f"{sid}/live/"
                if not key.startswith(live_prefix):
                    continue
                filename = key[len(live_prefix) :]
                # Destructive GC only accepts canonical direct children.  A
                # nested Markdown object or a malformed legacy filename is not
                # silently reclassified as a live note eligible for deletion.
                parsed = _parse_live_note_filename(filename)
                if "/" in filename or parsed is None:
                    continue
                ts, agent = parsed
                canonical_notes += 1

                if ts < cutoff_str:
                    old_notes.append(
                        {
                            "key": key,
                            "size": note_obj.get("Size", 0),
                            "timestamp": ts,
                        }
                    )
                    by_agent[agent] = by_agent.get(agent, 0) + 1
                    if oldest is None or ts < oldest:
                        oldest = ts

            if old_notes:
                total_size = sum(n["size"] for n in old_notes)
                result["spaces"][sid] = {
                    "total_notes": canonical_notes,
                    "old_notes": len(old_notes),
                    "old_notes_size": total_size,
                    "by_agent": by_agent,
                    "oldest": oldest,
                    "keys": [n["key"] for n in old_notes],
                }
                result["total_old_notes"] += len(old_notes)
                result["total_old_size"] += total_size

        result["eligible_set_token"] = _eligible_set_token(
            space_id=space_id,
            max_age_days=max_age_days,
            keys=_eligible_keys(result),
        )
        return result

    async def consolidate_old_notes(
        self,
        space_id: str = "",
        max_age_days: int = 7,
    ) -> dict:
        """
        Consolide les notes orphelines dans la Memory Bank via LLM.

        Pour chaque agent ayant des notes orphelines :
        1. Écrit une note "GC notice" avec le nom de l'agent
           (pour que le LLM sache que c'est une consolidation forcée)
        2. Appelle le consolidateur pour cet agent
        3. Les notes sont intégrées dans la bank et supprimées

        Args:
            space_id: Espace cible (vide = tous les espaces)
            max_age_days: Seuil en jours (défaut 7)

        Returns:
            Rapport de consolidation par espace et par agent
        """
        from .consolidator import get_consolidator
        from .locks import get_lock_manager

        # Scanner d'abord
        scan = await self.scan_old_notes(space_id, max_age_days)

        if scan["total_old_notes"] == 0:
            scan["action"] = "consolidate"
            scan["consolidated"] = 0
            scan["consolidation_requested"] = 0
            scan["consolidation_failed"] = 0
            scan["consolidation_details"] = {}
            scan.pop("eligible_set_token", None)
            scan["message"] = "Aucune note orpheline à consolider"
            return scan

        # Route-first, all-space preflight.  This completes before any notice,
        # consolidation, bank write, or live-note deletion can occur.
        await self._resolve_direct_local_sinks(scan, op="consolidate")

        consolidator = get_consolidator()
        total_consolidated = 0
        total_requested = scan["total_old_notes"]
        had_incomplete_agent = False
        consolidation_results = {}

        for sid in sorted(scan["spaces"]):
            space_data = scan["spaces"][sid]
            consolidation_results[sid] = {}
            agents = list(space_data["by_agent"].items())

            # Hold one per-space lock across every agent.  This prevents delete
            # or another consolidation from interleaving between agent batches.
            lock = get_lock_manager().consolidation(sid)
            if lock.locked():
                had_incomplete_agent = True
                for agent_name, note_count in agents:
                    consolidation_results[sid][agent_name] = {
                        "status": "skipped",
                        "reason": "consolidation_busy",
                        "notes_processed": 0,
                        "notes_requested": note_count,
                    }
                continue

            async with lock:
                for agent_name, note_count in agents:
                    selected_old_keys = [
                        key
                        for key in space_data.get("keys", [])
                        if _extract_agent(key.rsplit("/", 1)[-1]) == agent_name
                    ]
                    # The decision set is frozen.  If another actor removed a
                    # selected key before this lock was obtained, fail this
                    # agent before writing a notice or invoking the LLM.
                    try:
                        selected_keys_still_exist = all(
                            [
                                await get_storage().exists(key)
                                for key in selected_old_keys
                            ]
                        )
                    except Exception:
                        # A previous agent in this space may already have been
                        # integrated and deleted.  Preserve those exact counts
                        # instead of allowing a later read failure to replace
                        # the whole response with a false zero-work error.
                        had_incomplete_agent = True
                        logger.exception(
                            "GC selected-note recheck failed for '%s' in '%s'",
                            agent_name,
                            sid,
                        )
                        consolidation_results[sid][agent_name] = {
                            "status": "error",
                            "reason": "selected_note_recheck_failed",
                            "notes_processed": 0,
                            "notes_requested": note_count,
                        }
                        continue
                    if not selected_keys_still_exist:
                        had_incomplete_agent = True
                        consolidation_results[sid][agent_name] = {
                            "status": "conflict",
                            "reason": "selected_note_set_changed",
                            "notes_processed": 0,
                            "notes_requested": note_count,
                        }
                        continue

                    gc_notice = (
                        f"⚠️ GARBAGE COLLECTOR — Consolidation forcée\n\n"
                        f"Le Garbage Collector a détecté {note_count} notes "
                        f"orphelines de l'agent '{agent_name}' (> {max_age_days} jours).\n"
                        f"Ces notes n'ont jamais été consolidées par l'agent.\n"
                        f"Le GC force leur intégration dans la Memory Bank.\n\n"
                        f"**Attention** : cette consolidation est automatique. "
                        f"Les notes intégrées peuvent manquer de contexte "
                        f"car l'agent n'est plus actif."
                    )

                    # Re-resolve while serialized and immediately before EACH
                    # notice.  A space can change route after a previous agent
                    # completed; never reuse that agent's proven local sink for
                    # a later durable write.
                    try:
                        notice_sinks = await self._resolve_direct_local_sinks(
                            {"spaces": {sid: space_data}}, op="consolidate"
                        )
                    except Exception as e:
                        reason = self._route_refusal_reason(e)
                        had_incomplete_agent = True
                        consolidation_results[sid][agent_name] = {
                            "status": "error",
                            "reason": reason or "route_recheck_failed",
                            "notes_processed": 0,
                            "notes_requested": note_count,
                        }
                        continue

                    try:
                        notice_key = await _write_gc_notice(
                            space_id=sid,
                            agent_name=agent_name,
                            content=gc_notice,
                            sink=notice_sinks[sid],
                        )
                    except Exception as e:
                        had_incomplete_agent = True
                        logger.exception(
                            "GC: échec écriture notice pour '%s' dans '%s': %s",
                            agent_name,
                            sid,
                            e,
                        )
                        consolidation_results[sid][agent_name] = {
                            "status": "error",
                            "reason": self._route_refusal_reason(e)
                            or "gc_notice_failed",
                            "notes_processed": 0,
                            "notes_requested": note_count,
                            "notice_written": False,
                            "notice_processed": False,
                        }
                        continue

                    # The exact-key consolidator preserves caller order. Put
                    # the synthetic notice first so the max-notes cap cannot
                    # strand it as a fresh, unprocessed live note.
                    selected_keys = [notice_key, *sorted(selected_old_keys)]

                    try:
                        # A final route proof immediately precedes the raw
                        # consolidator, whose writes cannot be intercepted later.
                        await self._resolve_direct_local_sinks(
                            {"spaces": {sid: space_data}}, op="consolidate"
                        )
                        r = await consolidator.consolidate(
                            sid,
                            agent=agent_name,
                            enforce_cooldown=False,
                            note_keys=selected_keys,
                        )
                    except Exception as e:
                        had_incomplete_agent = True
                        reason = self._route_refusal_reason(e)
                        logger.exception(
                            "GC consolidation failed for agent '%s' in '%s'",
                            agent_name,
                            sid,
                        )
                        notice_cleaned, cleanup_reason = await self._cleanup_gc_notice(
                            space_id=sid,
                            notice_key=notice_key,
                            space_data=space_data,
                        )
                        detail = {
                            "status": "error",
                            "reason": reason or "consolidation_failed",
                            "notes_processed": 0,
                            "notes_requested": note_count,
                            "notice_written": True,
                            "notice_processed": False,
                            "notice_cleaned": notice_cleaned,
                        }
                        if cleanup_reason is not None:
                            detail["notice_cleanup_reason"] = cleanup_reason
                        consolidation_results[sid][agent_name] = detail
                        continue

                    raw_processed = r.get("notes_processed", 0)
                    processed_count = (
                        max(0, min(len(selected_keys), raw_processed))
                        if isinstance(raw_processed, int)
                        else 0
                    )
                    processed_keys = set(selected_keys[:processed_count])
                    old_notes_processed = sum(
                        key in processed_keys for key in selected_old_keys
                    )
                    agent_status = r.get("status", "error")
                    agent_reason = r.get("reason")
                    if agent_status == "ok" and old_notes_processed < note_count:
                        agent_status = "partial"
                        agent_reason = "partial_consolidation"
                    if agent_status != "ok":
                        had_incomplete_agent = True

                    notice_cleaned = False
                    cleanup_reason = None
                    if agent_status != "ok":
                        notice_cleaned, cleanup_reason = await self._cleanup_gc_notice(
                            space_id=sid,
                            notice_key=notice_key,
                            space_data=space_data,
                        )

                    detail = {
                        "status": agent_status,
                        "notes_processed": old_notes_processed,
                        "notes_requested": note_count,
                        "notice_written": notice_key is not None,
                        "notice_processed": notice_key in processed_keys,
                        "notice_cleaned": notice_cleaned,
                        "bank_files_created": r.get("bank_files_created", 0),
                        "bank_files_updated": r.get("bank_files_updated", 0),
                    }
                    if agent_reason is not None:
                        detail["reason"] = agent_reason
                    if cleanup_reason is not None:
                        detail["notice_cleanup_reason"] = cleanup_reason
                    if isinstance(r.get("message"), str):
                        detail["message"] = r["message"]
                    consolidation_results[sid][agent_name] = detail
                    total_consolidated += old_notes_processed

                    logger.info(
                        "GC: consolidated %d notes from '%s' in '%s'",
                        old_notes_processed,
                        agent_name,
                        sid,
                    )

        # Nettoyer les clés du résultat
        for sid in scan.get("spaces", {}):
            if "keys" in scan["spaces"][sid]:
                del scan["spaces"][sid]["keys"]

        scan["action"] = "consolidate"
        scan["consolidated"] = total_consolidated
        scan["consolidation_requested"] = total_requested
        scan["consolidation_failed"] = total_requested - total_consolidated
        scan["consolidation_details"] = consolidation_results
        scan.pop("eligible_set_token", None)
        if had_incomplete_agent or total_consolidated != total_requested:
            scan["status"] = "partial"
            scan["reason"] = "partial_consolidation"
            scan["message"] = (
                f"GC partiel : {total_consolidated}/{total_requested} notes "
                "orphelines consolidées. Une partie du traitement ou du "
                "nettoyage reste incomplète ; consultez le détail par agent."
            )
        else:
            scan["status"] = "ok"
            scan["message"] = (
                f"GC : {total_consolidated} notes orphelines consolidées "
                f"dans {len(scan['spaces'])} espace(s)"
            )
        return scan

    async def delete_old_notes(
        self,
        space_id: str = "",
        max_age_days: int = 7,
        expected_eligible_set_token: str = "",
    ) -> dict:
        """
        Supprime les notes orphelines SANS consolider (perte de données).

        ⚠️ Utiliser consolidate_old_notes() de préférence.

        Args:
            space_id: Espace cible (vide = tous les espaces)
            max_age_days: Seuil en jours (défaut 7)
            expected_eligible_set_token: Identité opaque de l'ensemble exact
                renvoyée par le dry-run préalable.

        Returns:
            Nombre de notes supprimées
        """
        if not _ELIGIBLE_SET_TOKEN_RE.fullmatch(expected_eligible_set_token or ""):
            return {
                "status": "error",
                "reason": "eligible_set_token_required",
                "action": "delete",
                "deleted": 0,
                "message": (
                    "Suppression refusée : expected_eligible_set_token valide requis "
                    "depuis un dry-run admin_gc_notes préalable."
                ),
            }

        # Discovery is read-only and supplies the deterministic lock set.  The
        # authoritative delete-time scan happens only AFTER every candidate
        # consolidation lock is held, closing scan/compare/delete races with a
        # concurrent consolidator or another GC writer.  A newly appearing
        # candidate after discovery makes the locked fresh scan differ and is
        # refused (never deleted unreviewed).
        discovery = await self.scan_old_notes(space_id, max_age_days)
        if not hmac.compare_digest(
            expected_eligible_set_token,
            discovery.get("eligible_set_token", ""),
        ):
            return {
                "status": "conflict",
                "reason": "eligible_set_changed",
                "action": "delete",
                "deleted": 0,
                "message": (
                    "Suppression refusée : l'ensemble exact des notes éligibles "
                    "a changé depuis le dry-run. Relancez le dry-run puis confirmez "
                    "le nouvel ensemble."
                ),
            }

        # First all-space route proof precedes lock acquisition.  A second proof
        # runs under all locks immediately before deletion, so both an initially
        # unsafe route and a route drift while waiting fail before the first key.
        if discovery["total_old_notes"]:
            await self._resolve_direct_local_sinks(discovery, op="delete_many")
        candidate_sids = sorted(discovery.get("spaces", {}))
        from .locks import get_lock_manager

        locks = [get_lock_manager().consolidation(sid) for sid in candidate_sids]
        if any(lock.locked() for lock in locks):
            return {
                "status": "conflict",
                "reason": "consolidation_in_progress",
                "action": "delete",
                "deleted": 0,
                "message": (
                    "Suppression refusée : une consolidation est en cours sur "
                    "au moins un espace candidat. Relancez le dry-run après sa fin."
                ),
            }

        async with AsyncExitStack() as lock_stack:
            for lock in locks:
                await lock_stack.enter_async_context(lock)

            scan = await self.scan_old_notes(space_id, max_age_days)
            actual_token = scan.get("eligible_set_token", "")
            locked_sids = set(candidate_sids)
            if (
                not set(scan.get("spaces", {})).issubset(locked_sids)
                or not hmac.compare_digest(expected_eligible_set_token, actual_token)
            ):
                return {
                    "status": "conflict",
                    "reason": "eligible_set_changed",
                    "action": "delete",
                    "deleted": 0,
                    "message": (
                        "Suppression refusée : l'ensemble exact des notes éligibles "
                        "a changé depuis le dry-run. Relancez le dry-run puis confirmez "
                        "le nouvel ensemble."
                    ),
                }

            if scan["total_old_notes"] == 0:
                scan["action"] = "delete"
                scan["deleted"] = 0
                scan["delete_requested"] = 0
                scan["delete_failed"] = 0
                scan["status"] = "deleted"
                scan.pop("eligible_set_token", None)
                scan["message"] = "Aucune note orpheline à supprimer"
                return scan

            # Resolve every candidate while the serialization locks are held
            # and before deleting the first key.  Reuse the proven sinks rather
            # than falling back to raw storage.
            await self._resolve_direct_local_sinks(scan, op="delete_many")
            requested = sum(
                len(set(space_data.get("keys", [])))
                for space_data in scan["spaces"].values()
            )
            deleted = 0
            route_failure_reason = None
            for sid in sorted(scan["spaces"]):
                keys = sorted(set(scan["spaces"][sid].get("keys", [])))
                # Re-resolve immediately before EACH space delete. The all-space
                # proof above prevents an initially unsafe later space from
                # allowing an earlier mutation; this per-space proof prevents a
                # route that drifts while deleting an earlier space from being
                # mutated through a cached local sink.
                try:
                    current = await self._resolve_direct_local_sinks(
                        {"spaces": {sid: scan["spaces"][sid]}},
                        op="delete_many",
                    )
                except Exception as e:
                    if deleted == 0:
                        raise
                    route_failure_reason = (
                        self._route_refusal_reason(e) or "route_recheck_failed"
                    )
                    break

                reported = await current[sid].delete_many(keys)
                if not isinstance(reported, int) or not 0 <= reported <= len(keys):
                    raise RuntimeError(
                        f"Invalid delete_many count for {sid!r}: {reported!r}"
                    )
                deleted += reported
                await current[sid].commit(reason="admin_gc_notes_delete")

        for sid in scan["spaces"]:
            del scan["spaces"][sid]["keys"]

        scan["action"] = "delete"
        scan["delete_requested"] = requested
        scan["deleted"] = deleted
        scan["delete_failed"] = requested - deleted
        scan.pop("eligible_set_token", None)
        if deleted == requested and route_failure_reason is None:
            scan["status"] = "deleted"
            scan["message"] = (
                f"⚠️ {deleted} notes supprimées SANS consolidation "
                f"dans {len(scan['spaces'])} espace(s)"
            )
        else:
            scan["status"] = "partial"
            scan["reason"] = "partial_delete"
            if route_failure_reason is not None:
                scan["failure_reason"] = route_failure_reason
                scan["message"] = (
                    f"⚠️ Suppression partielle : {deleted}/{requested} notes "
                    "supprimées SANS consolidation. La revalidation de route "
                    "a refusé la suite ; les notes non supprimées restent "
                    "présentes. Relancez un dry-run avant toute nouvelle tentative."
                )
            else:
                scan["message"] = (
                    f"⚠️ Suppression partielle : {deleted}/{requested} notes "
                    "supprimées SANS consolidation. Les notes non supprimées "
                    "restent présentes ; relancez un dry-run avant toute nouvelle tentative."
                )
        return scan


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _extract_timestamp(filename: str) -> str | None:
    """Extrait le timestamp du nom de fichier. Format : YYYYMMDDTHHMMSS_..."""
    parsed = _parse_live_note_filename(filename)
    return parsed[0] if parsed is not None else None


def _extract_agent(filename: str) -> str:
    """Extrait le nom de l'agent du nom de fichier."""
    parsed = _parse_live_note_filename(filename)
    return parsed[1] if parsed is not None else "unknown"


def _parse_live_note_filename(filename: str) -> tuple[str, str] | None:
    """Parse one canonical direct-child live-note filename.

    The category and UUID are anchored from the right so agent identifiers may
    safely contain underscores without being merged into another agent bucket.
    """
    match = _LIVE_NOTE_FILENAME_RE.fullmatch(filename)
    if match is None:
        return None
    timestamp = match.group("timestamp")
    try:
        datetime.strptime(timestamp, "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return timestamp, match.group("agent")


# ─────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────

_gc_service: GCService | None = None


def get_gc_service() -> GCService:
    """Retourne le singleton GCService."""
    global _gc_service
    if _gc_service is None:
        _gc_service = GCService()
    return _gc_service
