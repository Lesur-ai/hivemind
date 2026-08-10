# =============================================================================
# Dockerfile — Hivemind MCP Server (multi-stage, rootless)
# =============================================================================
# Public artifact: Hivemind (ADR-0018). Internal Python module path remains
# `live_mem` — see ADR-0018 §"Rebrand the public artifact, freeze the
# internal module tree". CMD below intentionally keeps `python -m live_mem`.
# =============================================================================
# Two-stage build:
#   1. Builder — installs dependencies via uv (frozen lockfile)
#   2. Runtime — copies only the venv + source code (no build tools)
#
# Usage :
#   docker compose build
#   docker compose up -d
# =============================================================================

# ─────────────────────────────────────────────────────────────
# Stage 1: Builder — install dependencies via uv
# ─────────────────────────────────────────────────────────────
# Registry indexes verified 2026-07-21. Keep the human-readable tag and the
# multi-architecture digest together; review and build-test every bump.
FROM ghcr.io/astral-sh/uv:0.9.30-python3.14-bookworm-slim@sha256:7cf77f594be8042dab6daa9fe326f90962252268b4f120a7f5dccce4d947e6c1 AS builder

WORKDIR /app

ENV UV_PROJECT_ENVIRONMENT="/opt/venv"

# 1) Install deps only (cached layer — only invalidated when lockfile changes)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-install-workspace

# 2) Install the project itself
COPY VERSION .
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ─────────────────────────────────────────────────────────────
# Stage 2: Runtime — lean production image
# ─────────────────────────────────────────────────────────────
FROM python:3.15.0rc1-slim-bookworm@sha256:6e3246a49a188d62360dcd248aafbc1834db4d86eff6b28f40ba13269c1bcc57

WORKDIR /app

# Créer l'utilisateur non-root et le mountpoint secret AVANT tout COPY. Le
# mountpoint image couvre les exécutions sans volume ; Compose répare aussi les
# volumes nommés existants avec son init one-shot avant de lancer Hivemind.
RUN useradd -r -u 10001 -s /bin/false mcp \
    && install -d -o mcp -g mcp -m 0700 /data/secrets

# Copy virtual environment from builder (no pip/setuptools in runtime)
COPY --from=builder --chown=mcp:mcp /opt/venv /opt/venv

# Code source — copié directement avec les bons droits
COPY --chown=mcp:mcp src/ src/
COPY --chown=mcp:mcp scripts/ scripts/
COPY --chown=mcp:mcp RULES/ RULES/
COPY --chown=mcp:mcp VERSION .

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Basculer sur l'utilisateur non-root (rootless)
USER mcp

EXPOSE 8002

# Healthcheck : vérifier que le serveur répond sur /health
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8002/health', timeout=2)" || exit 1

# Point d'entrée : le serveur MCP
CMD ["python", "-m", "live_mem"]
