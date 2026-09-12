#!/usr/bin/env bash
# Plan or execute the narrowly scoped FlowWeave Docker image retention policy
# on a locally configured production host. This script intentionally never prunes
# containers, networks, volumes, workspaces, source archives, or the broad
# Docker image/cache inventory.
set -euo pipefail

readonly CONFIRM_TOKEN='DELETE_UNREFERENCED_FLOWWEAVE_IMAGES'

apply=false
prune_build_cache=false
keep_rollback_images=3
cache_until='168h'
max_delete=500
confirmation=''
config=''

usage() {
  cat <<'EOF'
Usage: scripts/maintain-remote-docker-retention.sh --config <local-env-file> [options]

Default mode is read-only: it inventories the configured FlowWeave production host
and prints image IDs that are safe candidates under the retention policy.

Options:
  --apply                         Delete only the printed, unreferenced image IDs.
  --confirm TOKEN                 Required with --apply; exact token is
                                  DELETE_UNREFERENCED_FLOWWEAVE_IMAGES.
  --keep-rollback-images COUNT   Keep the newest COUNT rollback image IDs for
                                  each of flowweave-platform and flowweave-web
                                  (default: 3). The deployed remote-amd64 image
                                  and every image referenced by any container
                                  are always protected.
  --max-delete COUNT              Refuse an apply plan above COUNT image IDs
                                  (default: 500).
  --prune-build-cache             With --apply and --confirm, run only
                                  'docker builder prune --filter until=…'. It
                                  never prunes images, containers, volumes,
                                  networks, or workspaces.
  --cache-until DURATION          Build cache age filter (default: 168h).
  -h, --help                      Show this help.

The target comes from a private local config file. Do not commit it. Review
dry-run output before any --apply invocation.
EOF
}

require_positive_integer() {
  [[ $2 =~ ^[1-9][0-9]*$ ]] || { echo "$1 must be a positive integer" >&2; exit 2; }
}

while (($#)); do
  case "$1" in
    --config)
      [[ -n ${2:-} ]] || { echo '--config requires a local env file' >&2; exit 2; }
      config=$2
      shift 2
      ;;
    --apply) apply=true; shift ;;
    --confirm) confirmation=${2:-}; shift 2 ;;
    --keep-rollback-images)
      require_positive_integer "--keep-rollback-images" "${2:-}"
      keep_rollback_images=$2
      shift 2
      ;;
    --max-delete)
      require_positive_integer "--max-delete" "${2:-}"
      max_delete=$2
      shift 2
      ;;
    --prune-build-cache) prune_build_cache=true; shift ;;
    --cache-until)
      [[ -n ${2:-} ]] || { echo '--cache-until requires a Docker duration' >&2; exit 2; }
      cache_until=$2
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n $config ]] || { echo '--config is required' >&2; usage >&2; exit 2; }
[[ -f "$config" && ! -L "$config" ]] || { echo "Remote config must be a regular local file: $config" >&2; exit 1; }
# shellcheck disable=SC1090
set -a
source "$config"
set +a
for required in FLOWWEAVE_REMOTE_HOST FLOWWEAVE_REMOTE_USER FLOWWEAVE_REMOTE_ROOT; do
  [[ -n ${!required:-} ]] || { echo "Missing $required in local remote config" >&2; exit 1; }
