# -*- coding: utf-8 -*-
"""
Outils MCP — Catégorie System (3 outils).

Outils MCP de base (transport /mcp authentifié, sans permission handler en plus) :
    - system_health : vérifie S3, LLMaaS, compte les espaces
    - system_about  : version, outils disponibles, infos système

Outil avec identité authentifiée explicite :
    - system_whoami : identité du token courant (nom, permissions, espaces)
"""

import logging
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

_logger = logging.getLogger("live_mem.system")


def register(mcp: FastMCP) -> int:
    """
    Enregistre les outils system sur l'instance MCP.

    Args:
        mcp: Instance FastMCP

    Returns:
        Nombre d'outils enregistrés (3)
    """

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def system_health() -> dict:
        """
        Check the health of the Hivemind service.

        Tests S3 and LLM provider connectivity and reports the status of each
        dependency. The MCP transport requires valid authentication; this
        handler adds no permission check beyond that transport boundary.
        ``GET /health`` remains the public HTTP health probe.

        Returns:
            Overall health and per-dependency details.
        """
        from ..config import get_settings

        settings = get_settings()
        results = {}

        # ── Test S3 ──────────────────────────────────────────
        # LM2-24 fix : ne pas exposer str(e) (peut contenir endpoint S3 +
        # access key dans la trace botocore). On loggue server-side et
        # on renvoie un message générique. system_health expose des diagnostics
        # plus riches que /health, mais seulement via /mcp authentifié ; on
        # harmonise néanmoins les erreurs par défense en profondeur.
        try:
            from ..core.storage import get_storage

            storage = get_storage()
            results["s3"] = await storage.test_connection()
        except Exception as e:
            _logger.warning("system_health: S3 probe failed: %s", e)
            results["s3"] = {"status": "error", "message": "S3 unreachable"}

        # ── Test LLMaaS ─────────────────────────────────────
        try:
            if settings.llmaas_api_url and settings.llmaas_api_key:
                from openai import AsyncOpenAI

                t0 = time.monotonic()
                client = AsyncOpenAI(
                    base_url=settings.llmaas_api_url,
                    api_key=settings.llmaas_api_key,
                    timeout=5,
                )
                # HM-12 fix : sonde LÉGÈRE (models.list) au lieu d'une complétion
                # LLM réelle. L'ancien chat.completions.create dépensait des tokens
                # LLMaaS à CHAQUE appel, sans check de permission → un token
                # read-only pouvait boucler dessus et brûler le budget LLM
                # (amplification de coût / DoS de facturation). Aligne system_health
                # sur la sonde du endpoint public /health.
                models = await client.models.list()
                latency = round((time.monotonic() - t0) * 1000, 1)
                model_ids = [m.id for m in models.data]
                results["llmaas"] = {
                    "status": "ok",
                    "model": settings.llmaas_model,
                    "model_available": settings.llmaas_model in model_ids,
                    "latency_ms": latency,
                }
            else:
                results["llmaas"] = {
                    "status": "warning",
                    "message": "LLMaaS non configuré",
                }
        except Exception as e:
            _logger.warning("system_health: LLMaaS probe failed: %s", e)
            results["llmaas"] = {"status": "error", "message": "LLMaaS unreachable"}

        # ── Compteur d'espaces ───────────────────────────────
        spaces_count = -1
        try:
            from ..core.storage import get_storage

            storage = get_storage()
            prefixes = await storage.list_prefixes("")
            # Exclure les préfixes système (_system/, _backups/)
            spaces_count = len([p for p in prefixes if not p.startswith("_")])
        except Exception:
            pass

        # ── Statut global ────────────────────────────────────
        service_statuses = [
            r.get("status", "error") for r in results.values() if isinstance(r, dict)
        ]
        all_ok = all(s == "ok" for s in service_statuses)

        return {
            "status": "healthy" if all_ok else "degraded",
            "service_name": settings.mcp_server_name,
            "version": _read_version(),
            "uptime_seconds": round(time.monotonic() - _start_time, 1),
            "services": results,
            "spaces_count": spaces_count,
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def system_about() -> dict:
        """
        Describe the Hivemind MCP service.

        Returns the service version and the tools visible to the authenticated
        caller. The MCP transport requires valid authentication; this handler
        adds no permission check beyond that transport boundary.

        Returns:
            Service metadata and the caller's discoverable tool list.
        """
        from ..config import get_settings

        settings = get_settings()

        # Use the same permission projection as ``tools/list`` without losing
        # the authenticated ``/api/tool`` proxy path, which intentionally calls
        # handlers outside the MCP SDK request context. A lightweight plain
        # FastMCP test instance still lists its local registrations.
        from .exposure import (
            HivemindFastMCP,
            discovery_names_for_token,
        )

        registered = {
            tool.name: tool for tool in mcp._tool_manager.list_tools()
        }
        if isinstance(mcp, HivemindFastMCP):
            from ..auth.context import get_effective_token_info

            exposed_names = discovery_names_for_token(
                get_effective_token_info()
            )
            exposed_tools = [registered[name] for name in exposed_names]
        else:
            exposed_tools = list(registered.values())
        tools = []
        for tool in exposed_tools:
            tools.append(
                {
                    "name": tool.name,
                    "description": (tool.description or "")[:100],
                }
            )

        return {
            "status": "ok",
            "name": settings.mcp_server_name,
            "version": _read_version(),
            "description": "Shared memory layer for collaborative AI agents",
            "author": "Lesur AI",
            "documentation": "https://github.com/Lesur-ai/hivemind",
            # HM-19 fix : plateforme/kernel/version Python retirés de la réponse.
            # platform.platform() (ex "Linux-5.15.0-...-x86_64-glibc2.31") est du
            # fingerprinting d'hôte offert à tout token authentifié (read inclus),
            # utile à un attaquant pour cartographier les CVE kernel/glibc.
            "tools_count": len(tools),
            "tools": tools,
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def system_whoami() -> dict:
        """
        Describe the credential used for the current request.

        Returns the agent name, authentication type, permissions, accessible
        spaces, and available token metadata such as email and timestamps.

        Requires valid authentication with at least read permission.

        Returns:
            The current caller identity and authorization scope.
        """
        from ..auth.context import get_effective_token_info

        token_info = get_effective_token_info()
        if token_info is None:
            return {"status": "error", "message": "Authentification requise"}

        result = {
            "status": "ok",
            "client_name": token_info.get("client_name", "anonymous"),
            "auth_type": token_info.get("type", "unknown"),
            "permissions": token_info.get("permissions", []),
            "allowed_spaces": token_info.get("allowed_resources", []),
        }

        # Le hash du token authentifié est une identité de session STABLE et
        # unique (les noms de client, eux, ne le sont pas). On l'expose donc dès
        # qu'il est disponible, INDÉPENDAMMENT de l'enrichissement best-effort du
        # store ci-dessous : sinon un échec d'enrichissement (store indisponible,
        # token absent du store) priverait l'appelant du seul identifiant unique,
        # ce qui casse la frontière de confidentialité côté console
        # (cache stale-banks lié à la session — PR #159, cf. #164).
        token_hash = token_info.get("token_hash")
        if token_hash and token_info.get("type") == "token":
            result["token_hash"] = token_hash
            # Enrichissement best-effort avec les métadonnées du store S3.
            try:
                from ..core.tokens import get_token_service

                store_data = await get_token_service().list_tokens()
                for t in store_data.get("tokens", []):
                    if t.get("hash") == token_hash:
                        result["email"] = t.get("email", "")
                        result["created_at"] = t.get("created_at", "")
                        result["expires_at"] = t.get("expires_at")
                        result["last_used_at"] = t.get("last_used_at", "")
                        result["space_ids"] = t.get("space_ids", [])
                        break
            except Exception:
                pass  # Enrichissement best-effort

        # Pour le bootstrap key, indiquer clairement
        if token_info.get("type") == "bootstrap":
            result["note"] = "Bootstrap key — accès admin total, pas de token S3"

        return result

    return 3  # Nombre d'outils enregistrés


# ─────────────────────────────────────────────────────────────
# Helpers internes au module
# ─────────────────────────────────────────────────────────────

# Temps de démarrage pour le calcul d'uptime
_start_time = time.monotonic()


def _read_version() -> str:
    """Lit la version depuis le fichier VERSION à la racine du projet."""
    version_file = Path(__file__).parent.parent.parent.parent / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return "dev"
