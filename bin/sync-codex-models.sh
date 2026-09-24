#!/usr/bin/env bash
set -euo pipefail

restart_codex=false
if_due=false
for argument in "$@"; do
  case "$argument" in
    --restart-codex)
      restart_codex=true
      ;;
    --if-due)
      if_due=true
      ;;
    -h|--help)
      printf '%s\n' \
        'Usage: sync-codex-models.sh [--restart-codex] [--if-due]' \
        'Prepare Codex and sync models without restarting the gateway.' \
        '--if-due skips a successful sync less than four hours old.' \
        "--restart-codex syncs models and restarts the current user's Codex app-server daemon." \
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
codex_home="${CODEX_HOME:-$HOME/.codex}"
key_file="$project_root/.codex-sync/config/api-key"
if [[ ! -s "$key_file" ]]; then
  printf 'Create a gateway API key and save it to %s before restarting Codex.\n' "$key_file" >&2
  exit 1
fi
chmod 600 -- "$key_file"
export CODEX_SYNC_UID="${CODEX_SYNC_UID:-$(id -u)}"
export CODEX_SYNC_GID="${CODEX_SYNC_GID:-$(id -g)}"
if [[ "${DOCKER_WITH_SUDO:-0}" == "1" ]]; then
  docker_command=(sudo CODEX_SYNC_UID="$CODEX_SYNC_UID" CODEX_SYNC_GID="$CODEX_SYNC_GID" docker)
else
  docker_command=(docker)
fi

compose_config="$("${docker_command[@]}" compose config --format json)"
gateway_port="$(python3 -c '
import json, os, sys
service = json.load(sys.stdin)["services"]["new-api"]
ports = [str(port.get("published", "")) for port in service.get("ports", [])
         if port.get("protocol", "tcp") == "tcp"]
if not os.environ.get("NEW_API_CODEX_BASE_URL") and (
    len(ports) != 1 or not ports[0].isdigit() or not 1 <= int(ports[0]) <= 65535
):
    raise SystemExit("Compose new-api needs one fixed published TCP port or NEW_API_CODEX_BASE_URL")
print(ports[0] if len(ports) == 1 else "")
' <<< "$compose_config")"
base_url="${NEW_API_CODEX_BASE_URL:-http://127.0.0.1:$gateway_port/v1}"
if ! python3 "$project_root/bin/codex_model_sync.py" \
  --base-url "$base_url" --api-key-file "$key_file" \
  --output "$project_root/.codex-sync/catalog/models.json" --check-ready; then
  printf 'The gateway key or model channels are not ready. Complete setup before restarting Codex.\n' >&2
  exit 1
fi
python3 "$project_root/bin/configure_codex_sync.py" \
  --project-root "$project_root" --codex-home "$codex_home" --base-url "$base_url"

sync_args=(compose run --rm --no-deps -T codex-model-sync
  python -B /app/codex_model_sync.py
  --once
  --base-url http://new-api:3000/v1
  --api-key-file /config/api-key
  --template /config/template.json
  --output /catalog/models.json)
if "$if_due"; then
  sync_args+=(--if-due)
fi
"${docker_command[@]}" "${sync_args[@]}"
"${docker_command[@]}" compose --profile codex up -d --no-deps codex-model-sync

if "$restart_codex"; then
  if [[ -z "${NEW_API_KEY:-}" ]]; then
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
