#!/usr/bin/env bash
# release_smoke.sh — Hivemind release-gate smoke test (operator-run pre-tag).
#
# Operationalizes ADR-0018 §Smoke (executable pre-tag check). Brings up the
# default compose stack (WAF + hivemind + embedded long runtime: graph-memory,
# neo4j, qdrant — plus the dev profile for MinIO), polls /health until 200
# through the WAF, round-trips one short_*, one mid_* and one long_* MCP tool
# against the live service, then tears the stack down. Exits non-zero on any
# failure so the operator can block the tag.
#
# Long-tier behavior follows ADR-0019: the embedded long runtime is a
# MANDATORY product component. A release smoke where the long tier reports
# any disabled/unbound/unreachable state is a FAILURE — there is no
# acceptable "long disabled" release shape. The smoke proves the embedded
# runtime end-to-end: a real long_push binds the space to the embedded
# Graph Memory (internal auto-provision, P7-3), long_status must report
# connected AND reachable, and a long_ingest dry-run must return a
# non-empty plan. ADR-0010 authority semantics are unchanged: long stays
# derived and non-authoritative; "mandatory" is a product-presence claim,
# not a protocol-authority claim.
#
# Required tooling on the operator host:
#   - docker (with the compose plugin)
#   - curl
#   - jq
#
# This script is operator-run, not CI-run, because it builds and tears down
# real containers and consumes local resources. CI runs the lints and the
# pytest suite; the smoke is a manual pre-tag step documented in
# docs/WORKFLOW_GIT_EPIC.md §"Smoke test (operator-run pre-tag)".

set -euo pipefail

# --- Configuration --------------------------------------------------------
#
# Default entrypoint is the WAF on :8080 (docker-compose.yml — the WAF is the
# only public entry; hivemind itself has no host port). Override the URLs only
# when smoking a remapped stack (e.g. WAF_PORT=9090).

HEALTH_URL="${HIVEMIND_HEALTH_URL:-http://localhost:8080/health}"
API_URL="${HIVEMIND_API_URL:-http://localhost:8080/api/tool}"
HEALTH_TIMEOUT_SECONDS="${HIVEMIND_HEALTH_TIMEOUT_SECONDS:-90}"
COMPOSE_PROFILE="${HIVEMIND_COMPOSE_PROFILE:-dev}"
SMOKE_SPACE_ID="${HIVEMIND_SMOKE_SPACE_ID:-smoke-test}"
# Non-volatile bank filename: long_push skips activeContext.md / progress.md
# (GRAPH_PUSH_VOLATILE_FILES), so the smoke pushes a canonical fact sheet.
SMOKE_BANK_FILE="smoke-canonical.md"
SMOKE_INGEST_SOURCE_PATH="release-smoke/canonical.md"

# Color-free logging so output is grep-friendly in CI logs.
log() { printf '[release-smoke] %s\n' "$*"; }
fail() { printf '[release-smoke][FAIL] %s\n' "$*" >&2; exit 1; }

# --- Pre-flight ------------------------------------------------------------

for bin in docker curl jq; do
  command -v "$bin" >/dev/null 2>&1 || fail "missing required tool: $bin"
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ ! -f "docker-compose.yml" ] && [ ! -f "compose.yml" ]; then
  fail "no docker-compose.yml or compose.yml at repo root ($REPO_ROOT)"
fi

# --- Token fail-closed (Codex P6-8 review #2) ------------------------------
#
# The bootstrap token is environment-dependent (HIVEMIND_BOOTSTRAP_TOKEN env
# var or the value injected by the compose dev profile). The operator MUST
# export it before invoking this script. The earlier "skip with warning and
# exit 0" path produced a false-green, so we fail closed: a missing token
# blocks the release-gate smoke. The token must be MANAGE-capable (the
# bootstrap admin token is): mid_write requires 'manage', and the first
# long_push auto-provisions the embedded binding (requires 'write').
if [ -z "${HIVEMIND_BOOTSTRAP_TOKEN:-}" ]; then
  echo "[release-smoke][FAIL] HIVEMIND_BOOTSTRAP_TOKEN is not set." >&2
  echo "[release-smoke][FAIL] Export the admin bootstrap key from your .env (HIVEMIND_BOOTSTRAP_TOKEN=\"\$ADMIN_BOOTSTRAP_KEY\")" >&2
  echo "[release-smoke][FAIL] or a manage-capable token (admin_create_token) and re-run." >&2
  exit 1