done
[[ "$FLOWWEAVE_REMOTE_ROOT" == /* ]] || { echo 'FLOWWEAVE_REMOTE_ROOT must be an absolute path' >&2; exit 1; }

if $apply && [[ $confirmation != $CONFIRM_TOKEN ]]; then
  echo "--apply requires --confirm $CONFIRM_TOKEN" >&2
  exit 2
fi
if $prune_build_cache && ! $apply; then
  echo '--prune-build-cache requires --apply and the explicit confirmation token' >&2
  exit 2
fi

ssh "$FLOWWEAVE_REMOTE_USER@$FLOWWEAVE_REMOTE_HOST" \
  env KEEP_ROLLBACK_IMAGES="$keep_rollback_images" \
  MAX_DELETE="$max_delete" \
  APPLY="$apply" \
  PRUNE_BUILD_CACHE="$prune_build_cache" \
  CACHE_UNTIL="$cache_until" \
  ROOT="$FLOWWEAVE_REMOTE_ROOT" \
  DEPLOY_TARGET="$FLOWWEAVE_REMOTE_USER@$FLOWWEAVE_REMOTE_HOST" \
  bash -s <<'REMOTE'
set -euo pipefail

readonly repos=(flowweave-platform flowweave-web)
declare -A protected=()
declare -A known_flowweave_ids=()
declare -A candidate_ids=()
declare -A candidate_tags=()
declare -A seen_rollback_ids=()
container_protected=0
current_protected=0
rollback_protected=0
foreign_tag_protected=0

echo "target=$DEPLOY_TARGET deployment_root=$ROOT"
echo "mode=$([[ $APPLY == true ]] && echo apply || echo dry-run)"
echo "policy=keep-current-plus-${KEEP_ROLLBACK_IMAGES}-newest-rollback-image-ids-per-repository"

# Any image referenced by any container, including exited containers, is never
# considered for deletion. Docker image rm would reject such images anyway,
# but this explicit guard makes the planned set auditable.
while IFS= read -r container_id; do
  [[ -n $container_id ]] || continue
  image_id=$(docker inspect --format '{{.Image}}' "$container_id")
  if [[ -z ${protected[$image_id]:-} ]]; then
    protected[$image_id]=container
    ((container_protected += 1))
  fi
done < <(docker ps -aq)

for repo in "${repos[@]}"; do
  current_id=$(docker image inspect --format '{{.Id}}' "$repo:remote-amd64")
  if [[ -z ${protected[$current_id]:-} ]]; then
    protected[$current_id]=current-tag
    ((current_protected += 1))
  fi

  mapfile -t repo_rows < <(docker image ls --no-trunc --format '{{.Repository}}|{{.Tag}}|{{.ID}}' "$repo" | awk -F'|' -v repo="$repo" '$1 == repo && $2 != "<none>"')
  for row in "${repo_rows[@]}"; do
    IFS='|' read -r _ tag image_id <<<"$row"
    known_flowweave_ids[$image_id]=1
    if [[ $tag == *rollback* ]]; then
      created=$(docker image inspect --format '{{.Created}}' "$image_id")
      printf '%s|%s|%s|%s\n' "$created" "$repo" "$tag" "$image_id"
    fi
  done
done > /tmp/flowweave-rollback-images.$$.txt

# Keep the newest distinct rollback images per repository. Docker's formatted
# Created value is RFC3339 and sorts lexicographically by recency.
for repo in "${repos[@]}"; do
  kept=0
  while IFS='|' read -r _ _ _ image_id; do
    [[ -n ${seen_rollback_ids[$repo:$image_id]:-} ]] && continue
    seen_rollback_ids[$repo:$image_id]=1
    if [[ -z ${protected[$image_id]:-} ]]; then
      protected[$image_id]=rollback-retention
      ((rollback_protected += 1))
    fi
    ((kept += 1))
    ((kept >= KEEP_ROLLBACK_IMAGES)) && break
  done < <(awk -F'|' -v repo="$repo" '$2 == repo' /tmp/flowweave-rollback-images.$$.txt | sort -r)
done
rm -f /tmp/flowweave-rollback-images.$$.txt

for image_id in "${!known_flowweave_ids[@]}"; do
  [[ -n ${protected[$image_id]:-} ]] && continue

  # Only delete historical rollback image IDs whose complete tag set belongs
  # to the two governed FlowWeave repositories. This retains named build and
  # candidate images for a separate, explicitly reviewed policy and protects
  # an image shared with another host workload.
  mapfile -t tags < <(docker image inspect --format '{{range .RepoTags}}{{println .}}{{end}}' "$image_id")
  ((${#tags[@]})) || continue
  foreign_tag=false
  non_rollback_tag=false
  governed_tag_count=0
  for tag in "${tags[@]}"; do
    [[ -z $tag ]] && continue
    if [[ $tag == flowweave-platform:* || $tag == flowweave-web:* ]]; then
      ((governed_tag_count += 1))
      [[ $tag == *rollback* ]] || non_rollback_tag=true
      continue
    fi
    foreign_tag=true
    break
  done
  if [[ $foreign_tag == true || $non_rollback_tag == true || $governed_tag_count == 0 ]]; then
    ((foreign_tag_protected += 1))
    continue
  fi

  candidate_ids[$image_id]=1
  candidate_tags[$image_id]=$(printf '%s,' "${tags[@]}" | sed 's/,$//')
done

echo "protected_image_ids=${#protected[@]}"
echo "container_protected_image_ids=$container_protected"
echo "current_protected_image_ids=$current_protected"
echo "rollback_protected_image_ids=$rollback_protected"
echo "foreign_tag_protected_image_ids=$foreign_tag_protected"
echo "candidate_image_ids=${#candidate_ids[@]}"
for image_id in "${!candidate_ids[@]}"; do
  created=$(docker image inspect --format '{{.Created}}' "$image_id")
  size=$(docker image inspect --format '{{.Size}}' "$image_id")
  printf 'CANDIDATE image=%s created=%s bytes=%s tags=%s\n' "$image_id" "$created" "$size" "${candidate_tags[$image_id]}"
done | sort

if [[ $APPLY == true ]]; then
  ((${#candidate_ids[@]} <= MAX_DELETE)) || {
    echo "refusing to delete ${#candidate_ids[@]} images above --max-delete=$MAX_DELETE" >&2
    exit 1
  }
  while IFS= read -r image_id; do
    # A container might have been created after the dry-run. Recheck its image
    # reference immediately before changing any tag; never use --force.
    if docker ps -aq | xargs -r docker inspect --format '{{.Image}}' | grep -Fxq "$image_id"; then
      echo "refusing to untag image now referenced by a container: $image_id" >&2
      exit 1
    fi

    # Docker refuses `image rm <id>` when one image carries several tags. The
    # policy already proved every non-empty tag is a governed rollback tag, so
    # remove those exact tags one at a time. The final untag deletes the image
    # naturally; no force removal is ever used.
    mapfile -t current_tags < <(docker image inspect --format '{{range .RepoTags}}{{println .}}{{end}}' "$image_id")
    for tag in "${current_tags[@]}"; do
      [[ -z $tag ]] && continue
      if [[ $tag != flowweave-platform:* && $tag != flowweave-web:* ]] || [[ $tag != *rollback* ]]; then
        echo "refusing to untag image with a changed non-rollback or external tag: $image_id ($tag)" >&2
        exit 1
      fi
      docker image rm "$tag"
      printf 'REMOVED tag=%s image=%s\n' "$tag" "$image_id"
    done
  done < <(printf '%s\n' "${!candidate_ids[@]}" | sort)
  if [[ $PRUNE_BUILD_CACHE == true ]]; then
    docker builder prune --force --filter "until=$CACHE_UNTIL"
  fi
fi

echo 'post_operation_docker_system_df'
docker system df
REMOTE
