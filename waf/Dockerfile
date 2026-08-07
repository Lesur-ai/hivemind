# =============================================================================
# WAF Dockerfile — Caddy + Coraza WAF + OWASP CRS + Rate Limiting
# =============================================================================
# Build multi-stage :
# 1. Compile Caddy avec les plugins coraza-caddy et caddy-ratelimit via xcaddy
# 2. Image finale caddy:2-alpine avec le binaire custom
# =============================================================================

# Entrées supply-chain immuables, vérifiées le 2026-07-19 : images officielles
# Caddy v2.11.4 (index multi-architecture), Coraza Caddy v2.5.0 et commit
# caddy-ratelimit testé. Toute mise à jour doit conserver les digests/commits.

# Stage 1 : Build Caddy avec plugins
FROM caddy:2.11.4-builder@sha256:198d47eaee306d4d0c38a9960c89ff2c959aa29ad51d3e2dafa3e93ac961782a AS builder

RUN xcaddy build \
    --with github.com/corazawaf/coraza-caddy/v2@55c03d45dc552b6bbb493908d1af5d8969e864e0 \
    --with github.com/mholt/caddy-ratelimit@5625512f24f6f59d6f64fb3aafe5eecff0b286db

# Stage 2 : Image finale
FROM caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648

# Copier le binaire Caddy custom (avec Coraza + Rate Limit)
COPY --from=builder /usr/bin/caddy /usr/bin/caddy

# RA-2 : exécuter Caddy en NON-root (le seul conteneur exposé publiquement
# tournait en root). On donne au binaire la capacité de binder des ports
# privilégiés (80/443 en prod TLS) via file-capability, et on approprie /data et
# /config (stockage des certificats Let's Encrypt) à l'uid non-root, pour que le
# mode TLS prod fonctionne aussi. NB : à build-tester avant déploiement.
RUN apk add --no-cache libcap=2.78-r0 \
    && setcap cap_net_bind_service=+ep /usr/bin/caddy \
    && mkdir -p /data /config \
    && chown -R 10001:10001 /data /config

# Conserver une configuration exacte dans l'image. Le Compose produit peut la
# remplacer par son bind read-only pour les éditions opérateur, tandis que les
# exécutions sans ce bind utilisent ce fallback construit depuis le même SHA.
# La copie précède l'utilisateur non-root afin que Caddy puisse la lire.
COPY Caddyfile /etc/caddy/Caddyfile

USER 10001:10001
