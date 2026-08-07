#!/usr/bin/env bash
# Blocking Linux/Docker runtime proof for issue #183. All names are isolated;
# cleanup touches only resources created by this invocation.
set -Eeuo pipefail

ACTIVE_TEST_RUNNER="${HIVEMIND_ACTIVE_TEST_RUNNER:-}"
if [[ -n "$ACTIVE_TEST_RUNNER" ]]; then
  echo "refusing nested embedded-secret Docker suite; active runner is '$ACTIVE_TEST_RUNNER'" >&2
  exit 2
fi
export HIVEMIND_ACTIVE_TEST_RUNNER="embedded-secret-docker:$$"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "issue #183 runtime proof requires a Linux Docker host" >&2
  exit 1
fi
command -v docker >/dev/null
docker info >/dev/null
docker compose version >/dev/null

ENV_FILE="$REPO_ROOT/.env"
if [[ -e "$ENV_FILE" ]]; then
  echo "refusing to replace existing .env; run from an isolated checkout" >&2
  exit 1
fi
touch "$ENV_FILE"

RUN_KEY="${GITHUB_RUN_ID:-local}${GITHUB_RUN_ATTEMPT:-0}$$"
PRIMARY_PROJECT="hm183_${RUN_KEY}"
UNKNOWN_PROJECT="hm183_unknown_${RUN_KEY}"
MALFORMED_PROJECT="hm183_malformed_${RUN_KEY}"
NO_CHOWN_PROJECT="hm183_nochown_${RUN_KEY}"
PROJECTS=(
  "$PRIMARY_PROJECT"
  "$UNKNOWN_PROJECT"
  "$MALFORMED_PROJECT"
  "$NO_CHOWN_PROJECT"
)
HIVEMIND_PROOF_IMAGE="hivemind-issue-183-proof:${RUN_KEY}"
export HIVEMIND_PROOF_IMAGE
export NEO4J_PASSWORD="issue-183-proof-only"
export ADMIN_BOOTSTRAP_KEY="issue-183-proof-only"

OVERRIDE_FILE="$(mktemp "${TMPDIR:-/tmp}/hivemind-183-compose.XXXXXX.yml")"
printf '%s\n' \
  'services:' \
  '  hivemind-secrets-init:' \
  '    image: ${HIVEMIND_PROOF_IMAGE}' \
  '    pull_policy: never' \
  '  hivemind:' \
  '    image: ${HIVEMIND_PROOF_IMAGE}' \
  '    pull_policy: never' >"$OVERRIDE_FILE"

CONTAINERS=()
cleanup() {
  local container project
  set +e
  for container in "${CONTAINERS[@]}"; do
    docker rm -f "$container" >/dev/null 2>&1 || true
  done
  for project in "${PROJECTS[@]}"; do
    docker compose \
      --project-name "$project" \
      --file "$REPO_ROOT/docker-compose.yml" \
      --file "$OVERRIDE_FILE" \
      down --volumes --remove-orphans >/dev/null 2>&1 || true
  done
  docker image rm "$HIVEMIND_PROOF_IMAGE" >/dev/null 2>&1 || true
  rm -f "$OVERRIDE_FILE" "$ENV_FILE"
}
trap cleanup EXIT

compose_for() {
  local project="$1"
  shift
  docker compose \
    --project-name "$project" \
    --file "$REPO_ROOT/docker-compose.yml" \
    --file "$OVERRIDE_FILE" \
    "$@"
}

container_name() {
  printf 'hm183-%s-%s-%s' "$1" "$RUN_KEY" "$2"
}

seed_volume() {
  local project="$1" kind="$2" name
  name="$(container_name seed "$kind")"
  compose_for "$project" run --rm --no-deps -T --name "$name" \
    hivemind-secrets-init \
    python /app/scripts/verify_embedded_secret_container.py seed "$kind"
}

run_profiled_init() {
  local project="$1" suffix="$2" name
  name="$(container_name init "$suffix")"
  compose_for "$project" run --rm --no-deps -T --name "$name" \
    hivemind-secrets-init \
    python /app/scripts/verify_embedded_secret_container.py init
}

