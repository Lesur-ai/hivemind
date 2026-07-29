# -*- coding: utf-8 -*-
"""
Outils MCP — Catégorie Bank (11 outils).

Memory Bank consolidée : lire, lister, consolider via LLM, compacter,
réparer, écrire et supprimer manuellement.

Permissions :
    - bank_read        🔑 (read)    — Lit un fichier bank spécifique
    - bank_read_all    🔑 (read)    — Lit toute la bank (démarrage agent)
    - bank_list        🔑 (read)    — Liste les fichiers bank (sans contenu)
    - bank_consolidate ✏️ (write)   — Déclenche la consolidation LLM
    - bank_consolidation_status 🔑 (read) — Consulte un job de consolidation
    - bank_consolidation_queues 🔑 (read) — Résume les lanes de consolidation
    - bank_stale_spaces 🔑 (read)   — Liste les spaces avec trop de notes non consolidées
    - bank_compact     🔧 (manage)  — Compacte les fichiers bank surdimensionnés via LLM
    - bank_repair      🔧 (manage)  — Répare les noms de fichiers corrompus par le LLM
    - bank_write       🔧 (manage)  — Écrit/remplace un fichier bank directement
    - bank_delete      🔧 (manage)  — Supprime un fichier bank

La consolidation est l'opération qui transforme les notes live en
fichiers bank structurés. `bank_consolidate` place un job dans une file
FIFO en mémoire par espace. Un seul job à la fois mute la bank d'un espace
(protégé par asyncio.Lock).

Voir CONSOLIDATION_LLM.md pour le pipeline détaillé.
"""

import re
from datetime import datetime, timezone
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field


_LIVE_NOTE_TS_RE = re.compile(r"^(\d{8}T\d{6})_")


