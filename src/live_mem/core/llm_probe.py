# -*- coding: utf-8 -*-
"""
Sonde de connectivité LLM partagée par ``/health`` et ``system_health``.

P12-1 : les deux sondes de santé doivent honorer ``PROXY_URL`` comme le fait
déjà le consolidateur. La sonde reste l'appel LÉGER ``models.list()`` (HM-12) :
aucun token LLM n'est dépensé.

Cycle de vie du client (contrat P12-1) :

- un ``httpx.AsyncClient`` possédé est créé UNIQUEMENT quand ``PROXY_URL`` est
  configuré, puis injecté dans ``AsyncOpenAI`` (qui n'en prend pas ownership,
  même convention que ``ConsolidatorService``) ;
- SEUL ce client possédé est fermé, sur chaque chemin de succès comme
  d'exception (y compris un échec du constructeur ``AsyncOpenAI``) ;
- sans proxy, le comportement direct historique est préservé à l'identique :
  ``AsyncOpenAI`` gère son transport interne comme avant.

Les appelants conservent leurs propres sémantiques de redaction et de statut
HTTP : cette fonction PROPAGE les exceptions au lieu de les traduire.
"""

import httpx
from openai import AsyncOpenAI

# Timeout par sonde historique des deux endpoints de santé (secondes).
PROBE_TIMEOUT_SECONDS = 5


async def list_llm_models(settings) -> list[str]:
    """
    Sonde ``models.list()`` en honorant ``PROXY_URL``, et retourne les ids.

    Args:
        settings: ``Settings`` avec ``llmaas_api_url``, ``llmaas_api_key``
            et ``proxy_url`` déjà validés au démarrage.

    Returns:
        Liste des identifiants de modèles exposés par le provider.

    Raises:
        Exception: toute erreur de construction, de transport ou de provider —
            l'appelant applique sa propre redaction (LM2-24/HM-18).
    """
    owned_client: httpx.AsyncClient | None = None
    try:
        if settings.proxy_url:
            owned_client = httpx.AsyncClient(
                proxy=httpx.Proxy(url=settings.proxy_url),
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        client = AsyncOpenAI(
            base_url=settings.llmaas_api_url,
            api_key=settings.llmaas_api_key,
            timeout=PROBE_TIMEOUT_SECONDS,
            http_client=owned_client,
        )
        models = await client.models.list()
        return [m.id for m in models.data]
    finally:
        if owned_client is not None:
            await owned_client.aclose()
