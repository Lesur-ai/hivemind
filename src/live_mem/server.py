# -*- coding: utf-8 -*-
"""
Serveur MCP Hivemind — Point d'entrée principal.

Ce fichier :
1. Crée l'instance FastMCP
2. Enregistre les outils MCP via tools/ (modulaire, par catégorie)
3. Assemble la chaîne de middlewares ASGI
4. Démarre le serveur Uvicorn

Architecture des outils (enregistrement centralisé par catégorie) :
    tools/system.py → system_health, system_about, system_whoami
    tools/space.py  → space_create, space_update, space_info, ...
    tools/live.py   → live_note, live_read, live_search
    tools/bank.py   → bank_read, bank_consolidate, bank_stale_spaces, ...
    tools/graph.py  → graph_connect, graph_push, long_query, ...
    tools/backup.py → backup_create, backup_restore, ...
    tools/admin.py  → admin_create_token, admin_gc_notes, ...
    tools/access.py → token_create, space_invite_token

Usage :
    python -m live_mem.server
"""

import sys
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from hivemind_inference.asgi_lifespan import (
    LifespanGuard,
    LifespanHooks,
    LifespanOwnership,
)
from hivemind_inference.process_window import ProcessWindowGate

from .config import get_settings, redact_proxy_secrets
from .tools.exposure import HivemindFastMCP

# ─────────────────────────────────────────────────────────────
# Configuration du logging (stderr uniquement, JSON structuré)
# ─────────────────────────────────────────────────────────────


