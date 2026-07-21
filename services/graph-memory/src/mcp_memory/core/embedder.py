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


class EmbeddingService:
    """
    Service d'embedding via LLMaaS Cloud Temple.
    
    Utilise le modèle bge-m3:567m pour générer des vecteurs de 1024 dimensions.
    L'API est au format OpenAI : POST /v1/embeddings
    """
    
    def __init__(self):
        """Initialise le client OpenAI pour les embeddings."""
        settings = get_settings()
        
        # Utilise le même client OpenAI que l'extracteur
        # L'API LLMaaS Cloud Temple est compatible OpenAI
        self._client = AsyncOpenAI(
            base_url=settings.llmaas_base_url,
            api_key=settings.llmaas_api_key,
            timeout=60.0
        )
        self._model = settings.llmaas_embedding_model
        self._dimensions = settings.llmaas_embedding_dimensions
    
    @property
    def dimensions(self) -> int:
        """Dimension des vecteurs produits."""
        return self._dimensions
    
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
            print(f"❌ [Embedder] Erreur API: {e}", file=sys.stderr)
            raise
    
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
            print(f"❌ [Embedder] Erreur API: {e}", file=sys.stderr)
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
                "message": f"Erreur embedding: {str(e)}"
            }


# Singleton pour usage global
_embedding_service: Optional[EmbeddingService] = None


def get_embedding_service() -> EmbeddingService:
    """Retourne l'instance singleton du EmbeddingService."""
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service
