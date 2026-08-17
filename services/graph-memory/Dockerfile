# =============================================================================
# Hivemind embedded long runtime - Dockerfile
# =============================================================================
# Build   : docker build -f services/graph-memory/Dockerfile -t hivemind-graph-memory .
#           The build context is the REPOSITORY ROOT: the image installs the
#           exact shared `src/hivemind_inference` package consumed by the
#           Hivemind core, never a copied or reimplemented variant (#306,
#           P13-1C, ADR-0027). Compose and the public CI overlay wire this
#           context.
# Run     : Hivemind starts this internal-only service through Compose.
# =============================================================================

# Image officielle multi-architecture vérifiée le 2026-07-21. Toute mise à jour
# doit conserver un tag patch-level et un digest, puis repasser le build.
FROM python:3.14.7-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52

# Public artifact identity; upstream provenance remains in THIRD_PARTY_NOTICES.md.
LABEL org.opencontainers.image.title="Hivemind embedded long runtime"
LABEL org.opencontainers.image.description="Derived ontology and knowledge-graph runtime bundled with Hivemind"
LABEL org.opencontainers.image.version="3.2.0"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# Variables d'environnement Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Répertoire de travail
WORKDIR /app

# Dépendances Python : versions transitives et artefacts sont hash-lockés. Le
# pip fourni par l'image de base épinglée est utilisé tel quel, sans upgrade
# réseau flottant. Les images amd64/arm64 consomment uniquement des wheels.
COPY services/graph-memory/requirements.txt services/graph-memory/requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Créer un utilisateur non-root pour la sécurité
RUN groupadd -r mcp && useradd -r -g mcp -d /app -s /sbin/nologin mcp

# Copie des ontologies
COPY services/graph-memory/ONTOLOGIES/ ./ONTOLOGIES/

# Copie de la version et du code source
COPY services/graph-memory/VERSION .
COPY services/graph-memory/src/ ./src/

# P13-1C (ADR-0027) : frontière d'inférence partagée. L'image consomme le
# package EXACT du dépôt racine via PYTHONPATH=/app (même mécanisme que le code
# du runtime lui-même), jamais une copie divergente. Ses dépendances (httpx,
# python-dotenv) sont déjà hash-lockées dans requirements.lock.
COPY src/hivemind_inference/ ./hivemind_inference/

# Donner les droits à l'utilisateur mcp
RUN chown -R mcp:mcp /app

# Port exposé
EXPOSE 8002

# Passer en utilisateur non-root
USER mcp

# Readiness includes the process-local mutation-admission invariants.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8002/ready', timeout=2)" || exit 1

# Point d'entrée
ENTRYPOINT ["python", "-m", "src.mcp_memory.server"]
CMD ["--port", "8002"]
