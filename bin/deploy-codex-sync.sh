#!/usr/bin/env bash
set -euo pipefail
set +x

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_root"

mkdir -p -m 700 -- "$project_root/.codex-sync/config"
key_file="$project_root/.codex-sync/config/api-key"
export CODEX_SYNC_UID="$(id -u)" CODEX_SYNC_GID="$(id -g)"
if [[ "${DOCKER_WITH_SUDO:-0}" == "1" ]]; then
  docker_command=(sudo CODEX_SYNC_UID="$CODEX_SYNC_UID" CODEX_SYNC_GID="$CODEX_SYNC_GID" docker)
else
  docker_command=(docker)
fi

compose_config="$("${docker_command[@]}" compose config --format json)"
image_ref="$(python3 -c '
import json, sys
service = json.load(sys.stdin)["services"]["new-api"]
image = service.get("image", "")
if not image:
    raise SystemExit("Compose new-api image is empty")
print(image)
' <<< "$compose_config")"

"${docker_command[@]}" build --pull --build-arg "GOPROXY=${GOPROXY:-https://goproxy.cn,direct}" -t "$image_ref" .
"${docker_command[@]}" compose --profile codex stop codex-model-sync
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

printf 'Gateway is ready. Create an API key and usable model channels, write the key to %s, then restart Codex with bin/restart-codex-app-server.sh or bin/start-codex-with-new-api-key.sh.\n' "$key_file"
