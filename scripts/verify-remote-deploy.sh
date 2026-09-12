#!/usr/bin/env bash
# Read-only preflight for a locally configured FlowWeave remote deployment target.
# It deliberately does not open an SSH connection or invoke Docker.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/verify-remote-deploy.sh --config <local-env-file> --commit <SHA> --scope <web|platform|runtime|other>

This command is read-only. It validates that an immutable, local Git commit is
available for the configured remote target; it does not contact the server.
The config file is private and must not be committed. Start from
deploy/remote-deploy.env.example.
EOF
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
set +a
for required in FLOWWEAVE_REMOTE_HOST FLOWWEAVE_REMOTE_USER FLOWWEAVE_REMOTE_ROOT; do
  [[ -n ${!required:-} ]] || { echo "Missing $required in local remote config" >&2; exit 1; }
done
[[ "$FLOWWEAVE_REMOTE_ROOT" == /* ]] || { echo "FLOWWEAVE_REMOTE_ROOT must be an absolute path" >&2; exit 1; }
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

# The deployment Compose file is deliberately server-only. Its existence and
# validity are checked on the configured host before any service is recreated;
# requiring it here would incorrectly reject every clean local checkout.

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
REMOTE DEPLOYMENT PREFLIGHT: READY
target_ssh=${FLOWWEAVE_REMOTE_USER}@${FLOWWEAVE_REMOTE_HOST}
deployment_root=${FLOWWEAVE_REMOTE_ROOT}
commit=${resolved_commit}
scope=${scope}
worktree=${worktree_state}

Required next steps from AGENTS.md:
1. Build only the images affected by scope '${scope}' for linux/amd64, then inspect each image platform.
2. Package source with 'git archive' from ${resolved_commit}; do not package this working tree.
3. Preserve the configured environment file, deployment Compose file, named volumes, and persistent workspaces.
4. Never run 'docker compose down -v'. Update only affected services; platform changes run migration first.
5. Validate server health, prefixed API/static requests, the public FlowWeave page, deep Agent route, and FastGPT login.
EOF
