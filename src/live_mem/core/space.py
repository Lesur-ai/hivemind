# -*- coding: utf-8 -*-
"""
Service Space — Gestion des espaces mémoire et des notes live.

Ce service encapsule toutes les opérations sur les espaces :
    - CRUD espaces (create, list, info, rules, summary, export, delete)
    - Notes live (write, read, search)

Chaque méthode traduit l'opération en appels S3 via StorageService.
Les outils MCP (tools/space.py, tools/live.py) délèguent ici.

Voir S3_DATA_MODEL.md pour l'arborescence S3 des espaces.
Voir MCP_TOOLS_SPEC.md pour les signatures et retours attendus.
"""

import re
import base64
import tarfile
import io
from datetime import datetime, timezone
from typing import Optional

from .storage import get_storage, bank_relpath
from .locks import get_lock_manager
from .models import SpaceMeta, mask_meta_secrets
from .hivemind import hive_status_label, CorruptedStateError
from .reservation_guard import assert_space_not_reserved


# ─────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────

# Regex de validation du space_id (alphanumérique + tirets/underscores)
SPACE_ID_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# VULN-07 fix : limites de taille pour les contenus
MAX_RULES_SIZE = 50_000  # 50K caractères max pour les rules
MAX_DESCRIPTION_SIZE = 500  # 500 caractères max pour la description


# ─────────────────────────────────────────────────────────────
# P2-4 — Label de statut unifié, fail-closed au niveau service
# ─────────────────────────────────────────────────────────────


async def _hive_status_label_failclosed(storage, space_id: str) -> str:
    """
    Calcule ``hive_status_label`` pour une surface de lecture SpaceService,
    fail-closed (P2-4, EPIC-P2, ADR-0008/ADR-0004).

    ``hive_status_label`` (read-only, 6 valeurs : not_a_space/local_only/
    hivemind_healthy/hivemind_blocked/unsafe/resync_required — espace distinct
    de la clé 4-valeurs ``hive_status`` de ``hive_status()``, #10) lève
    ``CorruptedStateError`` quand node.json/members.json/node_status.json sont
    corrompus. La décision de design P2-4 est de LIER cette corruption à
    ``"unsafe"`` ICI, au niveau service :

    - On ne dégrade JAMAIS une corruption en ``local_only``/``not_a_space``
      (ce serait un faux « pas un espace partagé » qui ré-ouvrirait le chemin
      d'écriture directe legacy et provoquerait un split-brain — invariant
      FAIL-CLOSED de Hivemind).
    - On ne laisse JAMAIS la surface de lecture crasher sur l'exception : elle
      reste utilisable, avec le label ``"unsafe"`` qui signale l'état non sûr.

    On n'attrape QUE ``CorruptedStateError`` (pas ``Exception``) pour que les
    vrais bugs continuent de remonter.
    """
    try:
        return await hive_status_label(storage, space_id)
    except CorruptedStateError:
        return "unsafe"


