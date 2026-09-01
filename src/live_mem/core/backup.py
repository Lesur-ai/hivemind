# -*- coding: utf-8 -*-
"""
Service Backup — Sauvegarde et restauration d'espaces.

Les backups sont des snapshots complets stockés dans _backups/ sur S3.
Chaque backup copie tous les fichiers d'un espace dans un sous-dossier
horodaté.

Architecture :
    tools/backup.py → BackupService (ce fichier) → StorageService (S3)

Arborescence S3 :
    _backups/{space_id}/{timestamp}/
        ├── _meta.json
        ├── _rules.md
        ├── _synthesis.md
        ├── bank/...
        └── live/...

Voir S3_DATA_MODEL.md pour les détails.
"""

import base64
import io
import json as _json
import logging
import tarfile
import uuid
from datetime import datetime, timezone

from .storage import get_storage, inventory_object_size
from .locks import get_lock_manager
from .models import mask_meta_secrets
from .reservation_guard import (
    NotMembershipLeaderError,
    PairingActivationError,
    assert_direct_local_allowed,
    assert_membership_recovery_leader,
    assert_no_pairing_activation,
    assert_space_not_reserved,
)
from .hivemind import (
    BankVersionPointer,
    CommitIntent,
    CommitNotAuthorized,
    CommitRuntime,
    CorruptedStateError,
    EventEnvelope,
    EventType,
    HiveNodeStatus,
    HivemindStateStore,
    LeaseRuntime,
    MembershipService,
    MembershipView,
    Member,
    NodeHealth,
    NodeIdentity,
    QueueRuntime,
    TermState,
    TokenLeaseState,
    TokenState,
    Tombstone,
    hive_status_label,
    note_id_from_key,
)
from .hivemind.lease_runtime import (
    assert_active_lease_structural,
    assert_active_token_term_consistent,
)
from pydantic import ValidationError

logger = logging.getLogger("live_mem.core.backup")