expect_init_failure() {
  local project="$1" suffix="$2" output
  if output="$(run_profiled_init "$project" "$suffix" 2>&1)"; then
    printf '%s\n' "$output"
    echo "initializer unexpectedly accepted $suffix" >&2
    exit 1
  fi
  printf '%s\n' "$output"
  echo "PROOF_INIT_REJECTED kind=$suffix"
}

expect_entry_retained() {
  local project="$1" kind="$2" volume
  volume="${project}_hivemind_secrets"
  docker run --rm --network none --read-only \
    --user 0:0 --cap-drop ALL \
    --volume "$volume:/data/secrets" \
    "$HIVEMIND_PROOF_IMAGE" \
    python /app/scripts/verify_embedded_secret_container.py expect-entry "$kind"
}

echo "PROOF_BUILD isolated_image=$HIVEMIND_PROOF_IMAGE"
compose_for "$PRIMARY_PROJECT" build hivemind-secrets-init

# Quiescent upgrade: model the affected process against the pre-repair root-owned
# volume, stop it, and prove it is not running before invoking the initializer.
seed_volume "$PRIMARY_PROJECT" valid-orphan
OLD_NAME="$(container_name old process)"
CONTAINERS+=("$OLD_NAME")
compose_for "$PRIMARY_PROJECT" run -d --no-deps -T --name "$OLD_NAME" \
  hivemind \
  python /app/scripts/verify_embedded_secret_container.py legacy-wait >/dev/null
old_ready=false
for _attempt in {1..30}; do
  if docker logs "$OLD_NAME" 2>&1 | grep -q PROOF_QUIESCENCE_OLD_PROCESS_READY; then
    old_ready=true
    break
  fi
  sleep 1
done
docker logs "$OLD_NAME"
if [[ "$old_ready" != true ]]; then
  echo "old-process quiescence probe did not become ready" >&2
  exit 1
fi
docker stop --time 10 "$OLD_NAME" >/dev/null
if [[ "$(docker inspect --format '{{.State.Running}}' "$OLD_NAME")" != false ]]; then
  echo "old process is still running before volume repair" >&2
  exit 1
fi
echo "PROOF_QUIESCENCE_OK old_process_running=false before_initializer=true"
docker rm "$OLD_NAME" >/dev/null

# Exact Compose init profile: uid 0, CapEff=CAP_CHOWN only, read-only rootfs,
# no-new-privileges, no network. The valid crash orphan must be removed.
run_profiled_init "$PRIMARY_PROJECT" primary
INSPECT_NAME="$(container_name inspect initialized)"
compose_for "$PRIMARY_PROJECT" run --rm --no-deps -T --name "$INSPECT_NAME" \
  hivemind \
  python /app/scripts/verify_embedded_secret_container.py inspect-initialized

# Generate and persist as the exact main service profile, remove the stopped
# one-off container, recreate it, then require the same plaintext fingerprint.
MAIN_NAME="$(container_name main recreate)"
CONTAINERS+=("$MAIN_NAME")
write_output="$(compose_for "$PRIMARY_PROJECT" run --no-deps -T --name "$MAIN_NAME" \
  hivemind \
  python /app/scripts/verify_embedded_secret_container.py main-write 2>&1)"
printf '%s\n' "$write_output"
write_sha="$(printf '%s\n' "$write_output" | sed -n 's/^PROOF_SHA=\([0-9a-f]\{64\}\)$/\1/p')"
[[ -n "$write_sha" ]]
first_container_id="$(docker inspect --format '{{.Id}}' "$MAIN_NAME")"
docker rm "$MAIN_NAME" >/dev/null

# Create and acquire the real Mesh process-identity lock under the exact main
# profile (uid 10001, no capabilities) before exercising the restart path.
MESH_CREATE_NAME="$(container_name mesh create)"
compose_for "$PRIMARY_PROJECT" run --rm --no-deps -T \
  --name "$MESH_CREATE_NAME" hivemind \
  python /app/scripts/verify_embedded_secret_container.py mesh-lock-create