class _JsonFormatter(logging.Formatter):
    """Structured JSON log formatter for production log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        import json as _json

        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return _json.dumps(entry, ensure_ascii=False)


_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(_JsonFormatter())
logging.root.addHandler(_handler)
logging.root.setLevel(logging.INFO)

# Réduire le bruit des librairies tierces
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

logger = logging.getLogger("live_mem")


# =============================================================================
# Helpers internes
# =============================================================================


def _read_version() -> str:
    """Lit la version depuis le fichier VERSION à la racine du projet."""
    version_file = Path(__file__).parent.parent.parent / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return "dev"


# Clés bootstrap par défaut ou triviales, refusées au démarrage.
_WEAK_BOOTSTRAP_KEYS = {"change_me_in_production", "changeme", "admin", "password", ""}


def _reject_weak_bootstrap_key(key: str) -> None:
    """Raise RuntimeError when ``ADMIN_BOOTSTRAP_KEY`` is default or weak.

    HM-01: called from ``create_app()`` (the chokepoint for every service
    entrypoint), not only from ``main()``. Otherwise an ASGI factory launch
    (``uvicorn --factory``) could start with the repository's public key and
    grant full admin access without prior knowledge.
    """
    if key in _WEAK_BOOTSTRAP_KEYS or len(key) < 32:
        raise RuntimeError(
            "ADMIN_BOOTSTRAP_KEY is missing or too weak. Set a random key of "
            "at least 32 characters in .env before starting the "
            "service (see .env.example)."
        )


# =============================================================================
# Instance FastMCP
# =============================================================================

settings = get_settings()


@asynccontextmanager
async def _lifespan(app: HivemindFastMCP) -> AsyncIterator[None]:
    """
    Gère les préflights propres à chaque session MCP.

    Les transports partagés du processus — consolidateur ET runtime d'inférence
    partagé (PROXY_URL inclus) — ne sont ni validés ni fermés ici : FastMCP
    entre ce contexte une fois par SESSION (`StreamableHTTPSessionManager`
    appelle `Server.run()` par session). Y attacher un singleton de process
    faisait qu'une déconnexion client fermait les transports pour tout le
    monde. Ce cycle de vie appartient au guard ASGI extérieur, une fois au
    démarrage et une fois au shutdown du processus (#306 / P13-1C).
    """
    # LM2-11 : migration auth critique one-shot v1 -> v2. Elle vit dans le
    # lifespan (pas dans main()) afin de couvrir uvicorn --factory, gunicorn et
    # tous les autres entrypoints ASGI. Toute erreur bloque le démarrage :
    # servir avec un store v1 réinterprété serait une élévation de privilège.
    from .core.space import get_space_service
    from .core.tokens import get_token_service
    from .core.embedded_secret import resolve_embedded_token

    spaces_result = await get_space_service().list_spaces()
    if spaces_result.get("status") != "ok":
        raise RuntimeError("Unable to list spaces for the v1 token migration")
    all_ids = [s["space_id"] for s in spaces_result.get("spaces", [])]
    token_service = get_token_service()
    migration = await token_service.migrate_empty_space_ids(all_ids)
    logger.info(
        "Token store migration v1->v2: migrated=%d spaces=%d already=%s",
        migration.get("migrated", 0),
        len(all_ids),
        migration.get("already_migrated", False),
    )

    # Issue #183 — startup/readiness invariant for the mandatory embedded long
    # runtime. Persist the local plaintext first, then confirm its reserved hash
    # is durably active. Any filesystem, token-store, revocation, or expiry
    # ambiguity blocks serving; a retry reuses the already-published file.
    embedded_token = resolve_embedded_token(settings, generate=True)
    if not embedded_token:
        raise RuntimeError(
            "The local embedded long-runtime secret is unavailable; set "
            "LONG_EMBEDDED_TOKEN or repair /data/secrets according to "
            "docs/DEPLOYMENT.md"
        )
    registration = await token_service.register_internal_long_token(embedded_token)
    if (
        registration.get("status") != "ok"
        or registration.get("current_active") is not True
    ):
        raise RuntimeError("Internal long-runtime token is inactive or unregistered")
    logger.info(
        "Embedded long credential startup preflight: registered=%s rotated_out=%d",
        registration.get("registered", False),
        registration.get("rotated_out", 0),
    )

    yield


async def _close_core_process_resources() -> None:
    """Release Core resources exactly once at process shutdown."""

    from .core.consolidator import close_consolidator_if_initialized

    await close_consolidator_if_initialized()


def _validate_inference_startup() -> None:
    """Resolve the shared inference configuration fail-closed, once per window.

    Registered as an ``on_startup`` hook and NOT as ``on_validate``: despite the
    name, :meth:`hivemind_inference.holder.InferenceRuntimeHolder.validate_startup`
    is not a pure check. It lowers the holder's terminal shutdown flag — it is
    the only thing that reopens the seam after a shutdown — and publishes a
    resolved runtime. `on_validate` is documented for checks that acquire
    nothing, and its failure path rolls back with ``cleanup_required=False``;
    `on_startup` is the kind the guard designates for work that takes state,
    and its failure rolls back THROUGH ``on_shutdown``.

    Declaring a startup hook also makes the ASGI lifespan mandatory
    (``lifecycle_required``), so `uvicorn --lifespan off` is refused before
    application dispatch rather than serving on an unvalidated configuration.
    """

    from .core.inference_runtime import validate_inference_startup

    validate_inference_startup()


async def _close_inference_runtime() -> None:
    """Release the shared inference runtime's owned provider transports.

    A SIBLING of ``_close_core_process_resources`` rather than a step inside
    it: the guard runs every ``on_shutdown`` entry through ``run_finalizers``,
    so neither closer can be skipped by the other's failure or cancellation.
    Exhaustive finalisation is the guard's responsibility — re-implementing it
    per service is what produced two divergent copies in the first place.
    """

    from .core.inference_runtime import close_inference_runtime_if_initialized

    await close_inference_runtime_if_initialized()


def _report_lifespan(line: str) -> None:
    logger.warning("%s", line)


# Every resource the shutdown hooks below release is process-global — the
# consolidator singleton and the shared inference runtime holder — while each
# `create_app()` builds its OWN guard with its own startup gate. This gate is
# what makes those two scopes agree (#276 / R7-F1).
_process_window = ProcessWindowGate(service="Hivemind")


mcp = HivemindFastMCP(
    name=settings.mcp_server_name,
    host=settings.mcp_server_host,
    port=settings.mcp_server_port,
    lifespan=_lifespan,
)
# FastMCP 1.27.0 does not expose a constructor-level version argument.
# Without this explicit low-level assignment, MCP initialize/serverInfo.version
# falls back to the SDK package version ("mcp"), not Hivemind's VERSION file.
mcp._mcp_server.version = _read_version()

# =============================================================================
# Enregistrement des outils — délégué aux modules tools/
# =============================================================================
# Chaque module tools/xxx.py expose une fonction register(mcp) -> int
# qui déclare ses outils via @mcp.tool() et retourne le nombre d'outils.

from .tools import register_all_tools  # noqa: E402

tools_count = register_all_tools(mcp)


# =============================================================================
# Assemblage ASGI — Chaîne de middlewares
# =============================================================================


def create_app():
    """
    Crée l'application ASGI complète avec les middlewares.

    Pile d'exécution (premier exécuté → dernier) :
        RequestId → Auth → Metrics → Audit → Logging → ResponseLimit
        → [Mesh namespace, si activé] → StaticFiles → MCP Streamable HTTP

    Le RequestIdMiddleware génère un ID unique par requête (contextvars).
    Le MetricsMiddleware expose /metrics et collecte les compteurs.
    L'AuthMiddleware extrait le Bearer token et l'injecte dans les contextvars.
    L'AuditMiddleware émet des entrées d'audit JSON structurées.
    Le LoggingMiddleware trace les requêtes HTTP en JSON sur stderr.
    Le ResponseLimitMiddleware tronque les réponses > 512 KB (MCP exclu).
    Le StaticFilesMiddleware sert /live, /static/*, /api/* (interface web).
    """
    # HM-01 fix : rejeter une ADMIN_BOOTSTRAP_KEY par défaut/faible ICI, dans la
    # factory ASGI — le SEUL point traversé par TOUS les entrypoints (main(),
    # `uvicorn --factory live_mem.server:create_app`, gunicorn UvicornWorker).
    # Avant, le rejet n'existait QUE dans main() ; un lancement par factory (le
    # pattern ASGI idiomatique) le contournait et démarrait avec la clé publique
    # `change_me_in_production` → compromission admin totale zéro-connaissance.
    _reject_weak_bootstrap_key(settings.admin_bootstrap_key)

    # P13-1C : la validation d'inférence n'est PAS répétée ici. Elle était
    # appelée à la factory (revue Codex ronde 5, R5-F1) parce que la faire
    # uniquement dans le handshake lifespan la rendait tributaire de
    # l'existence de ce handshake — `uvicorn --factory ... --lifespan off` ne
    # dispatche aucun scope lifespan. Le guard partagé (#306) règle ce cas
    # sans acquérir quoi que ce soit : déclarer un hook de cycle de vie rend
    # le protocole lifespan OBLIGATOIRE, et une requête arrivant sans lui est
    # refusée avant tout dispatch applicatif. Valider à la factory publierait
    # au contraire un runtime résolu hors de toute fenêtre capable de le
    # libérer. Un seul propriétaire : `LifespanHooks.on_startup`, plus bas.

    from .auth.middleware import (
        AuthMiddleware,
        LoggingMiddleware,
        StaticFilesMiddleware,
    )
    from .middleware import (
        RequestIdMiddleware,
        MetricsMiddleware,
        ResponseLimitMiddleware,
        AuditMiddleware,
    )

    # P10-2: inspect only the strict feature flag before assembling the stack.
    # In disabled mode no Mesh module is imported and the middleware objects
    # below are constructed in the exact historical order.  Enabled-mode
    # configuration is loaded lazily and fails closed before the app is served.
    mesh_flag = settings.hivemind_mesh_enabled
    if mesh_flag not in {"true", "false"}:
        raise RuntimeError(
            "HIVEMIND_MESH_ENABLED must be exactly 'true' or 'false'"
        )
    mesh_enabled = mesh_flag == "true"
    mesh_config = None
    mesh_namespace = None
    mesh_process_lock = None
    mesh_pairing_service = None
    # The fingerprint-neutral preparation record is irreversible provenance,
    # even when Mesh is disabled after a restart. Register a core-only durable
    # GET checker before the optional Mesh imports. It resolves storage lazily,
    # performs no write/state creation, and never negative-caches across
    # processes. Enabled mode replaces it below with the strict parsed store.
    from .core.reservation_guard import (
        assert_no_active_source_preparation,
        assert_no_source_preparation_provenance,
        register_direct_local_checker,
        register_reservation_checker,
    )

    async def _core_direct_local_checker(space_id: str) -> None:
        from .core.storage import get_storage

        await assert_no_source_preparation_provenance(get_storage(), space_id)

    register_direct_local_checker(_core_direct_local_checker)

    async def _core_preparation_reservation_checker(space_id: str) -> None:
        from .core.storage import get_storage

        await assert_no_active_source_preparation(get_storage(), space_id)

    register_reservation_checker(_core_preparation_reservation_checker)

    if mesh_enabled:
        import time
        from pathlib import Path

        from .core.reservation_guard import (
            NotMembershipLeaderError,
            register_membership_recovery_leader_checker,
            register_pairing_activation_checker,
        )
        from .core.storage import get_storage
        from .mesh.config import load_mesh_config, load_mesh_environment
        from .mesh.destination import MeshDestination
        from .mesh.pairing_client import HttpPeerSender
        from .mesh.pairing_service import MeshPairingService
        from .mesh.replay import MeshProcessIdentityLock
        from .mesh.router import MeshNamespaceRouter
        from .mesh.transport import HttpPeerTransport

        mesh_config = load_mesh_config(load_mesh_environment())
        if mesh_config is None:  # Defensive: the strict flag above is true.
            raise RuntimeError("Mesh enabled configuration is unavailable")
        mesh_process_lock = MeshProcessIdentityLock(
            Path(settings.long_embedded_token_file).parent
            / "mesh-process-locks",
            mesh_config.fingerprint,
        )
        mesh_process_lock.acquire()
        mesh_namespace = MeshNamespaceRouter.is_mesh_namespace

        def _mesh_sender_factory(endpoint: str):
            # Each outbound peer call gets a fresh HTTPS transport with the full
            # P10-2 SSRF/DNS-rebinding/redirect/bounded-body defences.
            return HttpPeerSender(HttpPeerTransport(MeshDestination.parse(endpoint)))

        mesh_pairing_service = MeshPairingService(
            mesh_config,
            clock_ms=lambda: int(time.time() * 1000),
            sender_factory=_mesh_sender_factory,
            storage_factory=get_storage,
        )
        # Enabled mode upgrades the core-only PREPARING guard to the pairing
        # store so it also covers blank-target reservations. The wrapper defers
        # ``.store`` (and thus storage resolution) to call time.
        async def _mesh_reservation_checker(space_id: str) -> None:
            await mesh_pairing_service.assert_space_not_reserved(space_id)

        register_reservation_checker(_mesh_reservation_checker)

        # A completed preparation is not a STAGED-write reservation, but it is
        # permanent provenance: even if its Hivemind prefix is later lost, this
        # durable checker prevents the route from authorizing DIRECT_LOCAL.
        async def _mesh_direct_local_checker(space_id: str) -> None:
            await mesh_pairing_service.store.assert_direct_local_allowed(
                space_id
            )

        register_direct_local_checker(_mesh_direct_local_checker)

        # The pairing-activation fence blocks an operator epoch-advancing
        # membership mutation (re-scope / add_member) while a SOURCE pairing for
        # the space is mid-activation (promotion -> confirmed), so it cannot split
        # the source/target MembershipViews. Zero-cost no-op when Mesh is disabled.
        async def _mesh_activation_checker(
            space_id: str, ignore_pair_id: str | None
        ) -> None:
            await mesh_pairing_service.assert_no_pairing_activation(
                space_id, ignore_pair_id=ignore_pair_id
            )

        register_pairing_activation_checker(_mesh_activation_checker)

        # Mesh membership is a single-writer authority (the flock-elected leader).
        # Out-of-band membership recovery (unsafe backup_restore) must run only on
        # the leader so its roster write is serialized against pairing promotions by
        # the leader's in-process lock — a non-leader worker refuses (fail-closed),
        # closing the cross-process same-epoch-overwrite race the in-process lock
        # alone cannot. Reads the flock state held for this process's lifetime.
        # Host-scoped, exactly like the mesh router/admin 503 gate it mirrors: the
        # flock elects one leader per host; multi-HOST instances over shared storage
        # are unsupported in V1 (docs/DEPLOYMENT.md) and out of this gate's reach.
        async def _mesh_recovery_leader_checker(space_id: str) -> None:
            if getattr(mesh_process_lock, "acquired", False) is not True:
                raise NotMembershipLeaderError(space_id)

        register_membership_recovery_leader_checker(_mesh_recovery_leader_checker)

    # L'app de base est le Streamable HTTP handler du SDK MCP
    # Endpoint unique : POST/GET /mcp (remplace /sse + /messages)
    app = mcp.streamable_http_app()

    # Empiler les middlewares (dernier ajouté = premier exécuté)
    # Ordre d'exécution : RequestId → Auth → Metrics → Audit → Logging → ResponseLimit → Static → MCP
    # HM-02 fix : MetricsMiddleware est désormais APRÈS AuthMiddleware. Une requête
    # non authentifiée sur une route non-publique est court-circuitée en 401 par
    # Auth et n'atteint plus Metrics → plus de cardinalité de métriques pilotable
    # sans token (le vecteur de DoS mémoire). /metrics reste public (Auth le laisse
    # passer via PUBLIC_PATHS).
    # Audit APRÈS Auth pour que current_token_info soit encore set dans le finally d'Audit.
    # Les 401 (rejets auth) sont audités directement par AuthMiddleware.
    app = StaticFilesMiddleware(app)
    if mesh_config is not None:
        # The peer router sits inside the existing observability/response guards
        # but ahead of Static/MCP dispatch, so malformed reserved paths cannot
        # fall through into either legacy surface.
        if mesh_process_lock is None:  # Defensive fail-closed type narrowing.
            raise RuntimeError("Mesh process lock is unavailable")
        app = MeshNamespaceRouter(
            app,
            config=mesh_config,
            process_lock=mesh_process_lock,
            pairing_service=mesh_pairing_service,
        )
        # The admin control plane (/api/admin/mesh/*) sits behind AuthMiddleware
        # (added below, so it runs first) and delegates all other paths onward.
        from .mesh.mesh_admin import MeshAdminMiddleware

        app = MeshAdminMiddleware(app, mesh_pairing_service, process_lock=mesh_process_lock)
    app = ResponseLimitMiddleware(app, max_bytes=settings.response_max_bytes)
    app = LoggingMiddleware(app)
    app = AuditMiddleware(app)
    app = MetricsMiddleware(app)
    app = AuthMiddleware(app, peer_namespace=mesh_namespace)
    app = RequestIdMiddleware(app)

    async def _migrate_target_pairing_admission_anchors() -> None:
        """Materialize O(1) #417 target provenance before any dispatch.

        The mesh process identity lock is already held above.  In disabled mode
        this is intentionally a no-op; in enabled mode any ambiguous retained
        intent inventory aborts process startup rather than exposing an old
        #417 target to the legacy ordinary-write path.
        """

        if mesh_pairing_service is not None:
            await mesh_pairing_service.migrate_target_pairing_admission_anchors()

    # Outermost process owner. The consolidator transport and the shared
    # inference runtime are both lazy and process-scoped, so the guard also
    # refuses `--lifespan off` before request code can acquire either.
    #
    # The process window uses the guard's dedicated synchronous ownership
    # phase: pure validation remains pure, and a refused overlapping factory
    # runs no shutdown hook. Every process-global hook is owner-guarded.
    # Release is deliberately NOT a finalizer; the guard emits it only after a
    # fully cleaned startup rollback or a completely clean shutdown. Any close
    # failure, cancellation, inner death, or quarantine retains the slot and
    # requires process recycle.
    window = _process_window.new_window()
    app = LifespanGuard(
        app,
        name="hivemind-core",
        hooks=LifespanHooks(
            ownership=LifespanOwnership(
                reserve=window.claim,
                release_reusable=window.release,
            ),
            on_startup=(
                window.guard(_migrate_target_pairing_admission_anchors),
                window.guard(_validate_inference_startup),
            ),
            on_shutdown=(
                window.guard(_close_core_process_resources),
                window.guard(_close_inference_runtime),
            ),
        ),
        redact=redact_proxy_secrets,
        report=_report_lifespan,
    )

    return app


# =============================================================================
# Point d'entrée
# =============================================================================


def main():
    """Démarre le serveur MCP Hivemind."""
    import uvicorn

    version = _read_version()

    # Apply the same fail-closed gate as the ASGI factory so every supported
    # entrypoint enforces the documented minimum before binding a socket.
    try:
        _reject_weak_bootstrap_key(settings.admin_bootstrap_key)
    except RuntimeError as exc:
        logger.critical(
            "⛔ %s",
            exc,
        )
        sys.exit(1)

    # Lister les outils disponibles et les grouper par catégorie
    tools = mcp._tool_manager.list_tools()
    tool_names = [t.name for t in tools]

    # Tier-canonical aliases (short_/mid_/long_) are bucketed next to their
    # historical engine so the enumerated listing sums to the header count (#22).
    categories = {
        "System": [n for n in tool_names if n.startswith("system_")],
        "Space": [n for n in tool_names if n.startswith("space_")],
        "Live": [n for n in tool_names if n.startswith("live_")],
        "Short": [n for n in tool_names if n.startswith("short_")],
        "Bank": [n for n in tool_names if n.startswith("bank_")],
        "Mid": [n for n in tool_names if n.startswith("mid_")],
        "Graph": [n for n in tool_names if n.startswith("graph_")],
        "Long": [n for n in tool_names if n.startswith("long_")],
        "Backup": [n for n in tool_names if n.startswith("backup_")],
        "Admin": [n for n in tool_names if n.startswith("admin_")],
        "Token": [n for n in tool_names if n.startswith("token_")],
    }

    # Construire les lignes de contenu de la bannière
    content_lines = []
    content_lines.append(f"  Hivemind MCP Server v{version}")
    content_lines.append("")
    content_lines.append(f"  🔧 {len(tool_names)} MCP tools:")
    for cat, names in categories.items():
        if names:
            content_lines.append(f"     {cat:7s}: {', '.join(names)}")
    content_lines.append("")
    host = settings.mcp_server_host
    port = settings.mcp_server_port
    content_lines.append(f"  🌐 http://{host}:{port}")
    content_lines.append(f"  📡 http://{host}:{port}/mcp")
    content_lines.append(f"  🖥️  http://{host}:{port}/live")

    # Calculer la largeur du cadre (largeur max + marges)
    # Note : les emoji comptent pour 2 colonnes en affichage terminal
    def _display_len(s: str) -> int:
        """Longueur d'affichage (emoji/wide chars = 2 colonnes)."""
        import unicodedata

        length = 0
        for ch in s:
            eaw = unicodedata.east_asian_width(ch)
            if eaw in ("W", "F"):
                length += 2
            elif unicodedata.category(ch).startswith("So"):
                # Symboles (emoji non-CJK comme 🔧🌐📡)
                length += 2
            else:
                length += 1
        return length

    inner_width = max(_display_len(line) for line in content_lines) + 2
    inner_width = max(inner_width, 50)  # Minimum 50 colonnes

    # Construire la bannière avec cadre
    sep = "═" * inner_width
    banner = f"\n╔{sep}╗\n"
    for i, line in enumerate(content_lines):
        pad = inner_width - _display_len(line)
        banner += f"║{line}{' ' * pad}║\n"
        # Séparateur après le titre
        if i == 0:
            banner += f"╠{sep}╣\n"
    banner += f"╚{sep}╝\n"

    print(banner, file=sys.stderr)

    # Créer l'app ASGI avec middlewares et démarrer Uvicorn
    app = create_app()

    uvicorn.run(
        app,
        host=settings.mcp_server_host,
        port=settings.mcp_server_port,
        log_level="warning",  # Uvicorn en mode silencieux (on log via middleware)
    )


if __name__ == "__main__":
    main()
