#!/usr/bin/env bash
set -euo pipefail
set +x

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_root"

codex_home="${CODEX_HOME:-$HOME/.codex}"
mkdir -p -m 700 -- "$project_root/.codex-sync/config"
key_file="$project_root/.codex-sync/config/api-key"
if [[ ! -s "$key_file" ]]; then
  printf 'Create %s with one New API token first.\n' "$key_file" >&2
  exit 1
fi
chmod 600 -- "$key_file"
python3 "$project_root/bin/configure_codex_sync.py" \
  --project-root "$project_root" \
  --codex-home "$codex_home" \
  --base-url "${NEW_API_CODEX_BASE_URL:-http://127.0.0.1:9000/v1}"

export CODEX_SYNC_UID="$(id -u)" CODEX_SYNC_GID="$(id -g)"
if [[ "${DOCKER_WITH_SUDO:-0}" == "1" ]]; then
  sudo CODEX_SYNC_UID="$CODEX_SYNC_UID" CODEX_SYNC_GID="$CODEX_SYNC_GID" \
    docker compose --profile codex up -d new-api codex-model-sync
else
  docker compose --profile codex up -d new-api codex-model-sync
fi
"$project_root/bin/sync-codex-models.sh"
printf 'Model catalog ready. Restart Codex to load the updated menu.\n'
