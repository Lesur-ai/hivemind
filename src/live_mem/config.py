# -*- coding: utf-8 -*-
"""
Configuration du service MCP Live Memory via pydantic-settings.

Toutes les variables sont chargées depuis :
1. Variables d'environnement (priorité haute)
2. Fichier .env (priorité basse)

Usage :
    from .config import get_settings
    settings = get_settings()
    print(settings.s3_bucket_name)
"""

import logging
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

from .core.models import EMBEDDED_TOKEN_SENTINEL

_logger = logging.getLogger("live_mem.config")


class Settings(BaseSettings):
    """
    Configuration chargée depuis les variables d'env / .env.

    Includes startup validation that fails fast on misconfiguration.
    """

    # ─── Serveur MCP ───────────────────────────────────────────
    mcp_server_name: str = "Hivemind"
    mcp_server_host: str = "0.0.0.0"
    mcp_server_port: int = 8002
    mcp_server_debug: bool = False

    # ─── Project Mesh feature gate (public, no key material) ───
    # Only the non-secret gate enters Pydantic Settings so `.env` and process
    # environment precedence remain identical to the rest of the service.  The
    # seven enabled-mode values, especially the private key, are loaded lazily
    # into the opaque Mesh config and never become Settings fields.
    # Mesh is enabled unless the operator explicitly opts out with ``false``.
    # The remaining identity settings stay lazy and fail closed at app startup.
    hivemind_mesh_enabled: str = "true"

    @field_validator("hivemind_mesh_enabled", mode="before")
    @classmethod
    def _validate_mesh_feature_gate(cls, value: object) -> str:
        if type(value) is not str or value not in {"true", "false"}:
            raise ValueError(
                "HIVEMIND_MESH_ENABLED must be exactly 'true' or 'false'"
            )
        return value

    # ─── Auth ──────────────────────────────────────────────────
    # Clé bootstrap pour le premier accès admin.
    # ⚠️ Changer impérativement en production !
    admin_bootstrap_key: str = "change_me_in_production"

    # ─── S3 — Stockage objets ─────────────────────────────────
    # Live Memory supporte deux modes de signature S3 :
    #   - "dual" (défaut) : SigV2 pour PUT/GET/DELETE/COPY, SigV4 pour
    #     HEAD/LIST. Requis pour Dell ECS Cloud Temple — voir
    #     CLOUD_TEMPLE_SERVICES.md.
    #   - "sigv4" : SigV4 pour toutes les opérations. Recommandé pour
    #     MinIO, AWS S3, et tout provider S3-compatible moderne (SigV2
    #     est déprécié AWS depuis 2018 et non supporté par MinIO).
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket_name: str = "live-mem"
    s3_region_name: str = "fr1"
    s3_signature_mode: str = "dual"

    # ─── LLMaaS Cloud Temple ──────────────────────────────────
    # API OpenAI-compatible. L'URL INCLUT déjà /v1 — ne pas l'ajouter.
    llmaas_api_url: str = ""
    llmaas_api_key: str = ""
    llmaas_model: str = "qwen3.5:27b"
    llmaas_context_window: int = (
        131072  # Taille totale du context window du modèle (input + output)
    )
    llmaas_max_tokens: int = 16384  # Max tokens de SORTIE demandés à l'API
    llmaas_temperature: float = 0.3

    # ─── Long embarqué (Graph Memory) — P7-3 / ADR-0019 ───────
    # URL INTERNE du runtime long embarqué : le hostname du service compose,
    # PAS localhost ni une IP littérale (une IP tripperait la garde SSRF).
    # Consommé par l'auto-bind interne (graph_bridge._resolve_or_embedded).
    long_embedded_url: str = "http://graph-memory:8002"
    # Secret LOCAL-ONLY présenté au runtime long embarqué. Vide par défaut :
    # résolu et persisté avant readiness (env → fichier local durable → création
    # atomique, cf. core/embedded_secret.py). JAMAIS écrit dans les métadonnées
    # partagées, commits, ou backups partagés (ADR-0010 / ADR-0012). Ne doit
    # JAMAIS valoir le sentinel (sinon le sentinel deviendrait un bearer vivant).
    long_embedded_token: str = ""
    # Fichier local durable où le token embarqué CRÉÉ au démarrage est persisté
    # en 0600 pour survivre aux redémarrages. Local-only, hors de tout espace S3.
    long_embedded_token_file: str = "/data/secrets/long_embedded_token"

    # ─── Proxy HTTP sortant ───────────────────────────────────
    # Variable custom (pas HTTP_PROXY/HTTPS_PROXY) pour ne pas affecter
    # toutes les libs Python qui lisent automatiquement les vars d'env OS.
    # Injecté manuellement dans boto3 (S3) et httpx (LLM).
    # Non supporté pour les connexions Graph Memory (streamablehttp_client).
    proxy_url: str | None = None

    @field_validator("proxy_url", mode="before")
    @classmethod
    def _normalize_proxy_url(cls, v: str | None) -> str | None:
        """Normalise proxy_url : strip whitespace, retourne None si vide."""
        if v is None:
            return None
        stripped = str(v).strip()
        return stripped if stripped else None

    # ─── Rules par défaut ─────────────────────────────────────
    # Chemin vers le fichier Markdown utilisé comme rules par défaut
    # quand space_create est appelé sans paramètre rules.
    # Ex: RULES/live-mem.standard.memory.bank.md (relatif au CWD)
    # ou /app/RULES/live-mem.standard.memory.bank.md (absolu dans Docker)
    default_rules_file: str = ""

    # ─── Consolidation ────────────────────────────────────────
    consolidation_timeout: int = 600  # Timeout par appel LLM (secondes)
    # LM2-14 fix : limite revue à la baisse pour brider la conso budget LLM.
    # 200 = ~1 MB d'input LLM si chaque note fait 5 KB ; ~10 MB si 50 KB.
    # Au-delà, l'auto-compact bank prend le relais. Une note massive reste
    # bornée par MAX_NOTE_CONTENT_SIZE (100 KB) côté live.py.
    consolidation_max_notes: int = 200  # Max notes traitées par consolidation
    consolidation_batch_size: int = (
        5  # Notes par lot LLM (réponses courtes = moins de drift)
    )
    # LM2-18 fix : cooldown entre deux consolidations du même space.
    # Empêche un agent write de boucler sur bank_consolidate et de
    # saturer le budget LLM ou de monopoliser le lock du space.
    # 60s = ~1 consolidation/min/space max, largement suffisant pour
    # un flux de travail humain. Mettre à 0 pour désactiver (déconseillé).
    consolidation_cooldown_seconds: int = 60

    # Issue #17 — Post-consolidation validation pass (opt-in).
    # When enabled, after each consolidated batch the server counts the
    # "claims" (numeric facts, metrics, dates, refs) in the modified bank
    # that do NOT appear in any note of the batch. The counter
    # `unattributed_claims_count` is surfaced in the bank_consolidate
    # response for observability.
    # Code-only approach (regex + pattern matching): no LLM tokens spent,
    # deterministic, easy to reason about. Some false positives are
    # possible on structurally unchanged content — see
    # _validate_unattributed_claims() for the heuristic details.
    # Disabled by default to keep existing consolidations unaffected;
    # enable for observability/CI deployments.
    consolidation_validation_enabled: bool = False
    # Cap on reported claims (only the first few are returned, to bound
    # the payload size sent back to the MCP caller).
    consolidation_validation_max_examples: int = 20



    # ─── Bank Compaction ──────────────────────────────────────
    # Compaction automatique des fichiers bank avant consolidation
    # quand le contexte total est trop gros pour le LLM.
    # Voir DESIGN/live-mem/CONTEXT_COMPACTION.md pour les détails.
    compact_threshold: float = (
        0.6  # Ratio input/max_tokens au-delà duquel on compacte (0.6 = 60%)
    )
    bank_file_max_size: int = (
        15360  # Taille max universelle pour tout fichier bank (bytes)
    )

    # ─── Graph Push — Volatile-file guardrail (P4-8) ──────────
    # Fichiers bank "volatils" que `graph_push` SAUTE par défaut : ce sont des
    # snapshots transitoires (focus de session, journal récent borné) que le
    # consolidateur réécrit/compacte/élague en continu. Les indexer dans Graph
    # Memory enseigne au graphe du contenu déjà périmé, et une compaction
    # ultérieure les laisse orphelins (voir
    # DESIGN/live-mem/EVOLUTION_LIVE_GRAPH_INTEGRATION.md, Vague B).
    #
    # Le filtre s'applique sur le BASENAME du relpath normalisé (après
    # bank_relpath), donc "1.MEMORY_BANK/activeContext.md" est filtré aussi.
    # Le forçage (include_volatile=True) requiert la permission 'manage' et émet
    # un audit structuré 'graph_push_volatile_optin' (tool layer). Déconseillé.
    # Clé d'env : GRAPH_PUSH_VOLATILE_FILES.
    graph_push_volatile_files: tuple[str, ...] = ("activeContext.md", "progress.md")

    # ─── S3 chiffrement at-rest (LM2-15 fix) ─────────────────
    # Si configuré, applique le header `ServerSideEncryption` sur
    # tous les `put_object`. Valeurs typiques :
    #   - "" / None : aucune (compatible Dell ECS sans SSE)
    #   - "AES256"  : SSE-S3 (chiffrement géré par S3)
    #   - "aws:kms" : SSE-KMS (clé KMS, nécessite S3_SSE_KMS_KEY_ID)
    # Sur Dell ECS Cloud Temple, le chiffrement at-rest est déjà géré
    # au niveau cluster. Cette option ajoute une couche applicative
    # explicite pour les déploiements multi-cibles (S3 AWS, MinIO).
    s3_sse: str | None = None
    s3_sse_kms_key_id: str | None = None  # Optionnel, requis si s3_sse=aws:kms

    # ─── Response limits ──────────────────────────────────────
    response_max_bytes: int = 512 * 1024  # Max response body size (512 KB)

    # ─── Admin console /api/tool (ADM-05 fix) ─────────────
    api_tool_max_body_bytes: int = 1_048_576  # Max request body for /api/tool (1 MB)

    # ─── Admin console audit ring (P8-6, G1) ──────────────
    admin_audit_ring_size: int = 500  # Per-instance entries (validated 1..500)

    # extra="ignore" permet d'avoir des variables dans .env (SITE_ADDRESS, WAF_PORT)
    # qui ne sont pas déclarées dans Settings (utilisées par Docker/Caddy uniquement)
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @model_validator(mode="after")
    def _validate_config(self) -> "Settings":
        """Semantic validation — fail fast at startup on misconfiguration."""
        errors: list[str] = []

        # Port range
        if not (1 <= self.mcp_server_port <= 65535):
            errors.append(
                f"MCP_SERVER_PORT={self.mcp_server_port} out of range [1, 65535]"
            )

        # S3: all-or-nothing (all three must be set, or none)
        s3_fields = [
            self.s3_endpoint_url,
            self.s3_access_key_id,
            self.s3_secret_access_key,
        ]
        s3_set = [bool(f) for f in s3_fields]
        if any(s3_set) and not all(s3_set):
            errors.append(
                "S3 partially configured — set all of S3_ENDPOINT_URL, "
                "S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY or none"
            )

        # S3 endpoint URL format
        if self.s3_endpoint_url and not self.s3_endpoint_url.startswith(
            ("http://", "https://")
        ):
            errors.append(
                f"S3_ENDPOINT_URL must start with http:// or https://, "
                f"got '{self.s3_endpoint_url[:50]}'"
            )

        # Bucket name: S3 naming rules (3-63 chars, lowercase, no underscore)
        if self.s3_bucket_name:
            import re

            if not re.match(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", self.s3_bucket_name):
                _logger.warning(
                    "S3_BUCKET_NAME='%s' may not be a valid S3 bucket name",
                    self.s3_bucket_name,
                )

        # S3 signature mode
        if self.s3_signature_mode not in ("dual", "sigv4"):
            errors.append(
                f"S3_SIGNATURE_MODE='{self.s3_signature_mode}' invalid — "
                "must be 'dual' (Dell ECS Cloud Temple) or 'sigv4' "
                "(MinIO / AWS S3 / other S3-compatible providers)"
            )

        # LLM: API key without URL or vice versa
        if bool(self.llmaas_api_url) != bool(self.llmaas_api_key):
            errors.append(
                "LLMaaS partially configured — set both LLMAAS_API_URL "
                "and LLMAAS_API_KEY or neither"
            )

        # Long embarqué (P7-3) : URL http(s):// si renseignée.
        if self.long_embedded_url and not self.long_embedded_url.startswith(
            ("http://", "https://")
        ):
            errors.append(
                f"LONG_EMBEDDED_URL must start with http:// or https://, "
                f"got '{self.long_embedded_url[:50]}'"
            )
        # Fail-closed : le token embarqué ne doit JAMAIS valoir le sentinel,
        # sinon le sentinel persisté deviendrait un bearer vivant (P7-3 R3).
        if self.long_embedded_token == EMBEDDED_TOKEN_SENTINEL:
            errors.append(
                "LONG_EMBEDDED_TOKEN must not equal the reserved embedded "
                f"sentinel '{EMBEDDED_TOKEN_SENTINEL}'"
            )

        # Consolidation ranges
        if self.consolidation_timeout < 10:
            errors.append(
                f"CONSOLIDATION_TIMEOUT={self.consolidation_timeout} too low (min 10s)"
            )
        if self.consolidation_max_notes < 1:
            errors.append(
                f"CONSOLIDATION_MAX_NOTES={self.consolidation_max_notes} must be ≥1"
            )
        if self.consolidation_batch_size < 1:
            errors.append(
                f"CONSOLIDATION_BATCH_SIZE={self.consolidation_batch_size} must be ≥1"
            )

        # Temperature range
        if not (0.0 <= self.llmaas_temperature <= 2.0):
            errors.append(
                f"LLMAAS_TEMPERATURE={self.llmaas_temperature} out of range [0.0, 2.0]"
            )

        # Proxy URL format (optionnel — si renseigné doit être une URL valide)
        if self.proxy_url and not self.proxy_url.startswith(("http://", "https://")):
            errors.append(
                f"PROXY_URL must start with http:// or https://, "
                f"got '{self.proxy_url[:50]}'"
            )

        # Response limit
        if self.response_max_bytes < 1024:
            errors.append(
                f"RESPONSE_MAX_BYTES={self.response_max_bytes} too low (min 1024)"
            )

        # The 500-entry ceiling pairs with the ring's 900-byte hard entry
        # budget, keeping a full snapshot below the default 512 KiB response.
        if not (1 <= self.admin_audit_ring_size <= 500):
            errors.append(
                "ADMIN_AUDIT_RING_SIZE="
                f"{self.admin_audit_ring_size} out of range [1, 500]"
            )

        if errors:
            msg = "Configuration errors at startup:\n  - " + "\n  - ".join(errors)
            raise ValueError(msg)

        return self


@lru_cache()
def get_settings() -> Settings:
    """Singleton Settings (cached)."""
    return Settings()