# Run the unmodified production initializer command against the populated volume
# with its exact uid-0/CAP_CHOWN-only profile. It must accept and repair both the
# embedded-token files and the retained Mesh process lock.
PRODUCTION_INIT_NAME="$(container_name init production-command)"
compose_for "$PRIMARY_PROJECT" run --rm --no-deps -T \
  --name "$PRODUCTION_INIT_NAME" hivemind-secrets-init

# Back under the exact main profile, verify the initializer preserved the strict
# lock metadata/content contract and prove that a fresh process can reacquire it.
MESH_REACQUIRE_NAME="$(container_name mesh reacquire)"
compose_for "$PRIMARY_PROJECT" run --rm --no-deps -T \
  --name "$MESH_REACQUIRE_NAME" hivemind \
  python /app/scripts/verify_embedded_secret_container.py mesh-lock-reacquire

read_output="$(compose_for "$PRIMARY_PROJECT" run --no-deps -T --name "$MAIN_NAME" \
  hivemind \
  python /app/scripts/verify_embedded_secret_container.py main-read 2>&1)"
printf '%s\n' "$read_output"
read_sha="$(printf '%s\n' "$read_output" | sed -n 's/^PROOF_SHA=\([0-9a-f]\{64\}\)$/\1/p')"
[[ "$read_sha" == "$write_sha" ]]
second_container_id="$(docker inspect --format '{{.Id}}' "$MAIN_NAME")"
[[ "$second_container_id" != "$first_container_id" ]]
echo "PROOF_RECREATE_OK same_plaintext_sha=true different_container_id=true"
docker rm "$MAIN_NAME" >/dev/null

# Unknown entries and malformed crash-orphan names are retained and rejected.
seed_volume "$UNKNOWN_PROJECT" unknown-entry
expect_init_failure "$UNKNOWN_PROJECT" unknown-entry
expect_entry_retained "$UNKNOWN_PROJECT" unknown-entry

seed_volume "$MALFORMED_PROJECT" malformed-orphan
expect_init_failure "$MALFORMED_PROJECT" malformed-orphan
expect_entry_retained "$MALFORMED_PROJECT" malformed-orphan

# CAP_CHOWN is necessary: the same root-owned fixture cannot be handed to uid
# 10001 when all capabilities are dropped.
seed_volume "$NO_CHOWN_PROJECT" valid-orphan
NO_CHOWN_VOLUME="${NO_CHOWN_PROJECT}_hivemind_secrets"
if no_chown_output="$(docker run --rm --network none --read-only \
  --security-opt no-new-privileges:true \
  --user 0:0 --cap-drop ALL \
  --volume "$NO_CHOWN_VOLUME:/data/secrets" \
  "$HIVEMIND_PROOF_IMAGE" \
  python /app/scripts/verify_embedded_secret_container.py \
  init --expected-cap-eff 0 2>&1)"; then
  printf '%s\n' "$no_chown_output"
  echo "initializer unexpectedly repaired ownership without CAP_CHOWN" >&2
  exit 1
fi
printf '%s\n' "$no_chown_output"
echo "PROOF_CAP_CHOWN_REQUIRED"

# procfs is outside the reviewed local-filesystem allowlist and must fail before
# any credential publication.
if unsupported_output="$(docker run --rm --network none --read-only \
  --security-opt no-new-privileges:true \
  --user 0:0 --cap-drop ALL --cap-add CHOWN \
  --volume /proc:/data/secrets:ro \
  "$HIVEMIND_PROOF_IMAGE" \
  python /app/scripts/verify_embedded_secret_container.py init 2>&1)"; then
  printf '%s\n' "$unsupported_output"
  echo "initializer unexpectedly accepted unsupported procfs" >&2
  exit 1
fi
printf '%s\n' "$unsupported_output"
echo "PROOF_UNSUPPORTED_FILESYSTEM_REJECTED fs=procfs"

echo "PROOF_ISSUE_183_DOCKER_OK"
