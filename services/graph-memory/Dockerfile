# =============================================================================
# Hivemind embedded long runtime - Dockerfile
# =============================================================================
# Build   : docker build -t hivemind-graph-memory .
# Run     : Hivemind starts this internal-only service through Compose.
# =============================================================================

# Image officielle multi-architecture vérifiée le 2026-07-19. Toute mise à jour
# doit conserver un tag patch-level et un digest, puis repasser le build.
FROM python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30

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
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Créer un utilisateur non-root pour la sécurité
RUN groupadd -r mcp && useradd -r -g mcp -d /app -s /sbin/nologin mcp

# Copie des ontologies
COPY ONTOLOGIES/ ./ONTOLOGIES/

# Copie de la version et du code source
COPY VERSION .
COPY src/ ./src/

# Donner les droits à l'utilisateur mcp
RUN chown -R mcp:mcp /app

# Port exposé
EXPOSE 8002

# Passer en utilisateur non-root
USER mcp

# Healthcheck via /health endpoint, sans paquet système additionnel.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8002/health', timeout=2)" || exit 1

# Point d'entrée
ENTRYPOINT ["python", "-m", "src.mcp_memory.server"]
CMD ["--port", "8002"]
