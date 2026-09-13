#!/usr/bin/env bash
# Read-only preflight for a locally configured FlowWeave remote deployment target.
# It validates the immutable local commit and then verifies the declared remote
# Compose entry point through read-only SSH and Docker Compose commands.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/verify-remote-deploy.sh --config <local-env-file> --commit <SHA> --scope <web|platform|runtime|other>

This command is read-only. It validates an immutable local Git commit, then
uses SSH only to verify the declared remote Compose entry point and its service
topology. It never builds images, recreates services, changes files, or runs
Docker commands that mutate remote state. The config file is private and must
not be committed. Start from deploy/remote-deploy.env.example.
EOF
}

require_relative_path() {
  local name="$1" value="$2"
  [[ -n "$value" ]] || { echo "Missing $name in local remote config" >&2; exit 1; }
  [[ "$value" != /* && "$value" != *'..'* ]] || {
    echo "$name must be a non-empty path relative to FLOWWEAVE_REMOTE_ROOT without '..'" >&2
    exit 1
  }
}

require_optional_relative_path() {
  local name="$1" value="$2"
  [[ -z "$value" ]] && return
  [[ "$value" != /* && "$value" != *'..'* ]] || {
    echo "$name must be empty or a path relative to FLOWWEAVE_REMOTE_ROOT without '..'" >&2
    exit 1
  }
}

commit=""
scope=""
config=""
while (($#)); do
  case "$1" in
    --config)
      (($# >= 2)) || { echo "--config requires a local env file" >&2; exit 2; }
      config="$2"
      shift 2
      ;;
    --commit)
      (($# >= 2)) || { echo "--commit requires a SHA" >&2; exit 2; }
      commit="$2"
      shift 2
      ;;
    --scope)
      (($# >= 2)) || { echo "--scope requires a value" >&2; exit 2; }
      scope="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ -n "$config" && -n "$commit" && -n "$scope" ]] || { usage >&2; exit 2; }
[[ -f "$config" && ! -L "$config" ]] || { echo "Remote config must be a regular local file: $config" >&2; exit 1; }
# shellcheck disable=SC1090
set -a
source "$config"
entrypoints_config="${config%.env}.entrypoints.env"
if [[ -f "$entrypoints_config" && ! -L "$entrypoints_config" ]]; then
  # A legacy three-field remote-deploy.env can keep connection facts separate
  # while this Git-ignored sidecar supplies its approved deployment entrypoint.
  # shellcheck disable=SC1090
  source "$entrypoints_config"
fi
set +a
for required in FLOWWEAVE_REMOTE_HOST FLOWWEAVE_REMOTE_USER FLOWWEAVE_REMOTE_ROOT; do
  [[ -n ${!required:-} ]] || { echo "Missing $required in local remote config" >&2; exit 1; }
done
[[ "$FLOWWEAVE_REMOTE_ROOT" == /* ]] || { echo "FLOWWEAVE_REMOTE_ROOT must be an absolute path" >&2; exit 1; }
for required in FLOWWEAVE_REMOTE_COMPOSE_FILE FLOWWEAVE_REMOTE_ENV_FILE FLOWWEAVE_REMOTE_BUILD_ROOT FLOWWEAVE_REMOTE_IMAGE_ROOT; do
  require_relative_path "$required" "${!required:-}"
done
require_optional_relative_path FLOWWEAVE_REMOTE_STREAM_COMPOSE_FILE "${FLOWWEAVE_REMOTE_STREAM_COMPOSE_FILE:-}"
require_optional_relative_path FLOWWEAVE_REMOTE_STREAM_ENV_FILE "${FLOWWEAVE_REMOTE_STREAM_ENV_FILE:-}"
if [[ -n ${FLOWWEAVE_REMOTE_STREAM_COMPOSE_FILE:-} && -z ${FLOWWEAVE_REMOTE_STREAM_ENV_FILE:-} ]] || \
   [[ -z ${FLOWWEAVE_REMOTE_STREAM_COMPOSE_FILE:-} && -n ${FLOWWEAVE_REMOTE_STREAM_ENV_FILE:-} ]]; then
  echo "FLOWWEAVE_REMOTE_STREAM_COMPOSE_FILE and FLOWWEAVE_REMOTE_STREAM_ENV_FILE must be set together or both empty" >&2
  exit 1
fi
case "$scope" in web|platform|runtime|other) ;; *)
  echo "Invalid --scope '$scope'; expected web, platform, runtime, or other" >&2
  exit 2
  ;; esac

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "Run this from inside the FlowWeave Git checkout." >&2
  exit 1
}
[[ "$PWD" == "$repo_root" || "$PWD" == "$repo_root/"* ]] || {
  echo "Current directory is outside the repository root: $repo_root" >&2
  exit 1
}

for required in AGENTS.md docs/local-build-and-deploy.md; do
  [[ -s "$repo_root/$required" ]] || {
    echo "Required deployment guidance is missing or empty: $required" >&2
    exit 1
  }
done

resolved_commit=$(git rev-parse --verify "${commit}^{commit}" 2>/dev/null) || {
  echo "Commit is not available locally: $commit" >&2
  exit 1
}
git diff --check "${resolved_commit}^!"

if ! git merge-base --is-ancestor "$resolved_commit" HEAD; then
  echo "Commit $resolved_commit is not reachable from current HEAD; switch to its intended checkout first." >&2
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  worktree_state="dirty (safe only because deployment must use git archive of $resolved_commit)"
else
  worktree_state="clean"
fi

cat <<EOF
REMOTE DEPLOYMENT LOCAL PREFLIGHT: READY
target_ssh=${FLOWWEAVE_REMOTE_USER}@${FLOWWEAVE_REMOTE_HOST}
deployment_root=${FLOWWEAVE_REMOTE_ROOT}
compose_file=${FLOWWEAVE_REMOTE_COMPOSE_FILE}
env_file=${FLOWWEAVE_REMOTE_ENV_FILE}
build_root=${FLOWWEAVE_REMOTE_BUILD_ROOT}
image_root=${FLOWWEAVE_REMOTE_IMAGE_ROOT}
stream_compose_file=${FLOWWEAVE_REMOTE_STREAM_COMPOSE_FILE:-<main-compose>}
stream_env_file=${FLOWWEAVE_REMOTE_STREAM_ENV_FILE:-<main-env>}
commit=${resolved_commit}
scope=${scope}
worktree=${worktree_state}
EOF

ssh -o BatchMode=yes -o ConnectTimeout=15 "${FLOWWEAVE_REMOTE_USER}@${FLOWWEAVE_REMOTE_HOST}" \
  env ROOT="$FLOWWEAVE_REMOTE_ROOT" \
  MAIN_COMPOSE_FILE="$FLOWWEAVE_REMOTE_COMPOSE_FILE" \
  MAIN_ENV_FILE="$FLOWWEAVE_REMOTE_ENV_FILE" \
  BUILD_ROOT="$FLOWWEAVE_REMOTE_BUILD_ROOT" \
  IMAGE_ROOT="$FLOWWEAVE_REMOTE_IMAGE_ROOT" \
  STREAM_COMPOSE_FILE="${FLOWWEAVE_REMOTE_STREAM_COMPOSE_FILE:-}" \
  STREAM_ENV_FILE="${FLOWWEAVE_REMOTE_STREAM_ENV_FILE:-}" \
  DEPLOY_SCOPE="$scope" \
  bash -s <<'REMOTE'
set -euo pipefail

fail() { echo "REMOTE DEPLOYMENT PREFLIGHT: FAILED: $*" >&2; exit 1; }
absolute_path() { printf '%s/%s' "$ROOT" "$1"; }

[[ -d "$ROOT" ]] || fail "deployment root is not a directory"
[[ -d "$(absolute_path "$BUILD_ROOT")" ]] || fail "declared build root is not a directory"
[[ -d "$(absolute_path "$IMAGE_ROOT")" ]] || fail "declared image root is not a directory"

check_project() {
  local label="$1" compose_relative="$2" env_relative="$3"
  local compose_path env_path
  compose_path=$(absolute_path "$compose_relative")
  env_path=$(absolute_path "$env_relative")
  [[ -f "$compose_path" && ! -L "$compose_path" ]] || fail "$label Compose file is not a regular file"
  [[ -f "$env_path" && ! -L "$env_path" ]] || fail "$label environment file is not a regular file"
  docker compose --env-file "$env_path" -f "$compose_path" config --quiet
  docker compose --env-file "$env_path" -f "$compose_path" config --services | sort -u
}

main_services=$(check_project main "$MAIN_COMPOSE_FILE" "$MAIN_ENV_FILE")
stream_services="$main_services"
if [[ -n "$STREAM_COMPOSE_FILE" ]]; then
  stream_services=$(check_project stream "$STREAM_COMPOSE_FILE" "$STREAM_ENV_FILE")
fi

require_service() {
  local project="$1" service="$2" services="$3"
  grep -Fxq "$service" <<<"$services" || fail "$project Compose does not declare required service '$service' for scope '$DEPLOY_SCOPE'"
}

case "$DEPLOY_SCOPE" in
  web) require_service main web "$main_services" ;;
  platform)
    for service in migration runtime-provider api worker; do
      require_service main "$service" "$main_services"
    done
    require_service stream stream-api "$stream_services"
    ;;
  runtime) require_service main runtime-provider "$main_services" ;;
  other) ;;
esac

echo "REMOTE DEPLOYMENT PREFLIGHT: READY"
echo "main_services=$(tr '\n' ',' <<<"$main_services" | sed 's/,$//')"
if [[ -n "$STREAM_COMPOSE_FILE" ]]; then
  echo "stream_services=$(tr '\n' ',' <<<"$stream_services" | sed 's/,$//')"
fi
REMOTE

cat <<EOF

Required next steps from AGENTS.md:
1. Build only the images affected by scope '${scope}' for linux/amd64, then inspect each image platform.
2. Package source with 'git archive' from ${resolved_commit}; do not package this working tree. Use the verified build and image roots.
3. Preserve the verified environment and Compose files, named volumes, and persistent workspaces.
4. Never run 'docker compose down -v'. Update only affected services; platform changes run migration first.
5. Validate server health, prefixed API/static requests, the public FlowWeave page, deep Agent route, and FastGPT login.
EOF
