#!/usr/bin/env bash
# ==============================================================================
# Script de compilation pour l'outil Go standalone hivemind-ingest (Lot 13.2)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SRC_DIR="${ROOT_DIR}/tools/hivemind-ingest"
DIST_DIR="${ROOT_DIR}/bin"

mkdir -p "${DIST_DIR}"

echo "==> Compilation de hivemind-ingest..."

if ! command -v go >/dev/null 2>&1; then
    echo "Erreur: Go (Golang) n'est pas installé ou absent du PATH." >&2
    exit 1
fi

cd "${SRC_DIR}"

# Optionnel: exécuter les tests avant build
if [[ "${1:-}" == "--test" || "${RUN_TESTS:-false}" == "true" ]]; then
    echo "==> Exécution des tests unitaires Go..."
    go test ./... -v
fi

# Déterminer la version et les métadonnées de build
GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
BUILD_DATE="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
VERSION="v$(tr -d '\r\n' < "${ROOT_DIR}/VERSION")"

LDFLAGS="-s -w -X main.Version=${VERSION} -X main.Commit=${GIT_COMMIT} -X main.BuildDate=${BUILD_DATE}"

echo "==> Build binaire natif (${VERSION}, commit ${GIT_COMMIT})..."
CGO_ENABLED=0 go build -ldflags "${LDFLAGS}" -o "${DIST_DIR}/hivemind-ingest" .

echo "==> Binaire produit avec succès dans ${DIST_DIR}/hivemind-ingest"
OUTPUT_VERSION="$("${DIST_DIR}/hivemind-ingest" version 2>&1 || true)"
echo "==> Version rapportée: ${OUTPUT_VERSION}"
if [[ "${OUTPUT_VERSION}" != *"${VERSION}"* ]]; then
    echo "Erreur: la version attendue ${VERSION} n'a pas été trouvée dans la sortie: '${OUTPUT_VERSION}'" >&2
    exit 1
fi
if [[ "${GIT_COMMIT}" != "unknown" && "${OUTPUT_VERSION}" != *"${GIT_COMMIT}"* ]]; then
    echo "Erreur: le commit attendu ${GIT_COMMIT} n'a pas été trouvé dans la sortie: '${OUTPUT_VERSION}'" >&2
    exit 1
fi
if [[ "${OUTPUT_VERSION}" != *"${BUILD_DATE}"* ]]; then
    echo "Erreur: la date de build attendue ${BUILD_DATE} n'a pas été trouvée dans la sortie: '${OUTPUT_VERSION}'" >&2
    exit 1
fi
echo "==> Vérification de version, commit et date réussie."
echo "==> Terminé."
