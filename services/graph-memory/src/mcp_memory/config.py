# -*- coding: utf-8 -*-
"""
Configuration centralisée du service MCP Memory.

Utilise pydantic-settings pour charger et valider la configuration
depuis les variables d'environnement ou un fichier .env.
"""

from functools import lru_cache
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Configuration du service MCP Memory.
    
    Toutes les variables peuvent être définies via:
    - Variables d'environnement
    - Fichier .env à la racine du projet
    """
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )
    
    # =========================================================================
    # S3 Cloud Temple
    # =========================================================================
    s3_endpoint_url: str = "https://takinc5acc.s3.fr1.cloud-temple.com"
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_bucket_name: str = "quoteflow-memory"
    s3_region_name: str = "fr1"

    # P7-4 (Hivemind, ADR-0019 — see THIRD_PARTY_NOTICES.md): unified token
    # authority (Model B). The embedded GM validates the SAME tokens as Hivemind
    # by reading Hivemind's token store from the shared S3 bucket. The S3
    # signature mode for that read mirrors Hivemind's own `S3_SIGNATURE_MODE`
    # env (single source of truth, read directly by the validator) — there is no
    # separate GM-only knob that could diverge and brick auth on MinIO/AWS.
    hivemind_tokens_s3_key: str = "_system/tokens.json"

    # =========================================================================
    # LLMaaS Cloud Temple
    # =========================================================================
    llmaas_api_url: str = "https://api.ai.cloud-temple.com"
    llmaas_api_key: str
    llmaas_model: str = "gpt-oss:120b"
    llmaas_max_tokens: int = 60000  # gpt-oss:120b fait du chain-of-thought qui consomme beaucoup de tokens
    llmaas_temperature: float = 1.0  # gpt-oss:120b fonctionne mieux à température 1.0
    extraction_max_text_length: int = 950000  # Max chars du texte envoyé au LLM (défaut ~950K)
    extraction_chunk_size: int = 25000  # Max chars par chunk d'extraction graph (~6K tokens, laisse marge pour prompt+réponse)
    
    # =========================================================================
    # Embedding (LLMaaS)
    # =========================================================================
    llmaas_embedding_model: str = "bge-m3:567m"
    llmaas_embedding_dimensions: int = 1024  # Dimension des vecteurs BGE-M3
    
    # =========================================================================
    # Qdrant (base vectorielle)
    # =========================================================================
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection_prefix: str = "memory_"  # Préfixe pour les collections Qdrant
    
    # =========================================================================
    # Chunking sémantique
    # =========================================================================
    chunk_size: int = 500  # Taille cible en tokens par chunk
    chunk_overlap: int = 50  # Tokens de chevauchement entre chunks adjacents
    
    # =========================================================================
    # RAG — Recherche vectorielle
    # =========================================================================
    rag_score_threshold: float = 0.58  # Score cosinus minimum pour un chunk BGE-M3 (en dessous = ignoré)
    rag_chunk_limit: int = 8  # Nombre max de chunks retournés par Qdrant
    
    # =========================================================================
    # Neo4j
    # =========================================================================
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str
    neo4j_database: str = "neo4j"  # Base par défaut
    
    # =========================================================================
    # MCP Server
    # =========================================================================
    mcp_server_port: int = 8002
    mcp_server_host: str = "0.0.0.0"
    mcp_server_debug: bool = False
    mcp_server_name: str = "mcp-memory"
    
    # =========================================================================
    # Admin / Auth
    # =========================================================================
    admin_bootstrap_key: Optional[str] = None  # Pour créer le premier token
    
    # =========================================================================
    # Backup / Restore
    # =========================================================================
    s3_backup_prefix: str = "_backups"  # Préfixe S3 pour les backups
    backup_retention_count: int = 5     # Nombre max de backups conservés par mémoire (0 = illimité)
    
    # =========================================================================
    # Limites et timeouts
    # =========================================================================
    max_document_size_mb: int = 50
    extraction_timeout_seconds: int = 600  # 10 min par appel LLM (gros docs avec chain-of-thought)
    s3_upload_timeout_seconds: int = 60
    neo4j_query_timeout_seconds: int = 30

    # =========================================================================
    # Ingestion asynchrone (file de jobs in-memory best-effort)
    # =========================================================================
    ingest_max_history: int = 500          # Nombre max de jobs conservés en mémoire (trim des terminés)
    ingest_max_queued_per_memory: int = 200  # Jobs en attente max par mémoire (anti-saturation)
    ingest_max_queued_bytes: int = 300 * 1024 * 1024  # Octets décodés en file (global) avant rejet queue_full
    
    @property
    def llmaas_base_url(self) -> str:
        """URL complète pour le client OpenAI (compatible OpenAI)."""
        # L'URL doit pointer vers le endpoint compatible OpenAI
        # Cloud Temple: https://api.ai.cloud-temple.com (déjà avec /v1 intégré)
        return self.llmaas_api_url
    
    @property
    def max_document_size_bytes(self) -> int:
        """Taille max en bytes."""
        return self.max_document_size_mb * 1024 * 1024


@lru_cache()
def get_settings() -> Settings:
    """
    Retourne l'instance de configuration (singleton).
    
    Utilise lru_cache pour ne charger la config qu'une seule fois.
    
    Usage:
        from src.mcp_memory.config import get_settings
        settings = get_settings()
        print(settings.neo4j_uri)
    """
    return Settings()


# Pour usage direct: from config import settings
settings = get_settings()