class SpaceService:
    """
    Service de gestion des espaces mémoire et des notes live.

    Toutes les méthodes sont async et retournent un dict
    avec un champ "status" conforme à la convention MCP.
    """

    # ─────────────────────────────────────────────────────────
    # SPACES — CRUD
    # ─────────────────────────────────────────────────────────

    async def create(
        self,
        space_id: str,
        description: str,
        rules: str,
        owner: str = "",
        *,
        actor_token_hash: str = "",
        bootstrap_admin: bool = False,
    ) -> dict:
        """
        Crée un nouvel espace mémoire avec ses rules.

        ``_meta.json`` est le commit marker et est toujours écrit EN DERNIER,
        après rules/keeps et, pour un manager S3, son grant durable.
        Le registre de scopes est lu sous ``lifecycle → tokens`` même pour
        le bootstrap afin de bloquer toute réactivation ABA d'anciens grants.

        Args:
            space_id: Identifiant unique (alphanum + tirets, max 64 chars)
            description: Description courte de l'espace
            rules: Contenu Markdown des rules (structure de la bank)
            owner: Propriétaire (optionnel, informatif)

        Returns:
            {"status": "created", "space_id": ..., ...} ou erreur
        """
        # Valider le space_id
        if not SPACE_ID_REGEX.match(space_id):
            return {
                "status": "error",
                "message": (
                    f"space_id invalide : '{space_id}'. "
                    "Attendu : alphanumérique + tirets/underscores, 1-64 chars."
                ),
            }

        # VULN-07 fix : valider les tailles des champs
        if len(rules) > MAX_RULES_SIZE:
            return {
                "status": "error",
                "message": f"Rules trop longues ({len(rules)} chars, max {MAX_RULES_SIZE})",
            }
        if description and len(description) > MAX_DESCRIPTION_SIZE:
            return {
                "status": "error",
                "message": f"Description trop longue ({len(description)} chars, max {MAX_DESCRIPTION_SIZE})",
            }

        storage = get_storage()
        locks = get_lock_manager()

        # Le verrou de cycle de vie est toujours pris avant le verrou tokens.
        # ``delete`` utilise le même verrou, empêchant la suppression d'un
        # préfixe pendant sa préparation ou son commit.
        async with locks.space_lifecycle(space_id):
            from .tokens import get_token_service

            token_service = get_token_service()
            async with locks.tokens:
                # Toujours charger l'autorité de scopes, bootstrap compris :
                # elle porte l'historique nécessaire pour bloquer une
                # suppression→recréation ABA qui réactiverait d'anciens grants.
                store = await token_service._load_store()
                actor = None
                if not bootstrap_admin:
                    actor = token_service._authorize_stored_manager(
                        store, actor_token_hash
                    )
                    if actor is None:
                        return {
                            "status": "error",
                            "message": "Token S3 manage ou admin actif requis",
                        }
                return await self._create_locked(
                    storage,
                    space_id=space_id,
                    description=description,
                    rules=rules,
                    owner=owner,
                    token_service=token_service,
                    token_store=store,
                    actor=actor,
                )

    @staticmethod
    async def classify_committed_state(storage, space_id: str) -> tuple[str, str]:
        """Classe le marker + les objets constitutifs d'un espace.

        Returns:
            ``("committed", "")`` si le marker est valide et rules/keeps
            existent, ``("absent", "")`` si aucun marker n'existe, sinon
            ``("unsafe", reason)``. Cette classification unique est partagée
            par ``space_create`` et ``space_invite_token``.
        """
        meta_key = f"{space_id}/_meta.json"
        try:
            if not await storage.exists(meta_key):
                return ("absent", "")
        except Exception:
            return ("unsafe", "Impossible de déterminer si le commit marker existe.")

        try:
            existing_meta = await storage.get_json(meta_key)
            parsed_meta = SpaceMeta(**(existing_meta or {}))
        except Exception:
            return ("unsafe", "Commit marker présent mais corrompu ou illisible.")
        if parsed_meta.space_id != space_id:
            return (
                "unsafe",
                "Commit marker présent mais rattaché à un autre space_id.",
            )

        required = {
            f"{space_id}/_rules.md": None,
            f"{space_id}/live/.keep": "",
            f"{space_id}/bank/.keep": "",
        }
        for key, exact_content in required.items():
            try:
                content = await storage.get(key)
            except Exception:
                content = None
            if content is None:
                return (
                    "unsafe",
                    "Espace marqué committé mais objet constitutif absent/illisible.",
                )
            if exact_content is not None and content != exact_content:
                return (
                    "unsafe",
                    "Espace marqué committé mais sentinel constitutif invalide.",
                )
        return ("committed", "")

    @staticmethod
    def _partial_create(
        space_id: str,
        message: str,
        *,
        retry_safe: bool = False,
        recovery_action: str = "",
    ) -> dict:
        """Retour explicite et actionnable pour une création non committée."""
        if not recovery_action:
            if retry_safe:
                recovery_action = (
                    "Retentez space_create avec exactement les mêmes space_id, "
                    "description, owner et rules ; la reprise est additive et "
                    "n'écrase aucun objet existant."
                )
            else:
                recovery_action = (
                    "N'effectuez aucune reprise automatique. Un admin doit inspecter "
                    f"le préfixe exact '{space_id}/' et ne le retirer qu'après avoir "
                    "confirmé qu'il ne contient aucun état à conserver."
                )
        return {
            "status": "partial",
            "space_id": space_id,
            "recovery_required": True,
            "message": message,
            "recovery": {
                "retry_safe": retry_safe,
                "action": recovery_action,
            },
        }

    async def _create_locked(
        self,
        storage,
        *,
        space_id: str,
        description: str,
        rules: str,
        owner: str,
        token_service,
        token_store,
        actor=None,
    ) -> dict:
        """Prépare puis commit un espace sous ses verrous de cycle de vie.

        Un préfixe sans ``_meta.json`` n'est repris que s'il contient un
        sous-ensemble exact de ``_rules.md``, ``live/.keep`` et ``bank/.keep``
        avec les octets attendus. Aucun rollback destructif n'est tenté : une
        ambiguïté devient ``partial/recovery_required``. Un préfixe absent
        avec un grant historique n'est jamais recréé. Une préparation exacte
        ne peut reprendre qu'avec zéro référence persistée dans ``space_ids`` :
        les scopes admin, révoqués ou expirés comptent aussi, car une transition
        de permissions peut les rendre actifs plus tard. Même le grant du
        manager acteur exige un nettoyage admin explicite avant le retry.
        """
        await assert_space_not_reserved(space_id)
        meta_key = f"{space_id}/_meta.json"
        expected_objects = {
            f"{space_id}/_rules.md": rules,
            f"{space_id}/live/.keep": "",
            f"{space_id}/bank/.keep": "",
        }

        committed_state, committed_reason = await self.classify_committed_state(
            storage, space_id
        )
        if committed_state == "unsafe":
            return self._partial_create(space_id, committed_reason)
        if committed_state == "committed":
            return {
                "status": "already_exists",
                "message": f"L'espace '{space_id}' existe déjà",
            }

        try:
            existing = await storage.list_objects(f"{space_id}/")
        except Exception:
            return self._partial_create(
                space_id,
                "Impossible de classer le préfixe existant ; aucune mutation effectuée.",
            )
        existing_keys = {str(obj.get("Key", "")) for obj in existing}
        unexpected = existing_keys - set(expected_objects)
        if unexpected:
            # Inclut explicitement tout ``_hivemind/`` : un état de coordination
            # sans commit marker ne doit jamais être assimilé à un space vierge.
            return self._partial_create(
                space_id,
                "Préfixe non committé contenant un état inattendu ou Hivemind.",
            )

        for key in existing_keys:
            try:
                content = await storage.get(key)
            except Exception:
                return self._partial_create(
                    space_id,
                    "Préfixe non committé illisible ; reprise refusée.",
                )
            if content != expected_objects[key]:
                return self._partial_create(
                    space_id,
                    "Préfixe non committé en conflit avec la requête.",
                )

        # ABA delete→recreate : un scope de token survit à space_delete. Sans
        # cette barrière, recréer le même identifiant réactiverait en silence
        # tous les anciens droits. Toute référence persistée compte, y compris
        # sur un token admin/révoqué/expiré : un downgrade, une réactivation ou
        # une correction d'expiration pourrait la rendre effective plus tard.
        scoped_tokens = [
            token
            for token in token_store.tokens
            if space_id in token.space_ids
        ]
        stale_scope_recovery = (
            f"Un admin doit retirer explicitement '{space_id}' des space_ids "
            "de tous les tokens signalés, y compris admin, révoqués ou "
            "expirés, puis retenter exactement la même création. "
            "Aucun rollback automatique."
        )
        if scoped_tokens:
            if existing_keys:
                message = (
                    "Reprise refusée : la préparation compatible porte encore "
                    "au moins une référence de scope persistée."
                )
            else:
                message = (
                    "Création refusée : des références de scope historiques "
                    "survivraient à une suppression/recréation de cet identifiant."
                )
            return self._partial_create(
                space_id,
                message,
                recovery_action=stale_scope_recovery,
            )

        post_grant_recovery = (
            f"Un admin doit d'abord inspecter '{meta_key}'. Si le marker est "
            f"absent ou non committé, il doit retirer explicitement '{space_id}' "
            "des space_ids de tous les tokens, puis retenter exactement "
            "la même création. Aucun rollback automatique."
        )

        # Reprise additive : seuls les objets préparatoires absents sont écrits.
        # Un échec ambigu est reprobed ; si l'objet exact n'est pas observable,
        # le commit s'arrête sans rollback.
        for key, content in expected_objects.items():
            if key in existing_keys:
                continue
            try:
                await storage.put(key, content)
            except Exception:
                try:
                    confirmed = await storage.get(key)
                except Exception:
                    confirmed = None
                if confirmed != content:
                    return self._partial_create(
                        space_id,
                        "Préparation S3 incomplète ; reprise requise.",
                        retry_safe=True,
                    )

        grant_added = False
        if actor is not None and "admin" not in set(actor.permissions or []):
            if space_id not in actor.space_ids:
                actor.space_ids.append(space_id)
                try:
                    await token_service._save_store(token_store)
                except Exception:
                    # Timeout après PUT possible : confirmer l'unique grant
                    # additif depuis S3 avant de poursuivre vers le marker.
                    try:
                        persisted = await token_service._load_store()
                        confirmed_actor = token_service._find_exact_token(
                            persisted, actor.hash
                        )
                    except Exception:
                        return self._partial_create(
                            space_id,
                            "Grant du manager illisible après un PUT ambigu ; "
                            "espace non committé.",
                            recovery_action=post_grant_recovery,
                        )
                    if confirmed_actor is None:
                        return self._partial_create(
                            space_id,
                            "Le token acteur a disparu pendant la création ; "
                            "espace non committé.",
                            recovery_action=post_grant_recovery,
                        )
                    if space_id not in confirmed_actor.space_ids:
                        return self._partial_create(
                            space_id,
                            "Grant du manager non confirmé ; espace non committé.",
                            retry_safe=True,
                        )
                grant_added = True

            # Même après un PUT sans exception, CREATED exige une observation
            # durable du grant nouvellement posé dans cet appel.
            try:
                persisted = await token_service._load_store()
                confirmed_actor = token_service._find_exact_token(
                    persisted, actor.hash
                )
            except Exception:
                confirmed_actor = None
            if confirmed_actor is None or space_id not in confirmed_actor.space_ids:
                return self._partial_create(
                    space_id,
                    "Grant du manager non observable ; espace non committé.",
                    recovery_action=post_grant_recovery,
                )

        now = datetime.now(timezone.utc).isoformat()
        meta = SpaceMeta(
            space_id=space_id,
            description=description,
            owner=owner,
            created_at=now,
        ).model_dump()
        try:
            await storage.put_json(meta_key, meta)
        except Exception:
            # Reprobe d'un PUT ambigu : _meta exact suffit à confirmer le commit.
            pass

        try:
            committed_meta = await storage.get_json(meta_key)
        except Exception:
            return self._partial_create(
                space_id,
                "Commit marker _meta.json illisible après un PUT ambigu.",
                recovery_action=post_grant_recovery if grant_added else "",
            )
        if committed_meta is None:
            return self._partial_create(
                space_id,
                "Commit marker _meta.json absent ; reprise requise.",
                retry_safe=not grant_added,
                recovery_action=post_grant_recovery if grant_added else "",
            )
        if committed_meta != meta:
            return self._partial_create(
                space_id,
                "Commit marker _meta.json conflictuel ; reprise automatique refusée.",
                recovery_action=post_grant_recovery if grant_added else "",
            )

        if grant_added:
            token_service._invalidate_in_fresh_store([actor.hash])

        if actor is not None:
            token_service._emit_delegated_access_audit(
                "space_create",
                caller=actor.name,
                details={
                    "actor_token_hash": actor.hash,
                    "space_id": space_id,
                    "auto_grant": grant_added,
                },
            )

        response = {
            "status": "created",
            "space_id": space_id,
            "description": description,
            "rules_size": len(rules.encode("utf-8")),
            "created_at": now,
        }
        if grant_added:
            response["token_auto_updated"] = True
        return response

    async def update(
        self,
        space_id: str,
        description: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> dict:
        """
        Met à jour les métadonnées d'un espace existant.

        Seuls les champs fournis (non-None) sont modifiés.
        Les rules restent immuables.

        Opérations S3 : GET _meta.json + PUT _meta.json

        Args:
            space_id: Identifiant de l'espace
            description: Nouvelle description (None = pas de changement)
            owner: Nouveau propriétaire (None = pas de changement)

        Returns:
            {"status": "ok", "space_id": ..., "updated_fields": [...]}
        """
        await assert_space_not_reserved(space_id)
        storage = get_storage()

        # Lire les métadonnées existantes
        meta = await storage.get_json(f"{space_id}/_meta.json")
        if meta is None:
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        # Appliquer les modifications
        updated_fields = []
        if description is not None:
            meta["description"] = description
            updated_fields.append("description")
        if owner is not None:
            meta["owner"] = owner
            updated_fields.append("owner")

        if not updated_fields:
            return {
                "status": "ok",
                "space_id": space_id,
                "message": "Aucun champ à modifier",
                "updated_fields": [],
            }

        # P5-8 (#16) ROUTE-FIRST: ``_meta.json`` carries SHARED metadata
        # (``description`` / ``owner`` are in core/models.SHARED_META_FIELDS), so
        # ADR-0007 puts the shared portion of ``_meta.json`` BEHIND the WriteSink
        # boundary. Route the durable write through the per-space sink exactly like
        # ``update_rules`` (#16 sibling), NEVER a direct ``storage.put_json`` for a
        # Hivemind space (that would be a single-writer bypass of the all-ACK
        # commit path). Non-Hivemind -> DirectLocalWriteSink: byte-identical legacy
        # ``put_json`` (same json.dumps(indent=2, ensure_ascii=False) +
        # application/json shape) + no-op commit. Hivemind hive ->
        # StagedHivemindWriteSink: ``_meta.json`` is OUTSIDE ``{space}/bank/``,
        # which the current CommitRuntime.apply_commit (promote-only-under-bank/)
        # cannot express, so commit() FAILS CLOSED (StagedWriteNotImplemented) — no
        # direct write, single-writer guarantee held, non-bank ``_meta.json``
        # staging deferred (same deferral as ``_rules.md``). UNSAFE/RESYNC ->
        # RegistryRefused; corrupt -> CorruptedStateError: both raise before any
        # write. All surface via tools/space.py::space_update's try/except
        # safe_error.
        from .engines import get_engine_registry

        sink = await get_engine_registry().resolve_sink(space_id)
        await sink.put_json(f"{space_id}/_meta.json", meta)
        await sink.commit(reason="space_update")

        return {
            "status": "ok",
            "space_id": space_id,
            "updated_fields": updated_fields,
            "description": meta.get("description", ""),
            "owner": meta.get("owner", ""),
        }

    async def update_rules(self, space_id: str, rules: str) -> dict:
        """
        Met à jour les rules d'un espace existant (admin only).

        ⚠️ Les rules sont normalement immuables. Cet outil permet de les
        mettre à jour sans devoir supprimer/recréer l'espace.

        Opérations S3 : GET _meta.json (vérif existence) + PUT _rules.md

        Args:
            space_id: Identifiant de l'espace
            rules: Nouveau contenu Markdown des rules

        Returns:
            {"status": "ok", "space_id": ..., "rules_size": N}
        """
        await assert_space_not_reserved(space_id)
        # Valider la taille
        if len(rules) > MAX_RULES_SIZE:
            return {
                "status": "error",
                "message": f"Rules trop longues ({len(rules)} chars, max {MAX_RULES_SIZE})",
            }

        if not rules.strip():
            return {
                "status": "error",
                "message": "Le contenu des rules ne peut pas être vide",
            }

        storage = get_storage()

        # Vérifier que l'espace existe
        if not await storage.exists(f"{space_id}/_meta.json"):
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        # P5-8 (#16) ROUTE-FIRST: resolve the per-space WriteSink BEFORE the
        # durable _rules.md write. Non-Hivemind -> DirectLocalWriteSink
        # (byte-identical legacy put with NO explicit content_type, so the
        # StorageService default applies) + no-op commit. On a Hivemind hive ->
        # StagedHivemindWriteSink: _rules.md is OUTSIDE {space}/bank/, which the
        # current CommitRuntime.apply_commit (promote-only-under-bank/) cannot
        # express, so commit() FAILS CLOSED (StagedWriteNotImplemented) — no
        # direct write, single-writer guarantee held, _rules.md staging deferred
        # to the #9 follow-up. UNSAFE/RESYNC -> RegistryRefused; corrupt ->
        # CorruptedStateError: both raise before any write. All surface via
        # tools/space.py::space_update_rules' existing try/except safe_error.
        from .engines import get_engine_registry

        sink = await get_engine_registry().resolve_sink(space_id)
        await sink.put(f"{space_id}/_rules.md", rules)
        await sink.commit(reason="update_rules")

        return {
            "status": "ok",
            "space_id": space_id,
            "rules_size": len(rules.encode("utf-8")),
            "message": f"Rules mises à jour ({len(rules.encode('utf-8'))} octets)",
        }

    async def list_spaces(self, allowed_space_ids: Optional[list[str]] = None) -> dict:
        """
        Liste tous les espaces accessibles.

        Opérations S3 : LIST préfixes racine + N GETs _meta.json

        Args:
            allowed_space_ids: Liste des space_ids autorisés (None = tous)

        Returns:
            {"status": "ok", "spaces": [...], "total": N}
        """
        storage = get_storage()

        # Lister les préfixes racine (chaque espace = un préfixe)
        prefixes = await storage.list_prefixes("")

        spaces = []
        for prefix in prefixes:
            # Exclure les préfixes système (_system/, _backups/)
            if prefix.startswith("_"):
                continue

            # Extraire le space_id (retirer le / final)
            sid = prefix.rstrip("/")

            # Filtrer par permissions du token
            if allowed_space_ids is not None and sid not in allowed_space_ids:
                continue

            # Lire les métadonnées
            meta = await storage.get_json(f"{sid}/_meta.json")
            if meta is None:
                continue  # Préfixe sans _meta.json → pas un espace valide

            # Compter les notes live et fichiers bank
            live_objects = await storage.list_objects(f"{sid}/live/")
            bank_objects = await storage.list_objects(f"{sid}/bank/")
            live_count = len(
                [o for o in live_objects if not o["Key"].endswith(".keep")]
            )
            bank_count = len(
                [o for o in bank_objects if not o["Key"].endswith(".keep")]
            )

            spaces.append(
                {
                    "space_id": sid,
                    "description": meta.get("description", ""),
                    "owner": meta.get("owner", ""),
                    "created_at": meta.get("created_at", ""),
                    "live_notes_count": live_count,
                    "bank_files_count": bank_count,
                }
            )

        return {"status": "ok", "spaces": spaces, "total": len(spaces)}

    async def get_info(self, space_id: str) -> dict:
        """
        Informations détaillées sur un espace.

        Opérations S3 : GET _meta.json + LIST live/* + LIST bank/*

        Args:
            space_id: Identifiant de l'espace

        Returns:
            {"status": "ok", "space_id": ..., "live": {...}, "bank": {...}}
        """
        storage = get_storage()

        # Lire les métadonnées
        meta = await storage.get_json(f"{space_id}/_meta.json")
        if meta is None:
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        # Stats des notes live
        live_objects = await storage.list_objects(f"{space_id}/live/")
        live_files = [o for o in live_objects if not o["Key"].endswith(".keep")]

        # Stats des fichiers bank
        bank_objects = await storage.list_objects(f"{space_id}/bank/")
        bank_files = [o for o in bank_objects if not o["Key"].endswith(".keep")]

        # Vérifier l'existence de la synthèse
        synthesis_exists = await storage.exists(f"{space_id}/_synthesis.md")

        from .consolidation_queue import get_consolidation_queue

        consolidation_queue = await get_consolidation_queue().get_space_summary(
            space_id
        )

        # P2-4 : label de statut unifié, fail-closed (corruption -> "unsafe").
        # Ajouté EN DERNIER et UNIQUEMENT sur le chemin succès (status==ok) :
        # le not_found early return ci-dessus garde sa forme 2-clés inchangée.
        hive_label = await _hive_status_label_failclosed(storage, space_id)

        return {
            "status": "ok",
            "space_id": space_id,
            "description": meta.get("description", ""),
            "owner": meta.get("owner", ""),
            "created_at": meta.get("created_at", ""),
            "live": {
                "notes_count": len(live_files),
                "total_size": sum(o["Size"] for o in live_files),
            },
            "bank": {
                "files_count": len(bank_files),
                "total_size": sum(o["Size"] for o in bank_files),
                "files": [bank_relpath(o["Key"], space_id) for o in bank_files],
            },
            "last_consolidation": meta.get("last_consolidation"),
            "consolidation_count": meta.get("consolidation_count", 0),
            "consolidation_queue": consolidation_queue,
            "synthesis_exists": synthesis_exists,
            "hive_status_label": hive_label,
        }

    async def get_rules(self, space_id: str) -> dict:
        """
        Lit les rules immuables de l'espace.

        Args:
            space_id: Identifiant de l'espace

        Returns:
            {"status": "ok", "rules": "..."} ou not_found
        """
        storage = get_storage()
        rules = await storage.get(f"{space_id}/_rules.md")
        if rules is None:
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        return {"status": "ok", "space_id": space_id, "rules": rules}

    async def get_summary(self, space_id: str) -> dict:
        """
        Synthèse complète : info + rules + bank. L'outil de démarrage des agents.

        Args:
            space_id: Identifiant de l'espace

        Returns:
            Dict combinant info, rules et contenu bank complet
        """
        storage = get_storage()

        # Lire meta + rules
        meta = await storage.get_json(f"{space_id}/_meta.json")
        if meta is None:
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        rules = await storage.get(f"{space_id}/_rules.md") or ""

        # Lire tous les fichiers bank
        bank_data = await storage.list_and_get(f"{space_id}/bank/")
        bank_files = [
            {
                "filename": bank_relpath(item["key"], space_id),
                "content": item["content"],
                "size": item["size"],
            }
            for item in bank_data
        ]

        # Lire la synthèse si elle existe
        synthesis = await storage.get(f"{space_id}/_synthesis.md")

        # P2-4 : label de statut unifié, fail-closed (corruption -> "unsafe").
        # Ajouté EN DERNIER et UNIQUEMENT sur le chemin succès (status==ok).
        hive_label = await _hive_status_label_failclosed(storage, space_id)

        return {
            "status": "ok",
            "space_id": space_id,
            "description": meta.get("description", ""),
            "rules": rules,
            "bank_files": bank_files,
            "bank_file_count": len(bank_files),
            "synthesis": synthesis,
            "hive_status_label": hive_label,
        }

    async def export_space(self, space_id: str) -> dict:
        """
        Exporte un espace complet en archive tar.gz (base64).

        LM2-03 fix : le ``_meta.json`` inclus dans l'archive est masqué
        (token Graph Memory remplacé par ``<prefix>...``) avant ajout
        au tar. L'archive téléchargée n'expose donc plus le secret.

        Args:
            space_id: Identifiant de l'espace

        Returns:
            {"status": "ok", "archive_base64": "...", "files_count": N}
        """
        import json as _json

        storage = get_storage()

        # Vérifier l'existence
        if not await storage.exists(f"{space_id}/_meta.json"):
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        # Lire tous les fichiers de l'espace
        all_objects = await storage.list_and_get(f"{space_id}/", exclude_keep=False)

        # Créer l'archive tar.gz en mémoire
        buf = io.BytesIO()
        meta_key = f"{space_id}/_meta.json"
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for obj in all_objects:
                # Nom relatif dans l'archive (sans le space_id/ prefix)
                arcname = obj["key"][len(space_id) + 1 :]
                content = obj["content"]

                # LM2-03 fix : masquer les secrets dans _meta.json avant export
                if obj["key"] == meta_key:
                    try:
                        meta_raw = _json.loads(content)
                        meta_masked = mask_meta_secrets(meta_raw)
                        content = _json.dumps(
                            meta_masked, indent=2, ensure_ascii=False
                        )
                    except (_json.JSONDecodeError, TypeError):
                        # RA-4 fix : fail-CLOSED, aligné sur backup_download.
                        # Un _meta.json illisible partait sinon EN CLAIR dans
                        # l'archive (token graph_memory compris) — best-effort
                        # fail-OPEN. On remplace le contenu non parsable par "{}"
                        # pour qu'aucun secret ne puisse jamais s'exporter.
                        content = "{}"

                data = content.encode("utf-8")
                info = tarfile.TarInfo(name=arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

        archive_bytes = buf.getvalue()

        # P2-4 : label de statut unifié, fail-closed (corruption -> "unsafe").
        # Ajouté EN DERNIER et UNIQUEMENT sur le chemin succès (status==ok).
        hive_label = await _hive_status_label_failclosed(storage, space_id)

        return {
            "status": "ok",
            "space_id": space_id,
            "archive_base64": base64.b64encode(archive_bytes).decode("ascii"),
            "archive_size": len(archive_bytes),
            "files_count": len(all_objects),
            "hive_status_label": hive_label,
        }

    async def delete(self, space_id: str, unsafe_recovery: bool = False) -> dict:
        """Sérialise la suppression avec la création/reprise du même espace."""
        async with get_lock_manager().space_lifecycle(space_id):
            return await self._delete_locked(
                space_id, unsafe_recovery=unsafe_recovery
            )

    async def _delete_locked(
        self, space_id: str, unsafe_recovery: bool = False
    ) -> dict:
        """
        Supprime un espace et TOUTES ses données (irréversible).

        HM-10 fix : garde Hivemind refus-par-défaut, symétrique de
        ``BackupService.restore`` (P2-5 / ADR-0014). ``delete`` supprime tout le
        préfixe ``{space}/`` — y compris le sous-arbre ``_hivemind/`` (membership,
        lease, term, commits, tombstones, watermarks). Détruire cet état
        unilatéralement (hors chorégraphie de commit) fait basculer les autres
        pairs du mesh en ``unsafe``/``resync_required``. On REFUSE donc par défaut
        la suppression d'un espace Hivemind partagé/unsafe, sauf
        ``unsafe_recovery=True``. FAIL-CLOSED : état non classable → refus.

        Args:
            space_id: Identifiant de l'espace
            unsafe_recovery: autorise la suppression d'un space Hivemind
                partagé/unsafe (refus par défaut ; voir ADR-0014)

        Le commit marker ``_meta.json`` est supprimé EN DERNIER. Chaque objet
        est reprobed après DELETE ; une suppression non confirmée retourne un
        ``partial/recovery_required`` honnête et le marker reste en place tant
        qu'un payload subsiste.

        Returns:
            ``{"status": "deleted", "files_deleted": N}`` si chaque clé est
            confirmée absente, sinon un retour ``partial`` avec les clés non
            confirmées et une action de reprise explicite.
        """
        await assert_space_not_reserved(space_id)
        storage = get_storage()
        meta_key = f"{space_id}/_meta.json"

        def partial_delete(
            message: str,
            *,
            files_total: int,
            files_deleted: int,
            failed_keys: list[str],
            marker_preserved: bool | None,
        ) -> dict:
            if marker_preserved is True:
                action = (
                    "Retentez space_delete avec le même space_id : le commit "
                    "marker est conservé et seuls les objets encore présents "
                    "seront supprimés. Ne recréez pas l'espace entre-temps."
                )
            elif marker_preserved is False:
                action = (
                    f"Un admin doit inspecter le préfixe exact '{space_id}/' ; "
                    "le commit marker est absent. Ne recréez pas cet identifiant "
                    "et ne supprimez aucun résidu sans confirmer son origine."
                )
            else:
                action = (
                    "Retentez space_delete avec le même space_id. Si le marker "
                    "est déjà absent, vérifiez que le préfixe est vide avant "
                    "de considérer la suppression terminée."
                )
            return {
                "status": "partial",
                "space_id": space_id,
                "recovery_required": True,
                "message": message,
                "files_total": files_total,
                "files_deleted": files_deleted,
                "failed_keys": sorted(set(failed_keys)),
                "marker_preserved": marker_preserved,
                "recovery": {
                    "retry_safe": marker_preserved is not False,
                    "action": action,
                },
            }

        async def delete_and_confirm(key: str) -> bool | None:
            """True=absent confirmé, False=encore présent, None=illisible."""
            try:
                await storage.delete(key)
            except Exception:
                # Timeout après DELETE possible : seule la reprobe décide.
                pass
            try:
                return not await storage.exists(key)
            except Exception:
                return None

        # Vérifier l'existence
        try:
            marker_exists = await storage.exists(meta_key)
        except Exception:
            return partial_delete(
                "Impossible de confirmer l'existence du commit marker.",
                files_total=0,
                files_deleted=0,
                failed_keys=[meta_key],
                marker_preserved=None,
            )
        if not marker_exists:
            try:
                residual = await storage.list_objects(f"{space_id}/")
            except Exception:
                return partial_delete(
                    "Commit marker absent mais préfixe impossible à vérifier.",
                    files_total=0,
                    files_deleted=0,
                    failed_keys=[meta_key],
                    marker_preserved=False,
                )
            residual_keys = [str(item.get("Key", "")) for item in residual]
            if residual_keys:
                return partial_delete(
                    "Commit marker absent avec des objets résiduels ; nettoyage "
                    "automatique refusé.",
                    files_total=len(residual_keys),
                    files_deleted=0,
                    failed_keys=residual_keys,
                    marker_preserved=False,
                )
            return {
                "status": "not_found",
                "message": f"Espace '{space_id}' introuvable",
            }

        # ── Garde Hivemind refus-par-défaut (HM-10, symétrique de restore) ──
        try:
            label = await hive_status_label(storage, space_id)
        except CorruptedStateError:
            return {
                "status": "error",
                "message": (
                    f"Suppression refusée : l'état de coordination Hivemind de "
                    f"'{space_id}' est corrompu / illisible. Fail-closed : refus "
                    f"quel que soit unsafe_recovery (impossible de classer la "
                    f"cible en toute sécurité). Voir ADR-0014."
                ),
            }

        if (
            label in ("hivemind_healthy", "hivemind_blocked", "unsafe", "resync_required")
            and not unsafe_recovery
        ):
            return {
                "status": "error",
                "message": (
                    f"Suppression refusée : '{space_id}' est un space Hivemind "
                    f"partagé/unsafe (label='{label}'). Supprimer effacerait "
                    f"l'état de coordination Project Mesh et ferait basculer les "
                    f"autres pairs en resync/unsafe. Passez unsafe_recovery=True "
                    f"pour une suppression EXPLICITE unsafe. Voir ADR-0014."
                ),
            }

        # Lister puis dédupliquer tous les objets observés. Le marker est
        # ajouté explicitement si LIST ne le renvoie pas malgré le HEAD initial.
        try:
            all_objects = await storage.list_objects(f"{space_id}/")
        except Exception:
            return partial_delete(
                "Impossible de lister les objets à supprimer.",
                files_total=1,
                files_deleted=0,
                failed_keys=[],
                marker_preserved=True,
            )
        all_keys = list(
            dict.fromkeys(
                [str(item.get("Key", "")) for item in all_objects] + [meta_key]
            )
        )
        payload_keys = [key for key in all_keys if key and key != meta_key]
        files_total = len(payload_keys) + 1
        files_deleted = 0
        failed_payload: list[str] = []

        # Phase 1 : payload. Le marker n'est JAMAIS touché si une seule clé
        # reste présente ou illisible.
        for key in payload_keys:
            confirmed_absent = await delete_and_confirm(key)
            if confirmed_absent is True:
                files_deleted += 1
            else:
                failed_payload.append(key)
        if failed_payload:
            return partial_delete(
                "Suppression payload incomplète ; commit marker conservé.",
                files_total=files_total,
                files_deleted=files_deleted,
                failed_keys=failed_payload,
                marker_preserved=True,
            )

        # Re-LIST avant le marker : capte un objet non présent dans le snapshot
        # initial ou apparu concurremment. Il sera traité lors d'un retry.
        try:
            remaining = await storage.list_objects(f"{space_id}/")
        except Exception:
            return partial_delete(
                "Payload supprimé mais préfixe final impossible à confirmer ; "
                "commit marker conservé.",
                files_total=files_total,
                files_deleted=files_deleted,
                failed_keys=[],
                marker_preserved=True,
            )
        remaining_payload = [
            str(item.get("Key", ""))
            for item in remaining
            if str(item.get("Key", "")) not in ("", meta_key)
        ]
        if remaining_payload:
            return partial_delete(
                "Objets payload encore présents ; commit marker conservé.",
                files_total=max(files_total, files_deleted + len(remaining_payload) + 1),
                files_deleted=files_deleted,
                failed_keys=remaining_payload,
                marker_preserved=True,
            )

        # Phase 2 : publication de la suppression en retirant le marker EN
        # DERNIER. Timeout post-DELETE confirmé absent = succès ; toute
        # observation présente/illisible reste partial.
        marker_absent = await delete_and_confirm(meta_key)
        if marker_absent is not True:
            return partial_delete(
                "Payload supprimé mais suppression du commit marker non confirmée.",
                files_total=files_total,
                files_deleted=files_deleted,
                failed_keys=[meta_key],
                marker_preserved=True if marker_absent is False else None,
            )
        files_deleted += 1

        return {
            "status": "deleted",
            "space_id": space_id,
            "files_deleted": files_deleted,
            "files_total": files_total,
        }


# ─────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────

_space_service: SpaceService | None = None


def get_space_service() -> SpaceService:
    """Retourne le singleton SpaceService."""
    global _space_service
    if _space_service is None:
        _space_service = SpaceService()
    return _space_service