class BackupService:
    """
    Service de sauvegarde et restauration d'espaces mémoire.
    """

    async def create(
        self,
        space_id: str,
        description: str = "",
        *,
        operation_id: str | None = None,
        storage=None,
    ) -> dict:
        """
        Crée un snapshot complet de l'espace sur S3.

        Copie tous les fichiers de {space_id}/ vers _backups/{space_id}/{timestamp}/.

        Args:
            space_id: Espace à sauvegarder
            description: Description du backup (optionnel)
            operation_id: Internal opaque 32-character lower-hex suffix used
                when one caller needs collision-resistant same-second backup
                identity. Public backup calls retain the timestamp-only form.
            storage: Internal injected storage view; defaults to the normal
                process storage service for public backup operations.

        Returns:
            {"status": "created", "backup_id": "...", ...}
        """
        storage = storage if storage is not None else get_storage()

        # Vérifier l'existence de l'espace
        if not await storage.exists(f"{space_id}/_meta.json"):
            return {
                "status": "not_found",
                "message": f"Space '{space_id}' not found",
            }

        # Générer le timestamp pour le backup. The historical public form
        # remains ``space/timestamp``. A caller that must retain a distinct
        # same-second preimage can append one opaque operation id without
        # changing the existing backup layout or restore primitive.
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H-%M-%S")
        if operation_id is not None:
            if (
                type(operation_id) is not str
                or len(operation_id) != 32
                or any(character not in "0123456789abcdef" for character in operation_id)
            ):
                raise ValueError("operation_id must be 32 lowercase hexadecimal characters")
            ts = f"{ts}-{operation_id}"
        backup_prefix = f"_backups/{space_id}/{ts}/"
        backup_id = f"{space_id}/{ts}"

        # Lister et copier tous les fichiers
        objects = await storage.list_objects(f"{space_id}/")
        # Preflight the COMPLETE inventory before the first copy.  Otherwise a
        # late missing/malformed Size would leave a partial backup prefix and
        # only then fail during arithmetic.
        total_size = sum(inventory_object_size(obj) for obj in objects)

        for obj in objects:
            source_key = obj["Key"]
            # Chemin relatif dans l'espace
            relative = source_key[len(space_id) + 1 :]
            dest_key = backup_prefix + relative

            await storage.copy_object(source_key, dest_key)

        # Store backup description in the copied _meta.json (best-effort)
        if description:
            try:
                meta_key = f"{backup_prefix}_meta.json"
                meta = await storage.get_json(meta_key)
                if meta:
                    meta["backup_description"] = description
                    await storage.put_json(meta_key, meta)
            except Exception:
                pass  # non-blocking

        return {
            "status": "created",
            "backup_id": backup_id,
            "space_id": space_id,
            "timestamp": now.isoformat(),
            "description": description,
            "files_backed_up": len(objects),
            "total_size": total_size,
        }

    async def create_all(self, description: str = "") -> dict:
        """
        Crée un snapshot de TOUS les espaces (admin only).

        Liste tous les espaces existants et crée un backup pour chacun.
        Les erreurs sur un espace n'empêchent pas le backup des suivants.

        Args:
            description: Description commune pour tous les backups

        Returns:
            {"status": "ok", "spaces_backed_up": N, "spaces_failed": N, "details": [...]}
        """
        storage = get_storage()

        # Lister tous les espaces (préfixes de premier niveau avec _meta.json)
        # On utilise list_prefixes pour trouver les espaces
        all_prefixes = await storage.list_prefixes("", delimiter="/")

        # Filtrer : garder seulement les espaces réels (pas _backups, _system)
        space_ids = []
        for prefix in all_prefixes:
            sid = prefix.rstrip("/")
            if sid.startswith("_"):
                continue
            # Vérifier que c'est un vrai espace (a un _meta.json)
            if await storage.exists(f"{sid}/_meta.json"):
                space_ids.append(sid)

        if not space_ids:
            return {
                "status": "ok",
                "message": "No spaces found",
                "spaces_backed_up": 0,
                "spaces_failed": 0,
                "details": [],
            }

        # Backup chaque espace
        details = []
        spaces_ok = 0
        spaces_failed = 0

        for sid in sorted(space_ids):
            try:
                result = await self.create(sid, description)
                if result.get("status") == "created":
                    spaces_ok += 1
                    details.append(
                        {
                            "space_id": sid,
                            "status": "created",
                            "backup_id": result.get("backup_id", ""),
                            "files": result.get("files_backed_up", 0),
                            "size": result.get("total_size", 0),
                        }
                    )
                else:
                    spaces_failed += 1
                    details.append(
                        {
                            "space_id": sid,
                            "status": "error",
                            "message": result.get("message", "?"),
                        }
                    )
            except Exception as e:
                spaces_failed += 1
                details.append(
                    {
                        "space_id": sid,
                        "status": "error",
                        "message": str(e),
                    }
                )

        return {
            "status": "ok",
            "spaces_total": len(space_ids),
            "spaces_backed_up": spaces_ok,
            "spaces_failed": spaces_failed,
            "details": details,
        }

    async def list_backups(self, space_id: str = "") -> dict:
        """
        Liste les backups disponibles.

        Args:
            space_id: Filtrer par espace (vide = tous)

        Returns:
            {"status": "ok", "backups": [...], "total": N}
        """
        storage = get_storage()

        prefix = f"_backups/{space_id}/" if space_id else "_backups/"
        prefixes = await storage.list_prefixes(prefix, delimiter="/")

        raw_entries = []
        if space_id:
            # Lister les timestamps pour cet espace
            for p in prefixes:
                parts = p.rstrip("/").split("/")
                ts = parts[-1] if len(parts) >= 3 else "?"
                raw_entries.append((space_id, ts, p))
        else:
            # Lister les espaces qui ont des backups
            space_prefixes = await storage.list_prefixes("_backups/", delimiter="/")
            for sp in space_prefixes:
                sid = sp.rstrip("/").split("/")[-1]
                ts_prefixes = await storage.list_prefixes(sp, delimiter="/")
                for tp in ts_prefixes:
                    ts = tp.rstrip("/").split("/")[-1]
                    raw_entries.append((sid, ts, tp))

        # Enrich each backup with metadata from _meta.json (best-effort)
        backups = []
        for sid, ts, bprefix in raw_entries:
            entry = {
                "backup_id": f"{sid}/{ts}",
                "space_id": sid,
                "timestamp": ts,
            }
            # Try to read _meta.json to get description, files count, size
            try:
                meta_key = f"{bprefix}_meta.json"
                meta = await storage.get_json(meta_key)
                if meta:
                    if meta.get("backup_description"):
                        entry["description"] = meta["backup_description"]
                    # Count files in the backup prefix
                    objs = await storage.list_objects(bprefix)
                    entry["files_count"] = len(objs)
                    entry["total_size"] = sum(
                        inventory_object_size(o, missing_as_zero=True)
                        for o in objs
                    )
            except Exception:
                pass  # best-effort enrichment

            backups.append(entry)

        return {"status": "ok", "backups": backups, "total": len(backups)}

    async def restore(self, backup_id: str, unsafe_recovery: bool = False) -> dict:
        """Restore while serialized with create/delete/source preparation."""

        parts = backup_id.split("/", 1)
        if len(parts) != 2:
            return {
                "status": "error",
                "message": "Invalid backup_id (format: space_id/timestamp)",
            }
        space_id, _timestamp = parts
        async with get_lock_manager().space_lifecycle(space_id):
            return await self._restore_locked(
                backup_id, unsafe_recovery=unsafe_recovery
            )

    async def _restore_locked(
        self, backup_id: str, unsafe_recovery: bool = False
    ) -> dict:
        """
        Restaure un espace depuis un backup.

        L'espace NE DOIT PAS exister par défaut (supprimer d'abord), SAUF en
        recovery EXPLICITE unsafe (``unsafe_recovery=True``) sur un space marqué
        Hivemind, où la chorégraphie de forçage-en-avant prend la main.

        Garde Hivemind refus-par-défaut (P2-5 / issue #37, ADR-0014 Accepted) :
        avant la copie, on classe l'espace cible via ``hive_status_label``
        (détection READ-ONLY, ADR-0008). Si le label est partagé/unsafe
        (``hivemind_healthy`` / ``hivemind_blocked`` / ``unsafe`` /
        ``resync_required``), la restauration PAR-DESSUS est REFUSÉE avec une
        erreur bloquante hive-aware, SAUF si ``unsafe_recovery=True``.

        ``unsafe_recovery=True`` (P6-1 / issue #87, ADR-0014) : sur un label
        Hivemind, la restauration emprunte le chemin de FORÇAGE-EN-AVANT
        champ-par-champ (membership_epoch / term / token / bank_version / queue
        / acks / watermarks / tombstones), AVEC publication via
        ``CommitRuntime.stage_commit`` + ``CommitRuntime.apply_commit`` (gates
        G0-G3, journal-first, flip-pointeur-last, release convergent du token).
        Le check hérité « ``{space}/_meta.json`` existe -> refus » est SAUTÉ
        car ce chemin OWNS l'overwrite via la chorégraphie. Pour
        ``local_only`` / ``not_a_space`` (et lorsque ``unsafe_recovery=False``
        sur un label Hivemind, qui retourne plus haut), le check hérité reste
        appliqué octet-pour-octet.

        FAIL-CLOSED durs (aucune mutation, aucun event) :

        - ``CorruptedStateError`` (node/members/node_status.json) — REFUS quel
          que soit ``unsafe_recovery`` (on ne peut pas classer la cible).
        - ORPHELIN : ``NodeIdentity`` absente sous ``unsafe_recovery=True`` —
          REFUS avec instruction de bootstrap. Forcer un commit avec un
          ``origin_node_id`` vide casserait le journal d'audit.
        - PRÉCONDITION : ``backup.bank_version > live.bank_version`` — REFUS
          (rebuild_pointer ou restore sur un nœud vierge). Sinon le CAS du
          commit serait piloté par un parent stale.

        Args:
            backup_id: Format "space_id/timestamp"
            unsafe_recovery: Recovery EXPLICITE unsafe — autorise la
                restauration par-dessus un space Hivemind partagé/unsafe et
                déclenche la chorégraphie de forçage-en-avant (refus par
                défaut). N'autorise PAS de passer outre la corruption
                (fail-closed). Voir ADR-0014.

        Returns:
            {"status": "ok", "files_restored": N, ...}
            ou {"status": "error", ...} si refusée (garde Hivemind / hérité).
        """
        storage = get_storage()

        # Parser le backup_id
        parts = backup_id.split("/", 1)
        if len(parts) != 2:
            return {
                "status": "error",
                "message": "Invalid backup_id (format: space_id/timestamp)",
            }

        space_id, timestamp = parts
        await assert_space_not_reserved(space_id)
        backup_prefix = f"_backups/{space_id}/{timestamp}/"

        # Vérifier que le backup existe
        backup_objects = await storage.list_objects(backup_prefix)
        if not backup_objects:
            return {
                "status": "not_found",
                "message": f"Backup '{backup_id}' not found",
            }
        # Every restore copy source must have usable inventory metadata.  Run
        # the full proof before target classification or any restore mutation.
        for obj in backup_objects:
            inventory_object_size(obj)

        # ── Garde Hivemind refus-par-défaut (P2-5 / #37 ; ADR-0014 Accepted) ──
        # Détection READ-ONLY (ADR-0008) AVANT le check hérité _meta.json. La
        # garde n'écrit RIEN (aucun put/delete sous _hivemind/) et ne force
        # AUCUN champ : le forçage-en-avant champ-par-champ + audit +
        # assert_commit_allowed restent déférés à #8/#9 (ADR-0014 l.160-164,
        # PORTING_PLAN « Open Design Gap », issue #9).
        try:
            label = await hive_status_label(storage, space_id)
        except CorruptedStateError:
            # FAIL-CLOSED : cible non classable -> refus, même avec
            # unsafe_recovery (on ne peut pas savoir sur quel état il
            # s'appliquerait). Jamais de copie sur un état corrompu illisible.
            return {
                "status": "error",
                "message": (
                    f"Restore refused: Hivemind coordination state for '{space_id}' "
                    "is corrupt or unreadable. Fail-closed: restore is refused "
                    "regardless of unsafe_recovery because the target cannot be "
                    "classified safely. See ADR-0014 and issue #9."
                ),
            }

        hivemind_labels = (
            "hivemind_healthy",
            "hivemind_blocked",
            "unsafe",
            "resync_required",
        )

        if label in hivemind_labels and not unsafe_recovery:
            return {
                "status": "error",
                "message": (
                    f"Restore refused: '{space_id}' is a shared or unsafe Hivemind "
                    f"space (label='{label}'). Restoring over it would overwrite "
                    "Project Mesh coordination state. Pass unsafe_recovery=True for "
                    "an EXPLICIT unsafe recovery using field-by-field forward forcing "
                    "through CommitRuntime. See ADR-0014 and issue #87."
                ),
            }

        if label in hivemind_labels and unsafe_recovery:
            # Forçage-en-avant champ-par-champ (P6-1 / issue #87, ADR-0014). On
            # SAUTE le check hérité _meta.json-exists : ce chemin OWNS l'overwrite
            # via la chorégraphie de commit. Toutes les vérifications structurelles
            # de précondition (corruption déjà testée plus haut, orphelin et
            # backup_pointer > live_pointer testés à l'intérieur du helper) sont
            # fail-closed AVANT toute mutation visible.
            return await self._restore_unsafe_recovery(
                space_id=space_id,
                backup_id=backup_id,
                backup_prefix=backup_prefix,
                backup_objects=backup_objects,
                hive_status_label=label,
            )

        # Pour local_only / not_a_space, on retombe sur le chemin HÉRITÉ inchangé.
        # A durable source-preparation provenance record permanently removes
        # DIRECT_LOCAL authority even when the Hivemind prefix was lost. The
        # explicit shared-Hivemind recovery branch above remains unaffected.
        try:
            await assert_direct_local_allowed(space_id)
        except Exception:
            return {
                "status": "error",
                "message": (
                    f"Restore refused: '{space_id}' has irreversible Project Mesh "
                    "source provenance and cannot be restored through the local path."
                ),
            }

        # Vérifier que l'espace N'existe PAS
        if await storage.exists(f"{space_id}/_meta.json"):
            return {
                "status": "error",
                "message": f"Space '{space_id}' already exists. Delete it first.",
            }

        # Copier tous les fichiers du backup vers l'espace
        for obj in backup_objects:
            source_key = obj["Key"]
            relative = source_key[len(backup_prefix) :]
            dest_key = f"{space_id}/{relative}"
            await storage.copy_object(source_key, dest_key)

        return {
            "status": "ok",
            "backup_id": backup_id,
            "space_id": space_id,
            "files_restored": len(backup_objects),
        }

    # =====================================================================
    # P6-1 (issue #87, ADR-0014) — forçage-en-avant champ-par-champ
    # =====================================================================

    async def _restore_unsafe_recovery(
        self,
        *,
        space_id: str,
        backup_id: str,
        backup_prefix: str,
        backup_objects: list,
        hive_status_label: str,
    ) -> dict:
        """
        Chemin ``unsafe_recovery=True`` sur un label Hivemind (ADR-0014).

        Forge l'état de coordination EN AVANT plutôt que d'écraser l'existant :
        ``membership_epoch`` / ``term`` / ``token`` / ``bank_version`` montent
        TOUS strictement (max(live, backup)+1 pour epoch/term, pointer+1 pour
        la version de bank). Tombstones UNION (jamais de perte). Queue dropée,
        ``acks/`` purgé, ``watermarks/`` prunés à la nouvelle ``MembershipView``.

        Publication via ``CommitRuntime.stage_commit`` + ``CommitRuntime.apply_commit``
        (gates G0-G3, journal-first, flip-pointeur-last, release convergent du
        token). Le ``CommitIntent`` matche le nouveau ``term`` / pointer+1 ; le
        token est armé HELD au nouveau term juste avant l'apply pour que
        ``assert_commit_allowed`` (point d'autorisation unique ADR-0011) passe.

        Audit : ``UNSAFE_RECOVERY_RESTORED`` (operator-confirmed forward-
        forcing) + ``RESYNC_REQUIRED`` (invitation aux peers). Le node passe
        ``HiveNodeStatus.RESYNC_REQUIRED`` : ``RecoveryTriggers.request_resync``
        est BYPASSED (le resolver UNSAFE refuserait précisément le restore
        légitime ; ``operator + confirm`` upstream a déjà rempli le rôle de
        recovery audité).
        """
        storage = get_storage()
        store = HivemindStateStore(storage, space_id)

        # ---- 1. Lire l'état live (pointeur, membership, term, tombstones, +
        # token/queue/watermark : Codex P6-1 medium #4 — toute corruption
        # de l'état protocolaire LIVE doit faire échouer le préflight AVANT
        # le marker RESYNC_REQUIRED, sinon une corruption tardive (touchée
        # après les premiers writes) cassait au milieu de la chorégraphie).
        try:
            live_pointer = await store.get_bank_version_pointer()
            live_membership = await store.get_membership()
            live_term = await store.get_term()
            live_tombstones = await store.list_tombstones()
            local_node = await store.get_node_identity()
            # Probe complémentaire : token / queue / watermark local.
            # Chaque accès traverse `_get_model` qui lève `CorruptedStateError`
            # si le JSON est invalide ou le schéma Pydantic refuse.
            live_token = await store.get_token()
            _ = await store.list_queue()
            if local_node is not None and local_node.node_id:
                _ = await store.get_watermark(local_node.node_id)
            # Codex R2 high #2 : le préflight token DOIT exercer les MÊMES
            # gardes fail-closed que ``lease_runtime`` sur un token ACTIF
            # (HELD/RELEASING) ; ``get_token()`` seul ne prouve QUE que le
            # JSON s'aligne avec ``TokenLeaseState`` (les ``Optional`` du
            # modèle laissent passer un HELD sans ``lease_until`` ou sans
            # ``holder_node_id``, qui ``is_lease_expired`` traite déjà comme
            # CORROMPU). Sans cette validation sémantique, un token
            # structurellement-valide-mais-cassé passerait le préflight, le
            # marker RESYNC_REQUIRED serait écrit, et la chorégraphie
            # casserait plus loin (set_token monotone, apply_commit) en
            # laissant le live à moitié forçé. On exécute donc les MÊMES
            # gardes que ``release`` / ``reconcile_stale_holder`` —
            # ``assert_active_lease_structural`` (HELD sans lease_until /
            # holderless) ET ``assert_active_token_term_consistent``
            # (token.term > live term.term ; HELD sans term.json) — AVANT
            # toute mutation durable.
            if live_token is not None:
                now = datetime.now(timezone.utc)
                assert_active_lease_structural(live_token, now)
                assert_active_token_term_consistent(live_token, live_term)
        except CorruptedStateError as exc:
            # Défense en profondeur : `hive_status_label` a déjà couvert
            # node/members/node_status.json ; ici les autres fichiers
            # critiques (pointer/term/queue/tombstones/token/watermark)
            # sont aussi fail-closed AVANT toute mutation durable
            # (notamment AVANT le marker RESYNC_REQUIRED).
            return {
                "status": "error",
                "message": (
                    f"Restore refused: critical Hivemind state is corrupt ({exc}). "
                    "Fail-closed; see ADR-0014."
                ),
            }

        # ---- 2. ORPHELIN : NodeIdentity absente -> refus fail-closed ----
        # On NE peut PAS forger un commit ni un event d'audit sans
        # ``origin_node_id`` non vide. Bootstrap d'une identité (space_create
        # ou import bootstrap) requis avant le restore.
        if local_node is None or not local_node.node_id:
            return {
                "status": "error",
                "message": (
                    f"Restore refused: '{space_id}' has no local NodeIdentity. "
                    "Bootstrap a node identity before unsafe_recovery; otherwise the "
                    "audit log and BankCommit would have an empty origin_node_id. "
                    "See ADR-0014 and issue #87."
                ),
            }

        # ---- 3. Lire l'état du backup (epoch / term / pointer / tombstones) ----
        # Codex P6-1 high #3 : fail-closed sur tout fichier critique du backup
        # malformé (node.json / members.json / term.json / token.json /
        # bank_version.json / node_status.json / tombstones/*). Pas de defaults
        # silencieux, pas de skip d'un tombstone malformé. Le refus a lieu
        # AVANT le marker RESYNC_REQUIRED : un backup corrompu NE doit PAS
        # taguer le node live comme unsafe.
        try:
            backup_epoch, backup_term, backup_pointer_version, backup_tombs = (
                await self._read_backup_hivemind_state(backup_prefix)
            )
        except CorruptedStateError as exc:
            return {
                "status": "error",
                "message": (
                    f"Restore refused: backup '{backup_id}' contains corrupt "
                    f"Hivemind state ({exc}). Fail-closed before the RESYNC_REQUIRED "
                    "marker, so the live node classification remains unchanged. "
                    "See ADR-0014."
                ),
            }

        live_pointer_version = (
            live_pointer.bank_version if live_pointer is not None else -1
        )
        live_epoch = live_membership.epoch if live_membership is not None else 0
        live_term_val = live_term.term if live_term is not None else 0

        # ---- 4. PRÉCONDITION (AVANT toute mutation) : backup_pointer > live ----
        # Le forçage-en-avant pose un nouveau commit à ``live_pointer+1`` qui
        # MATÉRIALISE le contenu bank stagé du backup. Si la ``bank_version``
        # déclarée par le backup est STRICTEMENT supérieure au pointeur live,
        # ce nouveau commit aurait une ``bank_version`` (= live+1) inférieure
        # à la content-version que le backup décrit (= backup_pointer) — un
        # mismatch durable entre la version du commit et la version du
        # contenu staged. Forward-forcing exige content-version <= live+1,
        # donc on REFUSE fail-closed AVANT toute mutation (le
        # ``live_pointer_version`` utilisé pour la comparaison ci-dessous est
        # une valeur LOCALE de défaut -1 si absent, jamais persistée).
        if backup_pointer_version > live_pointer_version:
            return {
                "status": "error",
                "message": (
                    f"Restore refused: backup bank_version ({backup_pointer_version}) "
                    f"is GREATER than the live pointer ({live_pointer_version}). "
                    "The precondition is violated. Run rebuild_pointer_from_commits "
                    "first, or restore on a fresh node. See ADR-0014 and issue #87."
                ),
            }

        # ---- 4ter. GARDE LEADER (cross-process) : cette recovery remplace la
        # membership hors de l'autorité Mesh. La membership Mesh a un writer UNIQUE
        # (le leader élu par flock ; les routes/admin Mesh rejettent déjà les
        # non-leaders). Un non-leader qui écrirait ici pourrait courir la promotion
        # d'un pairing du leader (le lock membership est in-process, pas
        # cross-process) -> overwrite same-epoch = split indétectable. On REFUSE donc
        # sur un non-leader (fail-closed) ; sur le leader, son lock in-process
        # sérialise ce write avec les promotions. No-op quand Mesh est désactivé.
        try:
            await assert_membership_recovery_leader(space_id)
        except NotMembershipLeaderError as exc:
            return {"status": "error", "message": str(exc)}
        # FENCE : refuser si un pairing Mesh SOURCE est en cours d'activation. Cette
        # recovery remplace la membership par [self] à un epoch bumpé (les peers se
        # ré-enrôlent après resync) : si un pairing avait promu une cible à e+2 dont
        # l'activation est encore en vol, on la larguerait côté source tandis que la
        # cible s'auto-promeut -> split. Contrôle AVANT toute mutation (refus
        # propre), puis RE-contrôle atomique sous le lock membership au write du
        # roster ci-dessous. No-op quand Mesh est désactivé. Ce n'est PAS un pairing
        # (ignore_pair_id=None) : fencé contre TOUT pairing en vol.
        try:
            await assert_no_pairing_activation(space_id)
        except PairingActivationError as exc:
            return {"status": "error", "message": str(exc)}

        # ---- 4bis. STEP 1 — marker RESYNC_REQUIRED (Codex P6-1 high #1) ----
        # Le marker `node_status.RESYNC_REQUIRED` + l'event RESYNC_REQUIRED
        # DOIVENT être les TOUT PREMIERS writes durables une fois la
        # préflight validation passée, AVANT le bump epoch/term, AVANT le
        # drop queue, AVANT le seed token, AVANT stage/apply commit.
        # Justification : si l'un de ces writes ultérieurs crashe ou lève,
        # le node reste classé `RESYNC_REQUIRED` (et non `HEALTHY`) — ce qui
        # bloque tout commit ultérieur implicite et déclenche une recovery
        # opérateur explicite. Sans ce reorder, une exception entre les
        # premiers writes (set_membership) et le marker final laissait le
        # node étiqueté `hivemind_healthy` malgré un état partiellement
        # forçé en avant, faux-positif silencieux.
        await store.set_node_status(
            NodeHealth(
                status=HiveNodeStatus.RESYNC_REQUIRED,
                reason="backup_restore_unsafe_recovery",
                observed_epoch=live_epoch,
                observed_bank_version=live_pointer_version,
            )
        )
        # On retient le new_epoch/new_bank_version finaux pour l'event
        # RESYNC_REQUIRED ; mais à ce stade ils sont DÉJÀ calculables car la
        # politique est `max(live, backup)+1` (epoch/term) et `live+1`
        # (bank_version), tous trois purement dérivés des valeurs préflight.
        early_new_epoch = max(live_epoch, backup_epoch) + 1
        early_new_term = max(live_term_val, backup_term) + 1
        early_new_bank_version = live_pointer_version + 1
        early_resync_event_id = uuid.uuid4().hex
        await store.append_event(
            EventEnvelope(
                event_id=early_resync_event_id,
                type=EventType.RESYNC_REQUIRED,
                origin_node_id=local_node.node_id,
                term=early_new_term,
                membership_epoch=early_new_epoch,
                bank_version=early_new_bank_version,
                payload={
                    "reason": "backup_restore_unsafe_recovery",
                    "observed_epoch": live_epoch,
                    "observed_bank_version": live_pointer_version,
                },
            )
        )

        # Précondition OK : si le pointeur live était ABSENT (orphelin sans
        # pointer), on en pose maintenant un initial à ``bank_version=-1``
        # (sinon ``assert_commit_allowed`` BLOCKED parce que le CAS exige un
        # pointeur présent). Ce write est sûr : un pointeur à -1 est l'état
        # initial canonique (aucun commit), et le commit à venir avancera
        # atomiquement vers 0. Ce write n'a PAS lieu sur le chemin de refus.
        if live_pointer is None:
            await store.set_bank_version_pointer(
                BankVersionPointer(bank_version=-1, commit_id="")
            )

        # ---- 5. Avancer membership_epoch (strictement, mono-noeud post-bump) -
        new_epoch = max(live_epoch, backup_epoch) + 1
        # On restreint la membership au nœud local : les peers seront ré-enrôlés
        # via le canal d'enrôlement après resync (ADR-0016), pas via le restore.
        self_member = Member(
            node_id=local_node.node_id,
            display_name=local_node.display_name,
            public_key=local_node.public_key,
        )
        # Serialize the roster replacement with the membership mutation protocol
        # (same process-global space lock the pairing's promote takes) and, under
        # that lock, RE-verify no concurrent membership advance can turn this write
        # into a split. Two guards, both fail-closed (the node is already durably
        # RESYNC_REQUIRED, so aborting leaves it recoverable, never split):
        #   1. No pairing is mid-activation (awaiting_acks/blocked_recovery).
        #   2. The live epoch has NOT already reached ``new_epoch``. ``new_epoch``
        #      was derived from a PRE-lock preflight read; if a pairing promoted a
        #      target to ACTIVE in the window, the live epoch equals ``new_epoch``
        #      and `set_membership` (which rejects only a STRICTLY lower epoch)
        #      would overwrite the roster at the SAME epoch — a same-epoch split the
        #      peer-channel epoch fence can never detect. Aborting forces the
        #      operator to re-run against fresh state (which forward-forces past the
        #      advance and fences the now-stale target). Guard 1 alone misses this
        #      because the converged pairing is ACTIVE (no longer mid-activation).
        async with MembershipService(store).space_lock():
            try:
                await assert_no_pairing_activation(space_id)
            except PairingActivationError as exc:
                return {"status": "error", "message": str(exc)}
            fresh = await store.get_membership()
            if fresh is not None and fresh.epoch >= new_epoch:
                return {
                    "status": "error",
                    "message": (
                        f"Restore aborted: membership for '{space_id}' advanced during "
                        f"the recovery window (current {fresh.epoch} >= target {new_epoch}). "
                        "The node remains RESYNC_REQUIRED; retry after the in-flight "
                        "pairing converges or is evicted."
                    ),
                }
            await store.set_membership(
                MembershipView(epoch=new_epoch, members=[self_member])
            )

        # ---- 6. Avancer term (strictement, jamais redescendre) ----
        new_term = max(live_term_val, backup_term) + 1
        await store.bump_term(new_term, updated_by_node_id=local_node.node_id)

        # ---- 7. Tombstones UNION (live + backup, idempotent par note_id) ----
        seen_note_ids: set[str] = set()
        union_tombstones: list[Tombstone] = []
        for t in live_tombstones + backup_tombs:
            if t.note_id in seen_note_ids:
                continue
            seen_note_ids.add(t.note_id)
            union_tombstones.append(t)
        for t in union_tombstones:
            await store.add_tombstone(t)

        # ---- 8. Drop queue/ (acks/ purgés en 9, watermarks/ prunés en 10) ---
        queue_dropped = 0
        for entry in await store.list_queue():
            await store.remove_queue_entry(entry)
            queue_dropped += 1

        # ---- 9. Purger acks/ (chaque event_id antérieur est invalidé par le
        #         bump de term ; les pairs ré-ACKeront sur le nouveau term).
        acks_purged = await self._purge_prefix(storage, f"{space_id}/_hivemind/acks/")

        # ---- 10. Prune watermarks/ -> {local_node_id} (un seul membre) ----
        watermarks_pruned = 0
        wm_keys = await storage.list_objects(f"{space_id}/_hivemind/watermarks/")
        for wm in wm_keys:
            key = wm["Key"]
            # watermark_key = {prefix}{node_id}.json
            basename = key.rsplit("/", 1)[-1]
            node_id = basename[: -len(".json")] if basename.endswith(".json") else basename
            if node_id != local_node.node_id:
                await storage.delete(key)
                watermarks_pruned += 1

        # ---- 11. Armer le token HELD au new_term ----
        # Le restore EST l'opération opérateur audité ; on contourne
        # ``LeaseRuntime.acquire`` (qui exige queue+ACK, par construction
        # impossible dans un disaster restore mono-nœud) en écrivant directement
        # un token HELD au new_term, mirror exact de ``seed_holder`` dans les
        # tests commit. C'est la SEULE entorse à la chorégraphie normale, et
        # elle est rendue sûre par : (a) operator+confirm upstream ; (b) le
        # CommitIntent qui suit MATCHE strictement ce token sur tous les champs
        # gatés par ``assert_commit_allowed`` (point d'autorisation unique
        # ADR-0011) — sinon ``apply_commit`` ferme fail-closed avant mutation.
        now = datetime.now(timezone.utc)
        # Lease vivante de 300 s : suffisamment large pour absorber le staging
        # + apply in-process. La convergence FREE est portée par
        # ``CommitRuntime._converge_token_release`` après ``apply_commit``.
        from datetime import timedelta
        lease_until = (
            (now.replace(microsecond=0) + timedelta(seconds=300)).isoformat()
        )
        token_held = TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id=local_node.node_id,
            term=new_term,
            fencing_token=new_term,
            granted_at=now.isoformat(),
            lease_until=lease_until,
            membership_epoch=new_epoch,
            event_id="unsafe-recovery",
        )
        await store.set_token(token_held)

        # ---- 12-14. Stager le bank backup + apply_commit via la chorégraphie -
        new_bank_version = live_pointer_version + 1
        commit_id = uuid.uuid4().hex

        # Lire le contenu du sous-arbre bank/ du backup (texte UTF-8 verbatim)
        # pour le restager. Pour un restore, ``notes_consumed`` reste vide :
        # le backup capture déjà une bank consolidée ; on ne ré-applique pas
        # de transitions de notes.
        bank_files = await self._read_backup_bank_subtree(
            storage, backup_prefix
        )

        # Si le backup est entièrement vide de bank/, on stage tout de même un
        # commit "vide" (manifest=[]) pour MATÉRIALISER le bump bank_version
        # forçé. Le journal/audit/release/RESYNC_REQUIRED sont la valeur du
        # restore même sans contenu bank — un space fraîchement bootstrapé
        # peut légitimement avoir une bank vide.

        # Construire le QueueRuntime + LeaseRuntime + CommitRuntime
        queue_rt = QueueRuntime(store, space_id)
        lease_rt = LeaseRuntime(
            store,
            space_id,
            queue_rt,
            clock=lambda: datetime.now(timezone.utc),
        )
        commit_rt = CommitRuntime(
            store,
            storage,
            space_id,
            lease_rt,
            clock=lambda: datetime.now(timezone.utc),
        )

        # Stage : écrit chaque fichier bank stagé puis MANIFEST.json EN DERNIER.
        commit = await commit_rt.stage_commit(
            commit_id=commit_id,
            proposed_bank=bank_files,
            bank_version=new_bank_version,
            parent_bank_version=live_pointer_version,
            term=new_term,
            membership_epoch=new_epoch,
            committed_by_node_id=local_node.node_id,
            event_id="unsafe-recovery",
            request_id="",
            notes_consumed=[],
        )

        # CommitIntent (matché champ-par-champ au commit).
        intent = CommitIntent(
            holder_node_id=local_node.node_id,
            term=new_term,
            fencing_token=new_term,
            bank_version=new_bank_version,
            previous_bank_version=live_pointer_version,
            commit_id=commit_id,
        )

        # Apply : G0 (assert_commit_allowed) + G1/G2/G3 + journal + promote +
        # flip pointer + watermark + audit events + convergence du token (FREE).
        try:
            await commit_rt.apply_commit(
                commit,
                intent,
                local_node_id=local_node.node_id,
                fencing_token=new_term,
                reason="backup_restore_unsafe_recovery",
            )
        except CommitNotAuthorized as exc:
            # Le gate ADR-0011 a refusé : l'état durable est dans un état
            # incohérent que le forçage ne peut pas honorer. C'est rare (les
            # bumps ci-dessus ont été monotone-safe) mais on remonte clairement
            # plutôt que d'écraser silencieusement. Aucun event UNSAFE_RECOVERY_
            # RESTORED n'est émis (l'apply a échoué AVANT toute mutation
            # post-token). Le forçage en amont (epoch/term/tombstones/queue/
            # acks/watermarks) est FORWARD-ONLY et ne peut pas être déroulé ;
            # le restore est traité comme "à moitié appliqué" et l'opérateur
            # doit corriger l'état avant de rejouer.
            return {
                "status": "error",
                "message": (
                    "Restore interrupted: assert_commit_allowed refused "
                    f"({exc.reason.value}: {exc}). State was partially advanced "
                    "(epoch/term/tombstones/queue/acks/watermarks were ALREADY "
                    "forward-forced; bank and pointer are UNCHANGED). See ADR-0014 "
                    "and issue #87."
                ),
            }

        # ---- 14bis. Compute bank/* orphelins live (Codex P6-1 high #2) ----
        # `apply_commit` a écrit les fichiers du manifeste backup ; les
        # fichiers `bank/*` live ABSENTS du manifeste sont stale (présents
        # avant le restore mais pas dans le snapshot backup). Les laisser
        # vivre = restore = backup_bank ∪ live_bank, ce qui contredit la
        # garantie « bank live == bank du backup ».
        #
        # Codex R2 medium #3 — AUDIT-THEN-DELETE : on CALCULE la liste
        # complète des orphelins ICI, puis on émet l'audit
        # UNSAFE_RECOVERY_RESTORED avec la liste complète en payload AVANT
        # d'exécuter le delete loop (étape 14ter). Cette inversion garantit
        # qu'un crash entre apply_commit et le delete loop (ou mid-delete)
        # laisse une trace durable des clés que le restore AVAIT L'INTENTION
        # de supprimer ; un opérateur (ou une retry convergente) peut
        # consommer le payload de l'audit pour terminer les suppressions.
        # Sans cette inversion, l'audit racontait l'intention APRÈS l'avoir
        # silencieusement exécutée — un crash mid-cleanup laissait des
        # orphelins indétectables (pas dans l'audit, pas dans le manifeste).
        manifest_paths: set[str] = {
            entry.path for entry in commit.manifest
        }
        orphan_keys: list[str] = []
        live_bank_prefix = f"{space_id}/bank/"
        live_bank_objects = await storage.list_objects(live_bank_prefix)
        for obj in live_bank_objects:
            key = obj["Key"]
            rel = key[len(live_bank_prefix) :]
            # Le manifest référence des chemins relatifs à `bank/` (mirror
            # exact de `BankCommitManifestEntry.path`). Les sous-chemins
            # `bank/_meta.json` ou `bank/MANIFEST.json` ne sont jamais
            # dans le manifest (méta de commit) ; on les conserve.
            if rel in ("MANIFEST.json", "_meta.json"):
                continue
            if rel and rel not in manifest_paths:
                orphan_keys.append(key)
        # Tri déterministe pour que le payload audit ait un ordre stable
        # (rejoue idempotent, diff cross-host).
        orphan_keys.sort()
        bank_orphans_deleted = len(orphan_keys)

        # ---- 14quater. Anti-résurrection sur la sous-arborescence live/ -----
        # Codex R3 NO-GO #1 : la chorégraphie ci-dessus a rendu le bank/*
        # exact, mais la sous-arborescence live/ (short-tier) pouvait
        # contredire l'union de tombstones que CE MÊME restore vient de
        # rendre autoritaire. Une note dont le ``note_id`` est dans
        # ``tombstone_union`` mais dont le fichier ``live/{note_id}.md``
        # est encore présent serait, post-restore, EFFECTIVEMENT
        # ressuscitée — l'invariant anti-résurrection (ADR-0013 / cf.
        # ``note_replication``) tombe.
        #
        # On calcule ICI la liste des clés live/* à supprimer (note dont
        # le ``note_id`` apparait dans ``tombstone_union``), AVANT
        # d'émettre l'audit, et on les fait apparaître dans le MÊME
        # event ``UNSAFE_RECOVERY_RESTORED`` que les bank-orphans. Le
        # delete loop live (étape 14quinquies) s'exécute APRÈS l'audit,
        # de sorte qu'un crash entre l'audit et les deletes laisse une
        # trace durable de TOUTES les clés vouées à la suppression sur
        # les DEUX sous-arbres (mêmes garanties que pour bank/*).
        tombstoned_ids: set[str] = {t.note_id for t in union_tombstones}
        live_resurrection_keys: list[str] = []
        if tombstoned_ids:
            live_prefix = f"{space_id}/live/"
            live_objects = await storage.list_objects(live_prefix)
            for obj in live_objects:
                key = obj["Key"]
                # Sidecar de provenance (live/_origin/{note_id}.json) :
                # purge convergente alignée sur l'identité ; on n'attaque
                # pas ces clés ici (le ``_origin/`` est un sidecar P5-7,
                # géré par la couche ``note_replication`` et la GC). Le
                # filtrage par préfixe empêche aussi
                # ``note_id_from_key`` de lever sur un objet sans ``.md``.
                if key.startswith(f"{space_id}/live/_origin/"):
                    continue
                # ``note_id_from_key`` exige un suffixe ``.md`` et un
                # stem non-vide / sans ``/`` (ADR-0013). Tout autre
                # objet sous ``live/`` est un legacy/sidecar qui n'a pas
                # de note_id légitime — on ne le traite pas ici.
                try:
                    note_id = note_id_from_key(key)
                except ValueError:
                    continue
                if note_id in tombstoned_ids:
                    live_resurrection_keys.append(key)
        live_resurrection_keys.sort()
        live_resurrection_deleted = len(live_resurrection_keys)

        # ---- 15. Audit events (UNSAFE_RECOVERY_RESTORED) AVANT delete ------
        # NOTE : l'event RESYNC_REQUIRED a DÉJÀ été émis à l'étape 4bis
        # (premier write durable). On ne le ré-émet PAS ici pour éviter un
        # doublon journal. Le marker `node_status` est mis à jour ci-dessous
        # avec les nouvelles `observed_*` valeurs (toujours RESYNC_REQUIRED).
        old_state = {
            "epoch": live_epoch,
            "term": live_term_val,
            "bank_version": live_pointer_version,
        }
        new_state = {
            "epoch": new_epoch,
            "term": new_term,
            "bank_version": new_bank_version,
        }
        purged = {
            "acks_count": acks_purged,
            "watermarks_pruned_count": watermarks_pruned,
            "queue_dropped_count": queue_dropped,
            "bank_orphans_deleted": bank_orphans_deleted,
            "live_resurrection_deleted": live_resurrection_deleted,
        }

        unsafe_event_id = uuid.uuid4().hex
        await store.append_event(
            EventEnvelope(
                event_id=unsafe_event_id,
                type=EventType.UNSAFE_RECOVERY_RESTORED,
                origin_node_id=local_node.node_id,
                term=new_term,
                membership_epoch=new_epoch,
                bank_version=new_bank_version,
                payload={
                    # NOTE : l'identité opérateur n'est pas encore propagée
                    # via la couche MCP (le seam ``operator_id`` n'existe pas
                    # côté tool). Placeholder ``"operator"`` jusqu'à ce que
                    # le tool seam expose l'identité authentifiée (work
                    # ultérieur — pas dans le scope P6-1).
                    "operator": "operator",
                    "reason": "backup_restore_unsafe_recovery",
                    "confirm": True,
                    "unsafe_recovery": True,
                    "backup_id": backup_id,
                    "old": old_state,
                    "new": new_state,
                    "purged": purged,
                    "bank_orphans_deleted": bank_orphans_deleted,
                    "live_resurrection_deleted": live_resurrection_deleted,
                    # Codex R2 medium #3 : liste COMPLÈTE des clés vouées à
                    # la suppression EN payload. Un crash mid-delete-loop
                    # n'efface pas la trace durable de l'intention ; un
                    # opérateur peut rejouer la suppression depuis cette
                    # liste (clés absolues storage). Liste triée pour
                    # déterminisme cross-host.
                    "bank_orphan_keys": orphan_keys,
                    # Codex R3 NO-GO #1 : liste COMPLÈTE des clés
                    # ``live/{note_id}.md`` vouées à la suppression au
                    # nom de l'invariant anti-résurrection (le
                    # ``note_id`` est dans ``tombstone_union``). Mêmes
                    # propriétés que ``bank_orphan_keys`` (intention
                    # durable AVANT exécution, replay convergent depuis
                    # le payload audit). Liste triée pour déterminisme.
                    "live_resurrection_keys": live_resurrection_keys,
                    "hive_status_label_pre": hive_status_label,
                },
            )
        )

        # ---- 14ter. DELETE LOOP (après l'audit, Codex R2 medium #3) --------
        # L'audit est posé : tout crash ici laisse une trace durable de la
        # liste complète des clés que le restore AVAIT L'INTENTION de
        # supprimer. La retry/convergence peut consommer
        # ``UNSAFE_RECOVERY_RESTORED.payload.bank_orphan_keys`` pour
        # terminer le cleanup (no fail-open : pas de delete sans trace
        # d'intention).
        for key in orphan_keys:
            await storage.delete(key)

        # ---- 14quinquies. DELETE LOOP live/ anti-résurrection (Codex R3) ----
        # Mêmes garanties que le bank-orphan loop ci-dessus : l'audit
        # ``UNSAFE_RECOVERY_RESTORED`` est posé AVANT, sa payload porte
        # la liste complète des clés vouées à la suppression
        # (``live_resurrection_keys``). Un crash ICI laisse une trace
        # durable de l'intention ; le replay convergent re-supprime
        # depuis la liste.
        for key in live_resurrection_keys:
            await storage.delete(key)

        # ---- 16. Mettre à jour le marker RESYNC_REQUIRED avec les nouvelles
        # observed_* (toujours RESYNC_REQUIRED — l'opérateur explicite de
        # resync reste requise ; ``RecoveryTriggers.request_resync`` est
        # BYPASSED ici parce que le resolver `_resolve_hive_or_block`
        # refuserait sur UNSAFE, ce qui bloquerait précisément le restore
        # légitime que l'opérateur vient d'audit-confirmer).
        await store.set_node_status(
            NodeHealth(
                status=HiveNodeStatus.RESYNC_REQUIRED,
                reason="backup_restore_unsafe_recovery",
                observed_epoch=new_epoch,
                observed_bank_version=new_bank_version,
            )
        )

        # ---- 17. Restaurer les fichiers NON-bank (meta/rules/synthesis/live)
        # Le forçage-en-avant ci-dessus n'a touché QUE le bank vivant (via la
        # promotion staged -> live) + l'état Hivemind. On copie maintenant le
        # reste du contenu du backup (en EXCLUANT _hivemind/ : on a forçé en
        # avant, on ne replonge pas dans le passé). Le bank/ a déjà été
        # matérialisé par apply_commit, on l'exclut aussi pour ne pas
        # ré-écrire les fichiers verts par apply_commit.
        files_extra = 0
        live_resurrection_skipped_from_backup = 0
        for obj in backup_objects:
            source_key = obj["Key"]
            relative = source_key[len(backup_prefix) :]
            # Exclure les contenus Hivemind du backup (on a forcé en avant ; le
            # backup contient potentiellement une coordination plus ancienne).
            if relative.startswith("_hivemind/"):
                continue
            # Exclure bank/* (déjà matérialisé par apply_commit).
            if relative.startswith("bank/"):
                continue
            # Codex R3 NO-GO #1 : anti-résurrection appliquée à la copie
            # backup -> live. Le backup peut contenir
            # ``live/{note_id}.md`` pour un ``note_id`` qui figure dans
            # ``tombstone_union`` (le tombstone vient du LIVE, et le
            # backup est antérieur). On vient juste de purger le live
            # pour ce ``note_id`` ; le copier depuis le backup le
            # ressusciterait silencieusement. On skip le ``.md`` ET le
            # sidecar ``live/_origin/{note_id}.json`` correspondant.
            if relative.startswith("live/") and tombstoned_ids:
                if relative.startswith("live/_origin/") and relative.endswith(".json"):
                    origin_stem = relative[len("live/_origin/") : -len(".json")]
                    if origin_stem and origin_stem in tombstoned_ids:
                        live_resurrection_skipped_from_backup += 1
                        continue
                else:
                    try:
                        note_id_backup = note_id_from_key(source_key)
                    except ValueError:
                        note_id_backup = ""
                    if note_id_backup and note_id_backup in tombstoned_ids:
                        live_resurrection_skipped_from_backup += 1
                        continue
            dest_key = f"{space_id}/{relative}"
            await storage.copy_object(source_key, dest_key)
            files_extra += 1

        return {
            "status": "ok",
            "backup_id": backup_id,
            "space_id": space_id,
            "files_restored": len(backup_objects),
            "files_extra_copied": files_extra,
            "unsafe_recovery": True,
            "hive_status_label_pre": hive_status_label,
            "old": old_state,
            "new": new_state,
            "purged": purged,
            "commit_id": commit_id,
        }

    async def _read_backup_hivemind_state(
        self, backup_prefix: str
    ) -> tuple[int, int, int, list[Tombstone]]:
        """
        Lit l'état Hivemind durable d'un backup (epoch / term / bank_version /
        tombstones) directement depuis le sous-arbre ``_hivemind/`` du backup
        (le store ne pointe pas dessus — c'est une copie historique).

        Fail-closed (Codex P6-1 high #3, R2 schema-deep) : chaque fichier
        critique présent dans le backup est VALIDÉ contre le modèle Pydantic
        canonique (``NodeIdentity`` / ``MembershipView`` / ``TermState`` /
        ``TokenLeaseState`` / ``BankVersionPointer`` / ``NodeHealth``). Un
        objet JSON valide mais schéma-malformé (ex: ``node.json`` avec
        ``{}`` — pas de ``node_id`` requis ; ``node_status.json`` avec un
        ``status`` hors enum ; ``token.json`` avec une lease active
        invariant-cassée) lève ``CorruptedStateError``. Les valeurs
        ``term``/``epoch``/``bank_version`` négatives sont aussi refusées
        (mêmes invariants que les models, dupliqués ici en défense en
        profondeur car certaines lectures contournaient la validation côté
        modèle dans la version précédente).

        Les tombstones sont validés contre le modèle ``Tombstone`` (déjà fait
        en R1 ; conservé tel quel).

        Fichiers ABSENTS = defaults conservateurs (epoch=0, term=0,
        bank_version=-1, tombstones=[]) : c'est légitime (un backup d'un
        space fraîchement bootstrapé peut ne pas avoir tous les fichiers). Le
        distinguo est PRESENT-mais-schéma-malformé (CorruptedStateError) vs
        ABSENT (defaults).
        """
        storage = get_storage()

        async def _validate_model(key: str, model_cls):
            """Lit ``backup_prefix/key`` et le valide contre ``model_cls``.

            Retourne l'instance Pydantic si présent et valide ; ``None`` si
            absent (defaults conservateurs côté caller). Lève
            ``CorruptedStateError`` sur tout objet JSON mais schéma-malformé.
            """
            full_key = f"{backup_prefix}{key}"
            raw = await storage.get(full_key)
            if raw is None:
                return None
            try:
                data = _json.loads(raw)
            except (_json.JSONDecodeError, TypeError, ValueError) as exc:
                raise CorruptedStateError(
                    f"Backup _hivemind/{key}: invalid JSON ({exc})"
                ) from exc
            if not isinstance(data, dict):
                raise CorruptedStateError(
                    f"Backup _hivemind/{key}: non-object root "
                    f"({type(data).__name__})"
                )
            try:
                return model_cls.model_validate(data)
            except ValidationError as exc:
                raise CorruptedStateError(
                    f"Backup _hivemind/{key}: {model_cls.__name__} schema "
                    f"violation ({exc})"
                ) from exc
            except (ValueError, TypeError) as exc:
                # ``model_post_init`` (TokenLeaseState invariants) lève
                # ``ValueError`` brute — on l'aplatit en CorruptedStateError
                # pour la même taxonomie fail-closed.
                raise CorruptedStateError(
                    f"Backup _hivemind/{key}: {model_cls.__name__} invariant "
                    f"violation ({exc})"
                ) from exc

        # Validation Pydantic stricte des fichiers critiques (Codex R2 high
        # #1). Chaque ``None`` reflète un fichier absent → default
        # conservateur côté caller. Présent-mais-malformé lève au-dessus.
        node = await _validate_model("_hivemind/node.json", NodeIdentity)
        members = await _validate_model("_hivemind/members.json", MembershipView)
        term_state = await _validate_model("_hivemind/term.json", TermState)
        pointer = await _validate_model(
            "_hivemind/bank_version.json", BankVersionPointer
        )
        token = await _validate_model("_hivemind/token.json", TokenLeaseState)
        node_status = await _validate_model(
            "_hivemind/node_status.json", NodeHealth
        )
        # ``node``/``token``/``node_status`` ne sont pas consommés par la
        # chorégraphie de forçage-en-avant (les valeurs côté algo viennent de
        # ``members``/``term_state``/``pointer``) ; on les valide quand même
        # pour refuser un backup où n'importe quel fichier critique présent
        # serait schéma-malformé. ``_ = ...`` est explicit no-use.
        _ = node
        _ = token
        _ = node_status

        backup_epoch = members.epoch if members is not None else 0
        backup_term = term_state.term if term_state is not None else 0
        backup_bank_version = (
            pointer.bank_version if pointer is not None else -1
        )

        # Défense en profondeur : les models valident déjà ``>= 0`` côté
        # field_validator (TermState.term, MembershipView.epoch,
        # BankCommit.bank_version), mais ``BankVersionPointer.bank_version``
        # n'a PAS de validator (peut valoir -1 = aucun commit). Une valeur
        # négative autre que -1 est légitime côté pointer (default -1) ; en
        # revanche un epoch/term négatif sur les fichiers est un signe de
        # corruption schématique non couvert par le model (qui défaute à 0).
        # On re-vérifie explicitement pour aligner sur le contrat fail-closed.
        if backup_epoch < 0:
            raise CorruptedStateError(
                f"Backup _hivemind/members.json: negative epoch ({backup_epoch})"
            )
        if backup_term < 0:
            raise CorruptedStateError(
                f"Backup _hivemind/term.json: negative term ({backup_term})"
            )
        if backup_bank_version < -1:
            raise CorruptedStateError(
                f"Backup _hivemind/bank_version.json: non-canonical "
                f"bank_version ({backup_bank_version}, expected >= -1)"
            )

        # Tombstones : tout fichier non-parsable refuse le restore (Codex
        # high #3 : un tombstone skipped silencieusement pourrait
        # ressusciter une note supprimée après l'union backup+live).
        backup_tombs: list[Tombstone] = []
        tomb_prefix = f"{backup_prefix}_hivemind/tombstones/"
        tomb_objs = await storage.list_objects(tomb_prefix)
        for obj in tomb_objs:
            raw = await storage.get(obj["Key"])
            if raw is None:
                continue
            try:
                data = _json.loads(raw)
            except (_json.JSONDecodeError, TypeError, ValueError) as exc:
                raise CorruptedStateError(
                    f"Backup tombstone '{obj['Key']}': invalid JSON ({exc})"
                ) from exc
            try:
                backup_tombs.append(Tombstone.model_validate(data))
            except ValidationError as exc:
                raise CorruptedStateError(
                    f"Backup tombstone '{obj['Key']}': invalid schema ({exc})"
                ) from exc

        return backup_epoch, backup_term, backup_bank_version, backup_tombs

    async def _read_backup_bank_subtree(
        self, storage, backup_prefix: str
    ) -> list[tuple[str, str]]:
        """
        Lit le contenu texte du sous-arbre ``bank/`` du backup, retourné comme
        ``[(rel_path, content), ...]`` consommable par ``stage_commit``.

        ``rel_path`` est relatif à ``bank/`` (mirror exact de
        ``BankCommitManifestEntry.path``). Le contenu est UTF-8 verbatim — pas
        de projection ``_meta.json`` ici, car ``_meta.json`` n'est PAS dans
        ``bank/``.
        """
        bank_prefix = f"{backup_prefix}bank/"
        objects = await storage.list_objects(bank_prefix)
        out: list[tuple[str, str]] = []
        for obj in objects:
            key = obj["Key"]
            rel_path = key[len(bank_prefix) :]
            if not rel_path:
                continue
            content = await storage.get(key)
            if content is None:
                continue
            out.append((rel_path, content))
        return out

    async def _purge_prefix(self, storage, prefix: str) -> int:
        """Supprime toutes les clés sous ``prefix`` et retourne le nombre."""
        objs = await storage.list_objects(prefix)
        n = 0
        for obj in objs:
            await storage.delete(obj["Key"])
            n += 1
        return n

    async def download(self, backup_id: str) -> dict:
        """
        Télécharge un backup en archive tar.gz (base64).

        LM2-03 fix : si l'archive contient un ``_meta.json``, le token
        Graph Memory est masqué avant ajout au tar (mêmes garanties
        que ``space_export``). L'archive téléchargée n'expose donc plus
        le secret stocké en clair sur S3.

        Args:
            backup_id: Format "space_id/timestamp"

        Returns:
            {"status": "ok", "archive_base64": "...", ...}
        """
        storage = get_storage()

        parts = backup_id.split("/", 1)
        if len(parts) != 2:
            return {"status": "error", "message": "Invalid backup_id"}

        space_id, timestamp = parts
        backup_prefix = f"_backups/{space_id}/{timestamp}/"

        all_objects = await storage.list_and_get(backup_prefix, exclude_keep=False)
        if not all_objects:
            return {
                "status": "not_found",
                "message": f"Backup '{backup_id}' not found",
            }

        # Créer l'archive tar.gz
        buf = io.BytesIO()
        meta_key = f"{backup_prefix}_meta.json"
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for obj in all_objects:
                arcname = obj["key"][len(backup_prefix) :]
                content = obj["content"]

                # LM2-03 fix : masquer le token GM dans _meta.json avant export
                if obj["key"] == meta_key:
                    try:
                        meta_raw = _json.loads(content)
                        meta_masked = mask_meta_secrets(meta_raw)
                        content = _json.dumps(
                            meta_masked, indent=2, ensure_ascii=False
                        )
                    except (_json.JSONDecodeError, TypeError):
                        # Best-effort : on n'écrit pas en clair si parse échoue
                        # → on remplace par un meta vide protecteur.
                        content = "{}"

                data = content.encode("utf-8")
                info = tarfile.TarInfo(name=arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

        archive_bytes = buf.getvalue()

        return {
            "status": "ok",
            "backup_id": backup_id,
            "archive_base64": base64.b64encode(archive_bytes).decode("ascii"),
            "archive_size": len(archive_bytes),
            "files_count": len(all_objects),
        }

    async def delete(self, backup_id: str) -> dict:
        """
        Supprime un backup.

        Args:
            backup_id: Format "space_id/timestamp"

        Returns:
            {"status": "deleted", "files_deleted": N}
        """
        storage = get_storage()

        parts = backup_id.split("/", 1)
        if len(parts) != 2:
            return {"status": "error", "message": "Invalid backup_id"}

        space_id, timestamp = parts
        backup_prefix = f"_backups/{space_id}/{timestamp}/"

        objects = await storage.list_objects(backup_prefix)
        if not objects:
            return {
                "status": "not_found",
                "message": f"Backup '{backup_id}' not found",
            }

        keys = [o["Key"] for o in objects]
        deleted = await storage.delete_many(keys)

        return {
            "status": "deleted",
            "backup_id": backup_id,
            "files_deleted": deleted,
        }


# ─────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────

_backup_service: BackupService | None = None


def get_backup_service() -> BackupService:
    """Retourne le singleton BackupService."""
    global _backup_service
    if _backup_service is None:
        _backup_service = BackupService()
    return _backup_service
