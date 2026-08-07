# -*- coding: utf-8 -*-
"""
EmbeddingService - Génération d'embeddings via la frontière partagée (P13-1C).

Consomme le contrat ``EmbeddingProvider`` de ``hivemind_inference``
(ADR-0027) : le profil embedding résolu — le MÊME que celui du cœur
Hivemind — porte modèle, dimensions attendues, transport (``PROXY_URL``,
contrat egress P12-3 inchangé) et le retry borné. L'adapter valide ordre,
cardinalité, dimensions exactes et finitude des composantes AVANT tout retour :
aucun vecteur partiel ou mal dimensionné n'atteint Qdrant.

Utilisé pour :
- Vectoriser les chunks de documents (ingestion, ``input_type=document``)
- Vectoriser les requêtes utilisateur (recherche, ``input_type=query``)
"""

import sys
from typing import Optional, List

from hivemind_inference import EmbeddingResult, InferenceError
from hivemind_inference.certification_budget import (
    protected_certification_discovery_timeout_seconds,
    protected_certification_model_discovery,
)
from hivemind_inference.records import EmbeddingRequest

from ..config import get_settings
from .egress import redact_proxy_errors_async, redact_proxy_secrets

# Timeout total historique des appels embeddings (connexion + envoi + lecture
# + le seul retry borné autorisé — ADR-0027).
EMBEDDING_TIMEOUT_SECONDS = 60.0