def _parse_live_note_timestamp(filename: str) -> datetime | None:
    """
    Extrait le timestamp UTC depuis le préfixe d'un nom de fichier de note live.

    Format attendu : `YYYYMMDDTHHMMSS_<agent>_<category>_<uuid8>.md`
    (généré par `LiveService.write_note()`).

    Retourne None si le format ne matche pas — la note sera ignorée.
    """
    m = _LIVE_NOTE_TS_RE.match(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


# LM2-12 fix : caractères interdits dans les noms de fichiers bank.
# Le sanitize Unicode existant ne couvrait que les chars invisibles.
# Ces caractères-ci permettent un XSS persistant côté web (LM2-01) si
# un opérateur compromis écrit un fichier nommé `<img src=x onerror=...>`.
# La règle s'applique au filename ENTIER (y compris les sous-dossiers).
# Les `/` restent autorisés comme séparateurs de sous-dossiers (Option B v0.9.0).
_BANK_FILENAME_DANGEROUS = re.compile(r"[<>\"'\\\x00-\x1f\x7f]")


def _validate_bank_filename(filename: str) -> dict | None:
    """
    LM2-12 fix : refuse les filenames bank contenant des caractères dangereux.

    Retourne None si OK, sinon un dict d'erreur prêt à être renvoyé.
    """
    if not filename or not filename.strip():
        return {"status": "error", "message": "Filename is required"}
    if ".." in filename:
        return {
            "status": "error",
            "message": "Invalid filename: '..' is not allowed",
        }
    if filename.startswith("/"):
        return {
            "status": "error",
            "message": "Invalid filename: it cannot start with '/'",
        }
    if _BANK_FILENAME_DANGEROUS.search(filename):
        return {
            "status": "error",
            "message": (
                "The filename contains unsafe characters "
                "(< > \" ' \\ or control characters are not allowed)"
            ),
        }
    return None


def register(mcp: FastMCP) -> int:
    """
    Enregistre les 11 outils bank sur l'instance MCP.

    Args:
        mcp: Instance FastMCP

    Returns:
        Nombre d'outils enregistrés (11)
    """

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def bank_read(
        space_id: Annotated[str, Field(description="Space identifier")],
        filename: Annotated[
            str,
            Field(
                description="Memory-bank filename, for example 'activeContext.md' or 'progress.md'"
            ),
        ],
    ) -> dict:
        """
        Read one file from a space's memory bank.

        Bank files are Markdown documents created and maintained by the
        consolidator.

        If the direct object key is absent, the tool also compares normalized
        filenames to recover names containing invisible Unicode characters.

        Args:
            space_id: Space to read.
            filename: Bank filename, for example ``activeContext.md``.

        Returns:
            File content, size, and modification time.
        """
        from ..auth.context import check_access
        from ..core.storage import get_storage
        from ..core.consolidator import _sanitize_filename

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            storage = get_storage()
            key = f"{space_id}/bank/{filename}"
            content = await storage.get(key)

            if content is None:
                # Fallback : la clé S3 réelle peut contenir des caractères
                # Unicode invisibles (bug LLM drift). On scanne les vraies
                # clés et on cherche par correspondance sanitisée.
                objects = await storage.list_objects(f"{space_id}/bank/")
                sanitized_target = _sanitize_filename(filename)
                matched_key = None

                for obj in objects:
                    raw_filename = obj["Key"].split("/")[-1]
                    if _sanitize_filename(raw_filename) == sanitized_target:
                        matched_key = obj["Key"]
                        break

                if matched_key:
                    content = await storage.get(matched_key)
                    if content is not None:
                        return {
                            "status": "ok",
                            "space_id": space_id,
                            "filename": filename,
                            "content": content,
                            "size": len(content.encode("utf-8")),
                            "note": (
                                f"Fichier trouvé via fallback Unicode "
                                f"(clé S3 réelle: {matched_key.split('/')[-1]!r}). "
                                f"Utilisez bank_repair pour corriger."
                            ),
                        }

                return {
                    "status": "not_found",
                    "message": f"File '{filename}' not found in '{space_id}'",
                }

            return {
                "status": "ok",
                "space_id": space_id,
                "filename": filename,
                "content": content,
                "size": len(content.encode("utf-8")),
            }
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "bank")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def bank_read_all(
        space_id: Annotated[str, Field(description="Space identifier")],
    ) -> dict:
        """
        Read every file in a space's memory bank in one request.

        Use this at session startup when an agent needs the complete compact
        context rather than selected files.

        Args:
            space_id: Space to read.

        Returns:
            All bank files with their content and aggregate size.
        """
        from ..auth.context import check_access
        from ..core.storage import get_storage

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            storage = get_storage()

            # Vérifier l'existence de l'espace
            if not await storage.exists(f"{space_id}/_meta.json"):
                return {
                    "status": "not_found",
                    "message": f"Space '{space_id}' not found",
                }

            # Lire tous les fichiers bank
            from ..core.storage import bank_relpath

            bank_data = await storage.list_and_get(f"{space_id}/bank/")
            files = [
                {
                    "filename": bank_relpath(item["key"], space_id),
                    "content": item["content"],
                    "size": item["size"],
                }
                for item in bank_data
            ]

            total_size = sum(f["size"] for f in files)

            return {
                "status": "ok",
                "space_id": space_id,
                "files": files,
                "total_size": total_size,
                "file_count": len(files),
            }
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "bank")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def bank_list(
        space_id: Annotated[str, Field(description="Space identifier")],
    ) -> dict:
        """
        List memory-bank files without returning their content.

        Use this to inspect the bank structure before reading selected files.

        Args:
            space_id: Space whose bank should be listed.

        Returns:
            Filenames, sizes, and modification times.
        """
        from ..auth.context import check_access
        from ..core.storage import get_storage

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            storage = get_storage()

            if not await storage.exists(f"{space_id}/_meta.json"):
                return {
                    "status": "not_found",
                    "message": f"Space '{space_id}' not found",
                }

            # Lister les objets bank (sans les .keep)
            from ..core.storage import bank_relpath

            objects = await storage.list_objects(f"{space_id}/bank/")
            files = [
                {
                    "filename": bank_relpath(o["Key"], space_id),
                    "size": o["Size"],
                    "last_modified": str(o.get("LastModified", "")),
                }
                for o in objects
                if not o["Key"].endswith(".keep")
            ]

            return {
                "status": "ok",
                "space_id": space_id,
                "files": files,
                "file_count": len(files),
            }
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "bank")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False))
    async def bank_consolidate(
        space_id: Annotated[
            str, Field(description="Space whose notes should be consolidated")
        ],
        agent: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Agent whose notes should be consolidated. Omit or pass "
                    "null for the caller; pass an explicit empty string for "
                    "all agents (manage or admin permission required)."
                ),
            ),
        ] = None,
    ) -> dict:
        """
        Queue an asynchronous consolidation of short-term notes into the bank.

        The queue is process-local and best-effort: queued jobs do not survive
        a server restart. One consolidation runs at a time per space, while
        different spaces may run in parallel.

        The job reads the rules, residual summary, selected notes, and current
        bank; asks the configured model to consolidate them; stores the updated
        bank; removes processed notes; and updates the residual summary.

        Args:
            space_id: Space to consolidate.
            agent: Omit or pass ``None`` for the caller's notes. An explicit
                empty string selects every agent and requires manage or admin
                permission. Selecting another agent also requires manage.

        Returns:
            A running or queued acknowledgement with a job identifier. Return
            control to the user; query ``bank_consolidation_status`` only when
            the user explicitly asks for status.
        """
        from ..auth.context import (
            check_access,
            check_write_permission,
            check_manage_permission,
            get_effective_token_info,
        )
        from ..core.consolidation_queue import get_consolidation_queue
        from ..core.engines import get_engine_registry
        from ..core.write_sink import (
            DirectLocalWriteSink,
            StagedWriteNotImplemented,
        )

        try:
            # Vérifier accès à l'espace
            access_err = check_access(space_id)
            if access_err:
                return access_err

            # Règles de permissions pour bank_consolidate :
            #
            # 1. write+ est le minimum inconditionnel et doit donc être le
            #    niveau annoncé par tools/list.
            # 2. agent omis/None → caller pour write, manage ET admin.
            # 3. agent="" explicitement fourni → toutes les notes, manage+.
            # 4. agent=autre → notes d'un autre agent, manage+.
            #
            # La distinction None/"" est volontaire : elle empêche un manager
            # d'élargir silencieusement une demande par défaut, tout en gardant
            # la sentinelle globale historique lorsqu'elle est envoyée
            # explicitement sur le wire.

            write_err = check_write_permission()
            if write_err:
                return write_err

            # Identifier le caller depuis l'identité effective de CETTE requête,
            # jamais depuis le contextvar MCP de session potentiellement périmé.
            # Une identité vide est incompatible avec la sentinelle wire ""
            # (scope global) : refuser avant routage/enqueue plutôt que risquer
            # de transformer un défaut caller-only en consolidation globale.
            token_info = get_effective_token_info()
            caller = token_info.get("client_name") if token_info else None
            if not isinstance(caller, str) or caller == "":
                return {
                    "status": "error",
                    "message": (
                        "A non-empty client_name identity is required to "
                        "consolidate notes"
                    ),
                }

            if agent is None:
                # Défaut sûr identique pour write/manage/admin : mes notes.
                agent = caller
            elif agent == "":
                # Scope global uniquement sur demande explicite.
                manage_err = check_manage_permission()
                if manage_err:
                    return manage_err
            elif agent != caller:
                manage_err = check_manage_permission()
                if manage_err:
                    return {
                        "status": "error",
                        "message": (
                            f"The 'manage' permission is required to consolidate "
                            f"agent '{agent}' notes. "
                            f"You can consolidate your own notes by omitting "
                            f"agent or by using agent='{caller}'."
                        ),
                    }

            # P3-7 ROUTE-FIRST GATE (fail-closed-routing mandatory correction):
            # bank_consolidate enqueues a job whose BACKGROUND WORKER performs
            # the durable bank/_synthesis/_meta writes (ConsolidatorService
            # get_storage call sites). The worker runs OUTSIDE the MCP auth/route
            # context, so the WriteSink seam cannot intercept it later. If we let
            # a Hivemind-space consolidation enqueue, the worker would write to S3
            # DIRECTLY, bypassing the single-writer seam this gate exists to
            # protect. We therefore resolve the per-space route BEFORE enqueue:
            #   - DIRECT_LOCAL (non-Hivemind) -> proceed to enqueue: the worker's
            #     direct get_storage writes are the legacy, byte-for-byte path.
            #   - STAGED (Hivemind-healthy) -> raise StagedWriteNotImplemented
            #     BEFORE enqueue, so NO job is queued and NO worker write occurs.
            #   - REFUSE (unsafe/resync) / corrupt -> resolve_sink raises
            #     (RegistryRefused / CorruptedStateError) before enqueue.
            # This makes the gate fire at the tool entrypoint (the only point in
            # the auth/route context), keeping the worker write path fail-closed
            # for shared spaces. resolve_sink is a read-only route check; it
            # queues nothing and writes nothing on the non-Hivemind path.
            sink = await get_engine_registry().resolve_sink(space_id)
            if not isinstance(sink, DirectLocalWriteSink):
                # STAGED: surface the typed refusal before the job is queued so
                # the background worker (which writes via get_storage) never runs.
                raise StagedWriteNotImplemented(
                    op="consolidate", key=f"{space_id}/bank/"
                )

            # DIRECT_LOCAL only: enqueue the job VERBATIM on the queue singleton.
            # The gate above already proved the space is non-Hivemind, so the
            # worker's eventual direct get_storage writes are the legacy path
            # (byte-for-byte). The enqueue call itself is unchanged (same kwargs
            # the merged suite pins) — only the route-first gate is new.
            # Le worker de fond utilise l'agent effectif capturé ici, sans
            # dépendre du contexte d'auth MCP.
            # agent="" → consolide TOUTES les notes (demande explicite,
            # manage/admin uniquement)
            # agent="mon-agent" → consolide uniquement les notes de cet agent
            return await get_consolidation_queue().enqueue(
                space_id=space_id,
                agent=agent,
                requested_by=caller,
            )

        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "bank")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def bank_consolidation_status(
        job_id: Annotated[
            str, Field(description="Consolidation job identifier")
        ],
    ) -> dict:
        """
        Read the current status of a process-local consolidation job.

        Args:
            job_id: Identifier returned by ``mid_consolidate``.

        Returns:
            Job status and, when complete, its result or error.
        """
        from ..auth.context import check_access
        from ..core.consolidation_queue import get_consolidation_queue

        try:
            result = await get_consolidation_queue().get_job(job_id)
            if result.get("status") == "not_found":
                return result

            access_err = check_access(result["space_id"])
            if access_err:
                return access_err

            return result
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "bank")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def bank_consolidation_queues(
        space_ids: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Optional comma-separated space identifiers. Empty selects "
                    "all spaces accessible to the current credential."
                ),
            ),
        ] = "",
    ) -> dict:
        """
        Summarize consolidation activity by space.

        Each space has an independent FIFO queue and at most one running job.
        Different spaces may consolidate in parallel. Manage and admin callers
        can inspect all-agent work; other callers remain agent-scoped.

        Args:
            space_ids: Optional comma-separated target list. Empty selects all
                accessible spaces.

        Returns:
            Per-space queues and aggregate activity counts.
        """
        from ..auth.context import _get_effective_token_info, check_access
        from ..config import get_settings
        from ..core.consolidation_queue import get_consolidation_queue
        from ..core.space import get_space_service

        try:
            token_info = _get_effective_token_info()
            if token_info is None:
                return {"status": "error", "message": "Authentication required"}

            requested_ids = [
                sid.strip() for sid in space_ids.split(",") if sid.strip()
            ]
            denied_spaces = []

            if requested_ids:
                visible_ids = []
                for sid in requested_ids:
                    access_err = check_access(sid)
                    if access_err:
                        denied_spaces.append(
                            {"space_id": sid, "message": access_err.get("message")}
                        )
                        continue
                    visible_ids.append(sid)
            else:
                permissions = token_info.get("permissions", [])
                allowed = token_info.get("allowed_resources", [])
                if "admin" in permissions:
                    allowed_ids = None
                elif not allowed:
                    allowed_ids = []
                else:
                    allowed_ids = allowed
                spaces_result = await get_space_service().list_spaces(
                    allowed_space_ids=allowed_ids
                )
                if spaces_result.get("status") != "ok":
                    return spaces_result
                visible_ids = [s["space_id"] for s in spaces_result.get("spaces", [])]

            queue = get_consolidation_queue()
            lanes = [await queue.get_space_summary(sid) for sid in visible_ids]
            running = sum(1 for lane in lanes if lane.get("running_job"))
            queued = sum(lane.get("queued_count", 0) for lane in lanes)
            failed_recent = sum(
                1
                for lane in lanes
                for job in lane.get("latest_jobs", [])
                if job.get("status") == "failed"
            )
            active = sum(
                1
                for lane in lanes
                if lane.get("running_job") or lane.get("queued_count", 0) > 0
            )

            return {
                "status": "ok",
                "lanes": lanes,
                "total_spaces": len(lanes),
                "active_spaces": active,
                "running_spaces": running,
                "queued_jobs": queued,
                "failed_recent": failed_recent,
                "parallelism_model": "one_worker_per_space",
                "service_config": {
                    "batch_size": get_settings().consolidation_batch_size,
                },
                "denied_spaces": denied_spaces,
            }
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "bank")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def bank_stale_spaces(
        min_notes: Annotated[
            int,
            Field(
                default=5,
                ge=1,
                description=(
                    "Minimum unconsolidated note count for a space to be "
                    "considered stale (default: 5)."
                ),
            ),
        ] = 5,
        min_age_days: Annotated[
            int,
            Field(
                default=5,
                ge=0,
                description=(
                    "Minimum age in days of the oldest note for a space to be "
                    "considered stale (default: 5)."
                ),
            ),
        ] = 5,
        space_ids: Annotated[
            str,
            Field(
                default="",
                description=(
                    "Optional comma-separated space identifiers. Empty selects "
                    "all spaces accessible to the current credential."
                ),
            ),
        ] = "",
    ) -> dict:
        """
        Find spaces whose note consolidation is stale.

        For each accessible space, counts unconsolidated short-term notes and
        calculates the age of the oldest note. A space is stale only when both
        ``live_notes_count >= min_notes`` and
        ``oldest_note_age_days >= min_age_days``.

        This read-only tool helps operators find spaces accumulating context
        before deciding whether to trigger consolidation.

        Args:
            min_notes: Minimum unconsolidated note count.
            min_age_days: Minimum age of the oldest note, in days.
            space_ids: Optional comma-separated target list.

        Returns:
            Stale spaces, aggregate metrics, and inaccessible targets.
        """
        from ..auth.context import _get_effective_token_info, check_access
        from ..core.space import get_space_service
        from ..core.storage import get_storage

        try:
            token_info = _get_effective_token_info()
            if token_info is None:
                return {"status": "error", "message": "Authentication required"}

            requested_ids = [
                sid.strip() for sid in space_ids.split(",") if sid.strip()
            ]
            denied_spaces = []

            if requested_ids:
                visible_ids = []
                for sid in requested_ids:
                    access_err = check_access(sid)
                    if access_err:
                        denied_spaces.append(
                            {"space_id": sid, "message": access_err.get("message")}
                        )
                        continue
                    visible_ids.append(sid)
            else:
                permissions = token_info.get("permissions", [])
                allowed = token_info.get("allowed_resources", [])
                if "admin" in permissions:
                    allowed_ids = None
                elif not allowed:
                    allowed_ids = []
                else:
                    allowed_ids = allowed
                spaces_result = await get_space_service().list_spaces(
                    allowed_space_ids=allowed_ids
                )
                if spaces_result.get("status") != "ok":
                    return spaces_result
                visible_ids = [s["space_id"] for s in spaces_result.get("spaces", [])]

            storage = get_storage()
            now = datetime.now(timezone.utc)
            scanned = []
            stale = []

            for sid in visible_ids:
                objects = await storage.list_objects(f"{sid}/live/")
                notes_count = 0
                oldest_ts: datetime | None = None
                oldest_filename = ""

                for obj in objects:
                    key = obj.get("Key", "")
                    filename = key.rsplit("/", 1)[-1]
                    ts = _parse_live_note_timestamp(filename)
                    if ts is None:
                        continue
                    notes_count += 1
                    if oldest_ts is None or ts < oldest_ts:
                        oldest_ts = ts
                        oldest_filename = filename

                if notes_count == 0 or oldest_ts is None:
                    scanned.append(
                        {
                            "space_id": sid,
                            "live_notes_count": 0,
                            "oldest_note_age_days": 0.0,
                            "oldest_note_timestamp": "",
                            "oldest_note_filename": "",
                            "is_stale": False,
                        }
                    )
                    continue

                age_days = (now - oldest_ts).total_seconds() / 86400.0
                is_stale = (
                    notes_count >= min_notes and age_days >= float(min_age_days)
                )
                # Truncate (not round) to 2 decimals so the displayed age never
                # exceeds the real age. Otherwise "5.0 days, not stale" can
                # appear when the threshold is 5 — confusing the operator.
                displayed_age = int(age_days * 100) / 100.0
                entry = {
                    "space_id": sid,
                    "live_notes_count": notes_count,
                    "oldest_note_age_days": displayed_age,
                    "oldest_note_timestamp": oldest_ts.isoformat(),
                    "oldest_note_filename": oldest_filename,
                    "is_stale": is_stale,
                }
                scanned.append(entry)
                if is_stale:
                    stale.append(entry)

            stale.sort(
                key=lambda e: (
                    -e["live_notes_count"],
                    -e["oldest_note_age_days"],
                )
            )

            return {
                "status": "ok",
                "spaces": stale,
                "scanned": scanned,
                "total_spaces": len(scanned),
                "total_stale": len(stale),
                "min_notes": min_notes,
                "min_age_days": min_age_days,
                "denied_spaces": denied_spaces,
            }
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "bank")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    async def bank_repair(
        space_id: Annotated[
            str, Field(description="Identifier of the space to repair")
        ],
        dry_run: Annotated[
            bool,
            Field(
                default=True,
                description="True = scan only (list files to repair); False = apply the repairs",
            ),
        ] = True,
    ) -> dict:
        """
        Répare les fichiers bank : caractères Unicode invisibles,
        préfixes parasites (1.MEMORY_BANK/) et doublons multi-chemins.

        Détecte 3 types de problèmes :
        1. Caractères Unicode invisibles dans les noms de fichiers
        2. Préfixes parasites (1.MEMORY_BANK/, MEMORY_BANK/, bank/)
        3. Doublons : même fichier sanitisé à des chemins S3 différents

        Pour chaque fichier, extrait le chemin relatif complet,
        le sanitise, et si le chemin canonique diffère :
        - Écrit le contenu sous le chemin canonique
        - Supprime l'ancien fichier

        Si un doublon existe (même nom sanitisé, plusieurs clés S3),
        garde la version la plus récente et supprime les autres.

        ⚠️ Par défaut dry_run=True : scanne et rapporte sans modifier.
        Passez dry_run=False pour appliquer les corrections.

        Args:
            space_id: Espace à réparer
            dry_run: True = scan seul, False = correction effective

        Returns:
            Liste des fichiers réparés + doublons détectés
        """
        from ..auth.context import check_access, check_manage_permission
        from ..core.storage import get_storage, bank_relpath
        from ..core.consolidator import _sanitize_filename
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            manage_err = check_manage_permission()
            if manage_err:
                return manage_err

            storage = get_storage()

            # Vérifier l'existence de l'espace
            if not await storage.exists(f"{space_id}/_meta.json"):
                return {
                    "status": "not_found",
                    "message": f"Space '{space_id}' not found",
                }

            # Lister les vrais fichiers bank sur S3 (READ — stays on storage)
            objects = await storage.list_objects(f"{space_id}/bank/")

            # Phase 1 : Scanner et grouper par nom sanitisé
            # sanitized_name → [(s3_key, relpath, size, last_modified), ...]
            groups: dict[str, list] = {}
            for obj in objects:
                key = obj["Key"]
                if key.endswith(".keep"):
                    continue

                relpath = bank_relpath(key, space_id)
                sanitized = _sanitize_filename(relpath)

                if sanitized not in groups:
                    groups[sanitized] = []
                groups[sanitized].append(
                    {
                        "key": key,
                        "relpath": relpath,
                        "size": obj["Size"],
                        "last_modified": str(obj.get("LastModified", "")),
                    }
                )

            # Phase 2 : Identifier les réparations et doublons
            repairs = []
            duplicates = []
            files_ok = 0

            for sanitized, entries in groups.items():
                canonical_key = f"{space_id}/bank/{sanitized}"

                # Trier par date (plus récent d'abord) pour garder la meilleure version
                entries.sort(key=lambda e: e["last_modified"], reverse=True)

                if len(entries) == 1 and entries[0]["key"] == canonical_key:
                    # Fichier OK : un seul exemplaire au bon chemin
                    files_ok += 1
                    continue

                # Premier = version à garder (la plus récente)
                best = entries[0]

                if best["key"] != canonical_key:
                    # Le fichier principal n'est pas au bon chemin → réparer
                    repairs.append(
                        {
                            "original_relpath": best["relpath"],
                            "sanitized": sanitized,
                            "original_key": best["key"],
                            "canonical_key": canonical_key,
                            "size": best["size"],
                            "action": "move",
                        }
                    )

                # Les autres entrées sont des doublons à supprimer
                for dup in entries[1:] if len(entries) > 1 else []:
                    duplicates.append(
                        {
                            "relpath": dup["relpath"],
                            "key": dup["key"],
                            "size": dup["size"],
                            "canonical": sanitized,
                            "action": "delete_duplicate",
                        }
                    )

            # Phase 3 : Appliquer si dry_run=False
            if not dry_run:
                # P3-7 ROUTE-FIRST: resolve the per-space WriteSink BEFORE any
                # durable put/delete. Non-Hivemind -> DirectLocalWriteSink
                # (byte-identical legacy put/delete). Hivemind-healthy ->
                # StagedHivemindWriteSink (first sink.put raises
                # StagedWriteNotImplemented -> safe_error, NO write).
                # UNSAFE/RESYNC -> RegistryRefused; corrupt ->
                # CorruptedStateError: both raise here, before any write, and
                # surface via the except below. The reads (storage.get) STAY on
                # storage. The dry_run scan above is read-only and ungated.
                sink = await get_engine_registry().resolve_sink(space_id)
                for r in repairs:
                    content = await storage.get(r["original_key"])
                    if content is not None:
                        await sink.put(r["canonical_key"], content)
                        if r["original_key"] != r["canonical_key"]:
                            await sink.delete(r["original_key"])
                        r["status"] = "repaired"
                    else:
                        r["status"] = "error_read"

                for d in duplicates:
                    await sink.delete(d["key"])
                    d["status"] = "deleted"

                # P5-8 (#16): route-blind flush. DirectLocal -> no-op (the
                # put/delete above already wrote through, byte-for-byte). On a
                # Hivemind hive the repair's sink.delete (old-key move + dup
                # removal) already raised StagedWriteNotImplemented (live-bank
                # delete is the deferred put-only gap) -> safe_error, no mutation.
                await sink.commit(reason="bank_repair")
            else:
                for r in repairs:
                    r["status"] = "would_repair"
                for d in duplicates:
                    d["status"] = "would_delete"

            mode = "dry-run" if dry_run else "applied"
            total_issues = len(repairs) + len(duplicates)

            return {
                "status": "ok",
                "space_id": space_id,
                "mode": mode,
                "files_scanned": len(groups),
                "files_ok": files_ok,
                "files_to_repair": len(repairs),
                "duplicates_found": len(duplicates),
                "repairs": repairs,
                "duplicates": duplicates,
                "message": (
                    f"{len(repairs)} file(s) to move, "
                    f"{len(duplicates)} duplicate(s) to delete "
                    f"across {len(groups)} unique files. "
                    + (
                        "Set dry_run=False to apply the repairs."
                        if dry_run and total_issues > 0
                        else ""
                    )
                    + (
                        "Repairs applied."
                        if not dry_run and total_issues > 0
                        else ""
                    )
                    + ("All files are OK." if total_issues == 0 else "")
                ),
            }
        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "bank")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    async def bank_write(
        space_id: Annotated[str, Field(description="Space identifier")],
        filename: Annotated[
            str, Field(description="Bank filename (for example, 'activeContext.md')")
        ],
        content: Annotated[
            str, Field(description="Complete Markdown file content")
        ],
    ) -> dict:
        """
        Écrit ou remplace un fichier dans la Memory Bank (manage).

        ⚠️ Cet outil contourne la consolidation LLM — il écrit directement
        dans la bank. À utiliser pour les corrections manuelles quand la
        consolidation échoue (doublons, contenu tronqué, migration).

        Si un fichier avec le même nom existe déjà, il est remplacé.
        Les éventuels doublons Unicode sont automatiquement nettoyés.

        Args:
            space_id: Identifiant de l'espace
            filename: Nom du fichier à écrire
            content: Contenu Markdown complet

        Returns:
            Statut de l'écriture avec taille du fichier
        """
        from ..auth.context import check_access, check_manage_permission
        from ..core.storage import get_storage
        from ..core.consolidator import _sanitize_filename
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            manage_err = check_manage_permission()
            if manage_err:
                return manage_err

            # LM2-12 fix : refuser les caractères dangereux (XSS persistant
            # côté web — voir LM2-01). Cette validation s'ajoute au
            # _sanitize_filename existant qui ne traitait que l'Unicode.
            name_err = _validate_bank_filename(filename)
            if name_err:
                return name_err

            storage = get_storage()

            # Vérifier l'existence de l'espace
            if not await storage.exists(f"{space_id}/_meta.json"):
                return {
                    "status": "not_found",
                    "message": f"Space '{space_id}' not found",
                }

            # Sanitiser le filename
            sanitized = _sanitize_filename(filename)
            if not sanitized:
                return {
                    "status": "error",
                    "message": f"Invalid filename: '{filename}'",
                }

            # Re-valider après sanitisation Unicode (défense en profondeur :
            # _sanitize_filename pourrait ne pas être idempotent face à
            # certaines combinaisons de caractères).
            post_sanitize_err = _validate_bank_filename(sanitized)
            if post_sanitize_err:
                return post_sanitize_err

            # P3-7 ROUTE-FIRST: resolve the per-space WriteSink BEFORE the
            # durable put/delete. Non-Hivemind -> DirectLocalWriteSink
            # (byte-identical legacy put with NO explicit content_type, so the
            # StorageService default applies). Hivemind-healthy ->
            # StagedHivemindWriteSink (sink.put raises StagedWriteNotImplemented
            # -> safe_error, NO write). UNSAFE/RESYNC -> RegistryRefused;
            # corrupt -> CorruptedStateError: both raise before any write. The
            # exists/list_objects READS stay on storage.
            sink = await get_engine_registry().resolve_sink(space_id)

            # Écrire le fichier avec le nom canonique
            canonical_key = f"{space_id}/bank/{sanitized}"
            existed = await storage.exists(canonical_key)
            await sink.put(canonical_key, content)

            # Nettoyer les doublons Unicode (clés S3 qui sanitisent vers
            # le même nom mais avec des caractères invisibles)
            cleaned = 0
            objects = await storage.list_objects(f"{space_id}/bank/")
            for obj in objects:
                raw_key = obj["Key"]
                if raw_key == canonical_key or raw_key.endswith(".keep"):
                    continue
                raw_filename = raw_key.split("/")[-1]
                if _sanitize_filename(raw_filename) == sanitized:
                    await sink.delete(raw_key)
                    cleaned += 1

            # P5-8 (#16): flush the buffered durable write(s) as ONE atomic op.
            # On DirectLocal this is a no-op (the put/delete above already wrote
            # through, byte-for-byte). On a Hivemind StagedHivemindWriteSink this
            # drives the single CommitRuntime.apply_commit (assert_commit_allowed
            # is the only auth). The tool stays route-blind.
            await sink.commit(reason="bank_write")

            action = "replaced" if existed else "created"
            result = {
                "status": "ok",
                "space_id": space_id,
                "filename": sanitized,
                "action": action,
                "size": len(content.encode("utf-8")),
            }
            if cleaned > 0:
                result["unicode_duplicates_cleaned"] = cleaned
            return result

        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "bank")

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True))
    async def bank_delete(
        space_id: Annotated[str, Field(description="Space identifier")],
        filename: Annotated[str, Field(description="Bank filename to delete")],
        confirm: Annotated[
            bool,
            Field(
                default=False,
                description="Must be true to confirm deletion",
            ),
        ] = False,
    ) -> dict:
        """
        Delete one file from a space's memory bank (manage permission).

        Also removes duplicate objects whose normalized filename matches the
        requested filename.

        This operation is irreversible. Read the file first if its content must
        be preserved. An explicit ``confirm=True`` acknowledgement is required.

        Args:
            space_id: Space containing the file.
            filename: Filename to delete; subdirectories are allowed.
            confirm: Must be true to acknowledge the destructive operation.

        Returns:
            Number of deleted objects, including normalized duplicates.
        """
        from ..auth.context import check_access, check_manage_permission
        from ..core.storage import get_storage, bank_relpath
        from ..core.consolidator import _sanitize_filename
        from ..core.engines import get_engine_registry

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            manage_err = check_manage_permission()
            if manage_err:
                return manage_err

            # LM2-31 fix : exiger confirm=True (harmonisation avec les autres
            # outils destructifs : space_delete, backup_restore, backup_delete,
            # admin_gc_notes).
            if not confirm:
                return {
                    "status": "error",
                    "message": (
                        "Deletion refused: confirm=True is required to delete "
                        "a bank file (this action is irreversible)."
                    ),
                }

            storage = get_storage()

            # Vérifier l'existence de l'espace
            if not await storage.exists(f"{space_id}/_meta.json"):
                return {
                    "status": "not_found",
                    "message": f"Space '{space_id}' not found",
                }

            sanitized = _sanitize_filename(filename)

            # Trouver toutes les clés S3 qui sanitisent vers ce nom
            # (= le fichier canonique + tous ses doublons)
            objects = await storage.list_objects(f"{space_id}/bank/")
            keys_to_delete = []
            for obj in objects:
                raw_key = obj["Key"]
                if raw_key.endswith(".keep"):
                    continue
                raw_relpath = bank_relpath(raw_key, space_id)
                if _sanitize_filename(raw_relpath) == sanitized:
                    keys_to_delete.append(raw_key)

            if not keys_to_delete:
                return {
                    "status": "not_found",
                    "message": f"File '{filename}' not found in '{space_id}'",
                }

            # P3-7 ROUTE-FIRST: resolve the per-space WriteSink BEFORE the
            # destructive multi-key delete. Non-Hivemind -> DirectLocalWriteSink
            # (byte-identical legacy delete_many, returns the same int count).
            # Hivemind-healthy -> StagedHivemindWriteSink (delete_many raises
            # StagedWriteNotImplemented -> safe_error, targets still present).
            # UNSAFE/RESYNC -> RegistryRefused; corrupt -> CorruptedStateError:
            # both raise before any delete. The list_objects/exists READS stay
            # on storage. NOTE: requires delete_many on the sink's storage —
            # the real StorageService has it; routing tests use a fake that
            # implements it (WriteSinkFakeStorage).
            sink = await get_engine_registry().resolve_sink(space_id)

            # Supprimer toutes les variantes
            deleted = await sink.delete_many(keys_to_delete)
            # P5-8 (#16): route-blind flush. DirectLocal -> no-op (delete_many
            # already wrote through). On a Hivemind hive, delete_many above raised
            # StagedWriteNotImplemented (live-bank delete is the deferred put-only
            # gap) -> safe_error, no mutation; this commit() is never reached.
            await sink.commit(reason="bank_delete")

            return {
                "status": "deleted",
                "space_id": space_id,
                "filename": sanitized,
                "files_deleted": deleted,
                "keys_deleted": [k.split("/")[-1] for k in keys_to_delete],
            }

        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "bank")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
    async def bank_compact(
        space_id: Annotated[
            str, Field(description="Identifier of the space to compact")
        ],
        dry_run: Annotated[
            bool,
            Field(
                default=True,
                description="True = scan seul (rapport sans modification), False = compaction effective via LLM",
            ),
        ] = True,
    ) -> dict:
        """
        Compacte les fichiers bank surdimensionnés via LLM (manage).

        Analyse chaque fichier bank et compare sa taille à la limite
        universelle configurée (BANK_FILE_MAX_SIZE, par défaut 15 KB).
        Les fichiers dépassant cette limite sont résumés/nettoyés par le LLM.

        Le LLM utilise les rules de l'espace pour comprendre le rôle de
        chaque fichier et applique des règles de compaction adaptées :
        fusionne les redondances, supprime les détails obsolètes,
        résume les entrées anciennes en une ligne par jalon.

        ⚠️ Par défaut dry_run=True : scanne et rapporte sans modifier.
        Passez dry_run=False pour compacter effectivement.

        ⚠️ La compaction est protégée par le lock de consolidation.
        Si une consolidation est en cours, retourne "conflict".

        Args:
            space_id: Espace à compacter
            dry_run: True = scan seul, False = compaction effective

        Returns:
            Rapport de compaction avec détails par fichier (taille, ratio, réduction)
        """
        from ..auth.context import check_access, check_manage_permission
        from ..core.locks import get_lock_manager
        from ..core.consolidator import get_consolidator
        from ..core.engines import get_engine_registry
        from ..core.write_sink import (
            DirectLocalWriteSink,
            StagedWriteNotImplemented,
        )

        try:
            access_err = check_access(space_id)
            if access_err:
                return access_err

            manage_err = check_manage_permission()
            if manage_err:
                return manage_err

            # Protéger par le lock de consolidation (la compaction
            # modifie les fichiers bank — incompatible avec une
            # consolidation simultanée).
            #
            # P3-7 ROUTE-FIRST-THEN-DELEGATE: the consolidation LOCK +
            # conflict-check STAY in the tool layer (the engine adds no lock —
            # mid.py invariant: single-writer-per-space). Only the dry_run=False
            # (write) branch is gated. The compact PUTs live INSIDE
            # ConsolidatorService.compact_bank, which calls get_storage()
            # directly — the held sink is INERT (wrap-don't-rewrite; we do NOT
            # edit consolidator.py). So we resolve the route FIRST and:
            #   - DIRECT_LOCAL (non-Hivemind) -> delegate to mid_engine().
            #     compact_bank: the consolidator's verbatim get_storage path
            #     (byte-identical).
            #   - STAGED (Hivemind-healthy) -> raise StagedWriteNotImplemented
            #     BEFORE the consolidator runs, so NO compact PUT happens.
            #   - REFUSE (unsafe/resync) / corrupt -> resolve_sink raises
            #     (RegistryRefused / CorruptedStateError) before any write.
            # The dry_run=True branch is a READ-ONLY scan (compact_bank guards
            # its PUT with `if not dry_run`) and MUST NOT be gated — it stays
            # verbatim on get_consolidator() so an unsafe/corrupt hive can still
            # be scanned (reads-stay invariant).
            if not dry_run:
                lock = get_lock_manager().consolidation(space_id)
                if lock.locked():
                    return {
                        "status": "conflict",
                        "message": (
                            f"Consolidation is in progress for '{space_id}'. "
                            "Try again in a few minutes."
                        ),
                    }
                async with lock:
                    # SINGLE resolution: build the engine (resolves once) and
                    # gate on the ENGINE's own resolved sink, so an observed
                    # STAGED can never fall through to the inert legacy compact
                    # write. REFUSE/corrupt raise inside mid_engine() first.
                    registry = get_engine_registry()
                    engine = await registry.mid_engine(space_id)
                    if not isinstance(engine.write_sink, DirectLocalWriteSink):
                        # STAGED: surface the typed refusal before the legacy
                        # consolidator (which writes via get_storage) ever runs.
                        raise StagedWriteNotImplemented(
                            op="put", key=f"{space_id}/bank/"
                        )
                    return await engine.compact_bank(space_id, dry_run=False)
            else:
                # Dry-run : pas besoin de lock (lecture seule). READ-ONLY scan —
                # not routed through resolve_sink (reads-stay).
                return await get_consolidator().compact_bank(space_id, dry_run=True)

        except Exception as e:
            from ..auth.context import safe_error

            return safe_error(e, "bank")

    return 11  # Nombre d'outils enregistrés
