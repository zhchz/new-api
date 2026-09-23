#!/usr/bin/env bash
set -euo pipefail
set +x
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
key_file="$project_root/.codex-sync/config/api-key"
if [[ ! -r "$key_file" ]]; then
  printf 'Gateway key file is unavailable: %s\n' "$key_file" >&2
  exit 1
fi
NEW_API_KEY="$(<"$key_file")"
if [[ -z "$NEW_API_KEY" || "$NEW_API_KEY" == *$'\n'* || "$NEW_API_KEY" == *$'\r'* ]]; then
  printf 'Gateway key file is empty or invalid.\n' >&2
  exit 1
fi
export NEW_API_KEY
exec codex "$@"