class EmbeddingService:
    """
    Service d'embedding via la frontière d'inférence partagée.

    Le format wire reste OpenAI-compatible (``POST /embeddings``) via l'adapter
    générique enregistré ; aucun SDK provider n'est construit ici.
    """

    def __init__(self):
        """Snapshotte le profil embedding résolu (P13-1C).

        Un profil absent est un démarrage VALIDE : chaque opération échoue
        alors explicitement (enveloppe sûre), sans accès réseau.
        """
        # Parité historique : une configuration de service invalide échoue ici.
        get_settings()

        from .inference_runtime import get_inference_runtime

        self._embedding_profile = get_inference_runtime().config.embedding
        self._model = (
            self._embedding_profile.configured_model
            if self._embedding_profile
            else ""
        )
        self._dimensions = (
            self._embedding_profile.expected_dimensions
            if self._embedding_profile
            else 0
        )

    async def close(self) -> None:
        """Compatibilité shutdown : no-op idempotent.

        P13-1C : le transport appartient au runtime d'inférence partagé, fermé
        par le lifespan ASGI. Une annulation d'appel en vol ne peut donc pas le
        fermer.
        """
        return None

    @property
    def dimensions(self) -> int:
        """Dimension des vecteurs produits (profil résolu)."""
        return self._dimensions

    async def _embed(self, texts: List[str], input_type: str) -> EmbeddingResult:
        """Requête normalisée vers l'adapter enregistré (P13-1C).

        ``input_type`` (document|query) est préservé sémantiquement même si le
        format wire OpenAI-compatible est symétrique (ADR-0027). Le retry borné
        vit dans l'adapter : les décorateurs ``tenacity`` historiques, qui
        rejouaient jusqu'à 3 fois une requête dont la livraison était ambiguë,
        sont supprimés.
        """
        from .inference_runtime import get_inference_runtime

        provider = get_inference_runtime().embedding_provider()
        request = EmbeddingRequest(
            inputs=tuple(texts),
            timeout_seconds=EMBEDDING_TIMEOUT_SECONDS,
            input_type=input_type,
        )
        result = await provider.embed(request)
        if type(result) is not EmbeddingResult:
            raise RuntimeError(
                "embedding provider returned an invalid normalized result"
            )
        return result

    @redact_proxy_errors_async
    async def embed_texts_result(self, texts: List[str]) -> EmbeddingResult:
        """Génère un batch et conserve son ``EmbeddingResult`` exact.

        P13-1D : l'identité configurée/résolue et la preuve du modèle doivent
        atteindre le guard Qdrant avec les vecteurs. Les convertir ici en listes
        les détruirait avant que le consommateur puisse vérifier une dérive entre
        batches.
        """
        if not texts:
            raise ValueError(
                "embed_texts_result requires a non-empty text batch"
            )

        try:
            print(f"🔢 [Embedder] Vectorisation de {len(texts)} textes ({self._model})...", file=sys.stderr)

            result = await self._embed(texts, "document")

            print(
                f"✅ [Embedder] {len(result.vectors)} embeddings générés "
                f"(dim={result.effective_dimensions})",
                file=sys.stderr,
            )

            return result

        except InferenceError as e:
            if e.category == "timeout":
                print(f"⏰ [Embedder] Timeout — trop de textes ou textes trop longs", file=sys.stderr)
            else:
                # Enveloppe sûre par construction ; redaction conservée en
                # défense en profondeur.
                print(f"❌ [Embedder] Erreur provider: {redact_proxy_secrets(str(e))}", file=sys.stderr)
            raise

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Wrapper historique : renvoie des listes sans modifier leur ordre."""
        if not texts:
            return []
        result = await self.embed_texts_result(texts)
        return [list(vector) for vector in result.vectors]

    @redact_proxy_errors_async
    async def embed_query_result(self, query: str) -> EmbeddingResult:
        """Génère une requête et conserve son ``EmbeddingResult`` exact.

        La cardinalité est déjà validée par l'adapter contre la requête à une
        entrée. Le contrôle local reste explicite afin qu'un faux provider
        in-process ne puisse pas faire choisir silencieusement le premier de
        plusieurs vecteurs.
        """
        try:
            result = await self._embed([query], "query")
            if len(result.vectors) != 1:
                raise RuntimeError(
                    "embedding provider returned an invalid query cardinality"
                )
            return result

        except InferenceError as e:
            if e.category == "timeout":
                print(f"⏰ [Embedder] Timeout sur la requête", file=sys.stderr)
            else:
                print(f"❌ [Embedder] Erreur provider: {redact_proxy_secrets(str(e))}", file=sys.stderr)
            raise

    async def embed_query(self, query: str) -> List[float]:
        """Wrapper historique : renvoie le vecteur de requête comme liste."""
        result = await self.embed_query_result(query)
        return list(result.vectors[0])

    async def test_connection(self) -> dict:
        """Sonde de santé du provider embedding — discovery UNIQUEMENT.

        HM-12 / ADR-0027 (P13-1C) : plus AUCUN embedding réel ici — l'ancienne
        sonde vectorisait "test" et dépensait des tokens à CHAQUE appel santé.
        Une absence de ``/models`` reste un endpoint joignable, pas une panne.
        Forme historique préservée : ``{status, model, dimensions, message}`` ;
        ``dimensions`` rapporte la valeur ATTENDUE du profil, que l'adapter
        vérifie exactement sur chaque vraie réponse.
        """
        from .inference_runtime import get_inference_runtime

        try:
            runtime = get_inference_runtime()
            if runtime.config.embedding is None:
                return {
                    "status": "error",
                    "model": "",
                    "message": "Erreur embedding: provider embedding non configuré",
                }
            discovery_contract = protected_certification_model_discovery(
                role="embedding",
                provider_id=runtime.config.embedding.provider_id,
                endpoint=runtime.config.embedding.endpoint,
                configured_model=runtime.config.embedding.configured_model,
            )
            if discovery_contract == "unsupported":
                return {
                    "status": "ok",
                    "model": self._model,
                    "dimensions": self._dimensions,
                    "message": (
                        "Catalogue embedding non disponible pour le profil "
                        "de certification protégé"
                    ),
                }
            probe = runtime.embedding_probe()
            timeout_seconds = protected_certification_discovery_timeout_seconds()
            if timeout_seconds is None:
                # Preserve the adapter-owned ordinary-runtime default exactly.
                result = await probe.probe()
            else:
                result = await probe.probe(timeout_seconds=timeout_seconds)
        except Exception as e:
            return {
                "status": "error",
                "model": self._model,
                # P12-3 : jamais d'URL proxy brute dans la sortie santé.
                "message": f"Erreur embedding: {redact_proxy_secrets(str(e))}",
            }
        if result.healthy:
            return {
                "status": "ok",
                "model": self._model,
                "dimensions": self._dimensions,
                "message": f"Embedding OK ({self._model}, {self._dimensions}d attendus)",
            }
        return {
            "status": "error",
            "model": self._model,
            "message": "Erreur embedding: provider unreachable"
            + (
                f" ({result.error_category})"
                if result.error_category is not None
                else ""
            ),
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

    P13-1C : le transport appartient au runtime d'inférence partagé, fermé par
    le lifespan via ``close_inference_runtime_if_initialized``. Ce hook reste
    pour réinitialiser le singleton.
    """
    global _embedding_service
    if _embedding_service is not None:
        service = _embedding_service
        _embedding_service = None
        await service.close()
