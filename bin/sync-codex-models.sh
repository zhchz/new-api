#!/usr/bin/env bash
set -euo pipefail

restart_codex=false
for argument in "$@"; do
  case "$argument" in
    --restart-codex)
      restart_codex=true
      ;;
    -h|--help)
      printf '%s\n' \
        'Usage: sync-codex-models.sh [--restart-codex]' \
        'Sync the model catalog immediately; unchanged catalogs are not rewritten.' \
        "--restart-codex restarts the current user's Codex app-server daemon after a successful sync." \
        'Restarting interrupts active daemon tasks. Reconnect the client afterwards.' \
        'Standalone Codex CLI and IDE processes must be closed and reopened separately.'
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$argument" >&2
      exit 2
      ;;
  esac
done

if "$restart_codex"; then
  if ! command -v codex >/dev/null 2>&1; then
    printf 'codex is not installed or is not in PATH.\n' >&2
    exit 1
  fi
  codex app-server daemon restart --help >/dev/null
fi

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_root"
export CODEX_SYNC_UID="${CODEX_SYNC_UID:-$(id -u)}"
export CODEX_SYNC_GID="${CODEX_SYNC_GID:-$(id -g)}"

sync_args=(compose run --rm --no-deps -T codex-model-sync
  python -B /app/codex_model_sync.py
  --once
  --base-url http://new-api:3000/v1
  --api-key-file /config/api-key
  --template /config/template.json
  --output /catalog/models.json)
if [[ "${DOCKER_WITH_SUDO:-0}" == "1" ]]; then
  sudo CODEX_SYNC_UID="$CODEX_SYNC_UID" CODEX_SYNC_GID="$CODEX_SYNC_GID" docker "${sync_args[@]}"
else
  docker "${sync_args[@]}"
fi

if "$restart_codex"; then
  if [[ -z "${NEW_API_KEY:-}" ]]; then
    key_file="$project_root/.codex-sync/config/api-key"
    if [[ ! -r "$key_file" ]]; then
      printf 'Gateway key file is unavailable: %s\n' "$key_file" >&2
      exit 1
    fi
    NEW_API_KEY="$(<"$key_file")"
  fi
  if [[ -z "$NEW_API_KEY" || "$NEW_API_KEY" == *$'\n'* || "$NEW_API_KEY" == *$'\r'* ]]; then
    printf 'Gateway key file is empty or invalid.\n' >&2
    exit 1
  fi
  export NEW_API_KEY
  daemon_status="$(codex app-server daemon version)" || exit $?
  if ! python3 -c 'import json, sys; sys.exit(json.load(sys.stdin).get("backend") != "pid")' <<< "$daemon_status"; then
    printf '%s\n' \
      'Codex is running an unmanaged app-server. The CLI daemon cannot restart it.' \
      'Close the SSH Codex client, then stop its app-server from a separate SSH terminal.' \
      'Reconnect the client to start a fresh server and load the synced model catalog.' >&2
    exit 3
  fi
  printf 'Sync completed. Restarting the Codex daemon; active tasks will be interrupted.\n'
  exec codex app-server daemon restart
fi
