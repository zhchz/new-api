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
export CODEX_SYNC_UID="$(id -u)" CODEX_SYNC_GID="$(id -g)"
if [[ "${DOCKER_WITH_SUDO:-0}" == "1" ]]; then
  docker_command=(sudo CODEX_SYNC_UID="$CODEX_SYNC_UID" CODEX_SYNC_GID="$CODEX_SYNC_GID" docker)
else
  docker_command=(docker)
fi

compose_config="$("${docker_command[@]}" compose config --format json)"
metadata="$(python3 -c '
import json, os, sys
service = json.load(sys.stdin)["services"]["new-api"]
image = service.get("image", "")
ports = [str(port.get("published", "")) for port in service.get("ports", [])
         if port.get("protocol", "tcp") == "tcp"]
if not image:
    raise SystemExit("Compose new-api image is empty")
if not os.environ.get("NEW_API_CODEX_BASE_URL") and (
    len(ports) != 1 or not ports[0].isdigit() or not 1 <= int(ports[0]) <= 65535
):
    raise SystemExit("Compose new-api needs one fixed published TCP port or NEW_API_CODEX_BASE_URL")
print(image + "\t" + (ports[0] if len(ports) == 1 else ""))
' <<< "$compose_config")"
IFS=$'\t' read -r image_ref gateway_port <<< "$metadata"
python3 "$project_root/bin/configure_codex_sync.py" \
  --project-root "$project_root" \
  --codex-home "$codex_home" \
  --base-url "${NEW_API_CODEX_BASE_URL:-http://127.0.0.1:$gateway_port/v1}"

"${docker_command[@]}" build --pull --build-arg "GOPROXY=${GOPROXY:-https://goproxy.cn,direct}" -t "$image_ref" .
"${docker_command[@]}" compose up -d --no-deps --force-recreate new-api

container_id="$("${docker_command[@]}" compose ps -q new-api)"
if [[ -z "$container_id" ]]; then
  printf 'The new-api container did not start.\n' >&2
  exit 1
fi
healthy=false
for attempt in {1..60}; do
  health="$("${docker_command[@]}" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
  if [[ "$health" == "healthy" ]]; then
    healthy=true
    break
  fi
  sleep 3
done
if ! "$healthy"; then
  printf 'The rebuilt new-api container is not healthy. Check docker compose logs new-api.\n' >&2
  exit 1
fi

"${docker_command[@]}" compose --profile codex up -d --no-deps --force-recreate codex-model-sync
"$project_root/bin/sync-codex-models.sh"
printf 'Model catalog ready. Restart Codex to load the updated menu.\n'