fi
TOKEN="$HIVEMIND_BOOTSTRAP_TOKEN"

# --- Cleanup trap ----------------------------------------------------------

cleanup() {
  rc=$?
  log "tearing down compose stack (profile=$COMPOSE_PROFILE)"
  docker compose --profile "$COMPOSE_PROFILE" down -v >/dev/null 2>&1 || true
  if [ "$rc" -ne 0 ]; then
    log "smoke failed with exit code $rc"
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

# --- 1. Bring up the stack -------------------------------------------------

log "compose up (profile=$COMPOSE_PROFILE, build; default stack includes embedded long runtime per ADR-0019)"
docker compose --profile "$COMPOSE_PROFILE" up -d --build

# --- 2. Wait for /health 200 -----------------------------------------------

log "polling $HEALTH_URL (timeout=${HEALTH_TIMEOUT_SECONDS}s)"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if curl -fsS -o /dev/null -w '%{http_code}' "$HEALTH_URL" 2>/dev/null | grep -q '^200$'; then
    log "/health returned 200"
    break
  fi
  sleep 2
done
if ! curl -fsS -o /dev/null -w '%{http_code}' "$HEALTH_URL" 2>/dev/null | grep -q '^200$'; then
  fail "/health did not return 200 within ${HEALTH_TIMEOUT_SECONDS}s"
fi

mcp_call() {
  # mcp_call <tool_name> <json_args>
  local tool="$1"
  local args="$2"
  curl -fsS \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"tool\":\"$tool\",\"arguments\":$args}" \
    "$API_URL"
}

# --- 3. space_create ------------------------------------------------------

log "creating smoke space '$SMOKE_SPACE_ID'"
space_create_response=$(mcp_call "space_create" "{\"space_id\":\"$SMOKE_SPACE_ID\",\"description\":\"Release-gate smoke space (auto-created by release_smoke.sh)\"}" || true)
# Real SpaceService.create() contract (core/space.py): "created" for a new
# space, "already_exists" for reuse — anything else is a failure.
status=$(printf '%s' "$space_create_response" | jq -r '.status // "missing"')
if [ "$status" != "created" ] && [ "$status" != "already_exists" ]; then
  fail "space_create returned unexpected status: $status (response: $space_create_response)"
fi

# --- 4. Round-trip short_* and mid_* ---------------------------------------
#
# Tool surfaces and required arguments are locked by
# tests/fixtures/tool_surface.json and the P6-3 tool-surface lint. The
# arguments below match the live registered signatures:
#   - short_note  (alias of live_note):  space_id, category, content
#   - mid_write   (alias of bank_write): space_id, filename, content (manage)
#   - mid_list    (alias of bank_list):  space_id

log "round-trip short_* (short_note)"
short_response=$(mcp_call "short_note" "{\"space_id\":\"$SMOKE_SPACE_ID\",\"category\":\"observation\",\"content\":\"smoke note\"}")
# Real note-creation contract (core/live.py): a successful short_note/live_note
# returns "created" (never "ok") — anything else is a failure. Same
# contract-exactness rule as the space_create check above (P7-5 Codex R1).
short_status=$(printf '%s' "$short_response" | jq -r '.status // "missing"')
if [ "$short_status" != "created" ]; then
  fail "short_note returned status='$short_status' (response: $short_response)"
fi

log "round-trip mid_* (mid_write: seed a canonical bank file for the long push)"
mid_write_response=$(mcp_call "mid_write" "{\"space_id\":\"$SMOKE_SPACE_ID\",\"filename\":\"$SMOKE_BANK_FILE\",\"content\":\"# Release smoke canonical file\\n\\nStable canonical content used by the release-gate long push.\\n\"}")
mid_write_status=$(printf '%s' "$mid_write_response" | jq -r '.status // "missing"')
if [ "$mid_write_status" != "ok" ]; then
  fail "mid_write returned status='$mid_write_status' (response: $mid_write_response)"
fi

log "round-trip mid_* (mid_list)"
mid_response=$(mcp_call "mid_list" "{\"space_id\":\"$SMOKE_SPACE_ID\"}")
mid_status=$(printf '%s' "$mid_response" | jq -r '.status // "missing"')
if [ "$mid_status" != "ok" ]; then
  fail "mid_list returned status='$mid_status' (response: $mid_response)"
fi

# --- 5. Long tier: REQUIRED embedded runtime (ADR-0019) --------------------
#
# The long tier is a mandatory product component. Every check below fails
# closed: any disabled/unbound/unreachable long state blocks the release.

# 5a. Real long_push — binds the space to the embedded runtime (P7-3
# auto-provision triggers ONLY from a real push) and proves end-to-end
# ingestion (Graph Memory + datastores + LLM extraction).
log "long_push (binds embedded runtime; real ingestion — may take ~10-30s/file)"
long_push_response=$(mcp_call "long_push" "{\"space_id\":\"$SMOKE_SPACE_ID\"}" || true)
long_push_status=$(printf '%s' "$long_push_response" | jq -r '.status // "missing"')
if [ "$long_push_status" != "ok" ]; then
  fail "long_push returned status='$long_push_status' — embedded long runtime must accept a real push (response: $long_push_response)"
fi
# Type-safe numeric assertions: jq -e exits non-zero unless the predicate is
# true, and the type check rejects a malformed non-numeric field (a shell
# `[ ... -lt 1 ]` on a non-numeric value would print an error but NOT fail
# the script under `set -e` inside an `if`).
if ! printf '%s' "$long_push_response" | jq -e '(.pushed | type == "number") and (.pushed >= 1)' >/dev/null; then
  fail "long_push must report a numeric pushed >= 1 (response: $long_push_response)"
fi
if ! printf '%s' "$long_push_response" | jq -e '(.errors | type == "number") and (.errors == 0)' >/dev/null; then
  fail "long_push must report a numeric errors == 0 (response: $long_push_response)"
fi

# 5b. long_status — the embedded runtime must be bound AND reachable.
# Disabled-state shapes are explicit FAILURES (ADR-0019 — no release ships
# with long disabled). The case below enumerates the legacy disabled-state
# tokens first so a regression to P6-5 semantics fails loudly, then accepts
# ONLY status=ok.
log "long_status (must report connected + reachable; disabled-state = release failure)"
long_response=$(mcp_call "long_status" "{\"space_id\":\"$SMOKE_SPACE_ID\"}" || true)
long_status=$(printf '%s' "$long_response" | jq -r '.status // "missing"')
case "$long_status" in
  disabled|long_disabled|not_configured|not_connected)
    fail "long_status returned disabled-state status='$long_status' — a disabled long tier blocks the release (ADR-0019) (response: $long_response)"
    ;;
  ok)
    ;;
  *)
    fail "long_status returned unexpected status='$long_status' (response: $long_response)"
    ;;
