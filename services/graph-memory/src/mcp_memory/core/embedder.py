# -*- coding: utf-8 -*-
"""
EmbeddingService - Génération d'embeddings via LLMaaS Cloud Temple.

Utilise l'endpoint /v1/embeddings compatible OpenAI avec le modèle
bge-m3:567m (multilingue, 1024 dimensions).

Utilisé pour :
- Vectoriser les chunks de documents (ingestion)
- Vectoriser les requêtes utilisateur (recherche)
"""

import sys
from typing import Optional, List

from openai import AsyncOpenAI
from openai import APIError, APITimeoutError
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import get_settings
from .egress import (
    build_owned_async_http_client,
    close_owned_client_from_sync,
    display_proxy_url,
    redact_proxy_errors_async,
    redact_proxy_secrets,
)


class EmbeddingService:
    """
    Service d'embedding via LLMaaS Cloud Temple.
    
    Utilise le modèle bge-m3:567m pour générer des vecteurs de 1024 dimensions.
    L'API est au format OpenAI : POST /v1/embeddings
    """
    
    def __init__(self):
        """Initialise le client OpenAI pour les embeddings.

        P12-3 (Hivemind #268) : quand ``PROXY_URL`` est configurée, un
        ``httpx.AsyncClient`` POSSÉDÉ route documents, requêtes et sonde
        provider-health via le proxy (même contrat de cycle de vie que
        l'extracteur : fermeture sur échec du constructeur et via ``close()``
        au shutdown). Sans proxy, comportement direct historique inchangé.
        """
        settings = get_settings()

        self._owned_http_client = None
        if settings.proxy_url:
            self._owned_http_client = build_owned_async_http_client(
                settings.proxy_url,
                timeout=60.0,
            )
            # Jamais l'URL brute (potentiellement porteuse de credentials).
            print(
                f"🔀 [Embedder] LLM egress via proxy "
                f"{display_proxy_url(settings.proxy_url)}",
                file=sys.stderr,
            )
        # Utilise le même client OpenAI que l'extracteur
        # L'API LLMaaS Cloud Temple est compatible OpenAI
        try:
            self._client = AsyncOpenAI(
                base_url=settings.llmaas_base_url,
                api_key=settings.llmaas_api_key,
                timeout=60.0,
                http_client=self._owned_http_client,
            )
        except BaseException:
            if self._owned_http_client is not None:
                close_owned_client_from_sync(self._owned_http_client)
                self._owned_http_client = None
            raise
        self._model = settings.llmaas_embedding_model
        self._dimensions = settings.llmaas_embedding_dimensions

    async def close(self) -> None:
        """Ferme le transport proxy possédé (idempotent, référence conservée).

        Une annulation d'appel en vol ne ferme jamais ce transport partagé —
        seul close() (shutdown du service) le fait.
        """
        if self._owned_http_client is not None:
            await self._owned_http_client.aclose()
    
    @property
    def dimensions(self) -> int:
        """Dimension des vecteurs produits."""
        return self._dimensions
    
    @redact_proxy_errors_async
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True
    )
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        Génère les embeddings pour une liste de textes (batch).
        
        Utilisé principalement à l'ingestion pour vectoriser tous les
        chunks d'un document en une seule passe.
        
        Args:
            texts: Liste de textes à vectoriser
            
        Returns:
            Liste de vecteurs (chacun de dimension self._dimensions)
            
        Raises:
            APIError: Si l'API LLMaaS retourne une erreur
            APITimeoutError: Si l'appel dépasse le timeout
        """
        if not texts:
            return []
        
        try:
            print(f"🔢 [Embedder] Vectorisation de {len(texts)} textes ({self._model})...", file=sys.stderr)
            
            response = await self._client.embeddings.create(
                model=self._model,
                input=texts
            )
            
            # Extraire les vecteurs dans l'ordre
            embeddings = [item.embedding for item in response.data]
            
            print(f"✅ [Embedder] {len(embeddings)} embeddings générés (dim={len(embeddings[0])})", file=sys.stderr)
            
            return embeddings
            
        except APITimeoutError:
            print(f"⏰ [Embedder] Timeout — trop de textes ou textes trop longs", file=sys.stderr)
            raise
        except APIError as e:
            print(f"❌ [Embedder] Erreur API: {redact_proxy_secrets(str(e))}", file=sys.stderr)
            raise
    
    @redact_proxy_errors_async
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True
    )
    async def embed_query(self, query: str) -> List[float]:
        """
        Génère l'embedding pour une requête utilisateur.
        
        Utilisé à la recherche pour vectoriser la question avant
        de la comparer aux chunks dans Qdrant.
        
        Args:
            query: Texte de la requête
            
        Returns:
            Vecteur de dimension self._dimensions
        """
        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=[query]
            )
            
            return response.data[0].embedding
            
        except APITimeoutError:
            print(f"⏰ [Embedder] Timeout sur la requête", file=sys.stderr)
            raise
        except APIError as e:
            print(f"❌ [Embedder] Erreur API: {redact_proxy_secrets(str(e))}", file=sys.stderr)
            raise
    
    async def test_connection(self) -> dict:
        """Teste la connexion au service d'embedding."""
        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=["test"]
            )
            
            dim = len(response.data[0].embedding)
            
            return {
                "status": "ok",
                "model": self._model,
                "dimensions": dim,
                "message": f"Embedding OK ({self._model}, {dim}d)"
            }
            
        except APIError as e:
            return {
                "status": "error",
                "model": self._model,
                # P12-3 : jamais d'URL proxy brute dans la sortie santé.
                "message": f"Erreur embedding: {redact_proxy_secrets(str(e))}"
            }


# Singleton pour usage global
_embedding_service: Optional[EmbeddingService] = None


def get_embedding_service() -> EmbeddingService:
    """Retourne l'instance singleton du EmbeddingService."""
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service


async def close_embedding_service_if_initialized() -> None:
    """Ferme le singleton s'il a été instancié (shutdown du service).

    P12-3 : libère le transport proxy possédé injecté dans AsyncOpenAI quand
    ``PROXY_URL`` est défini. Sans instanciation préalable, no-op.
    """
    global _embedding_service
    if _embedding_service is not None:
        service = _embedding_service
        _embedding_service = None
        await service.close()
