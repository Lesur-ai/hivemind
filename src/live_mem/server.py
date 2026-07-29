# -*- coding: utf-8 -*-
"""
Serveur MCP Hivemind — Point d'entrée principal.

Ce fichier :
1. Crée l'instance FastMCP
2. Enregistre les outils MCP via tools/ (modulaire, par catégorie)
3. Assemble la chaîne de middlewares ASGI
4. Démarre le serveur Uvicorn

Architecture des outils (48 outils directs, 8 catégories) :
    tools/system.py → system_health, system_about, system_whoami (3)
    tools/space.py  → space_create, space_update, space_info, ... (9)
    tools/live.py   → live_note, live_read, live_search (3)
    tools/bank.py   → bank_read, bank_consolidate, bank_stale_spaces, ... (11)
    tools/graph.py  → graph_connect, graph_push, long_query, ... (6)
    tools/backup.py → backup_create, backup_restore, ... (5)
    tools/admin.py  → admin_create_token, admin_gc_notes, ... (9)
    tools/access.py → token_create, space_invite_token (2)

Usage :
    python -m live_mem.server
"""

import sys
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from .config import get_settings
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
    Gère le cycle de vie du serveur MCP.

    Au shutdown : ferme proprement le ConsolidatorService si actif
    (libère le httpx.AsyncClient injecté quand PROXY_URL est défini).
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

    try:
        yield
    finally:
        from .core.consolidator import close_consolidator_if_initialized
        await close_consolidator_if_initialized()


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
    if mesh_enabled:
        import time
        from pathlib import Path

        from .core.reservation_guard import (
            NotMembershipLeaderError,
            register_membership_recovery_leader_checker,
            register_pairing_activation_checker,
            register_reservation_checker,
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
        # The blank-target reservation guard consults the pairing store; it is a
        # zero-cost no-op when Mesh is disabled (no checker registered). The
        # wrapper defers ``.store`` (and thus storage resolution) to call time.
        async def _mesh_reservation_checker(space_id: str) -> None:
            await mesh_pairing_service.store.assert_space_not_reserved(space_id)

        register_reservation_checker(_mesh_reservation_checker)

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
    content_lines.append(f"  🔧 {len(tool_names)} outils MCP :")
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