esac
long_connected=$(printf '%s' "$long_response" | jq -r '.connected // false')
long_reachable=$(printf '%s' "$long_response" | jq -r '.reachable // false')
if [ "$long_connected" != "true" ]; then
  fail "long_status reports connected=$long_connected; the embedded long runtime must be bound (response: $long_response)"
fi
if [ "$long_reachable" != "true" ]; then
  fail "long_status reports reachable=$long_reachable; the embedded long runtime must be reachable (response: $long_response)"
fi

# 5c. long_ingest dry-run — canonical-ingestion planning must return a
# non-empty plan for a non-empty document set (shape check, zero transport).
log "long_ingest dry-run (canonical plan shape check)"
long_ingest_response=$(mcp_call "long_ingest" "{\"space_id\":\"$SMOKE_SPACE_ID\",\"mode\":\"dry-run\",\"documents\":[{\"source_path\":\"$SMOKE_INGEST_SOURCE_PATH\",\"content\":\"Release smoke canonical document.\"}]}" || true)
long_ingest_status=$(printf '%s' "$long_ingest_response" | jq -r '.status // "missing"')
if [ "$long_ingest_status" != "ok" ]; then
  fail "long_ingest dry-run returned status='$long_ingest_status' (response: $long_ingest_response)"
fi
if ! printf '%s' "$long_ingest_response" | jq -e '(.planned | type == "array") and ((.planned | length) >= 1)' >/dev/null; then
  fail "long_ingest dry-run must return a non-empty planned array (response: $long_ingest_response)"
fi
planned_source_path=$(printf '%s' "$long_ingest_response" | jq -r '.planned[0].source_path // "missing"')
if [ "$planned_source_path" != "$SMOKE_INGEST_SOURCE_PATH" ]; then
  fail "long_ingest dry-run planned source_path='$planned_source_path'; expected '$SMOKE_INGEST_SOURCE_PATH' (response: $long_ingest_response)"
fi

log "release smoke OK (short + mid + REQUIRED embedded long all green)"
# Cleanup trap will tear down the stack.
