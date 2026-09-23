#!/usr/bin/env bash
set -euo pipefail
set +x

dry_run=false
case "${1:-}" in
  "") ;;
  --dry-run) dry_run=true ;;
  -h|--help)
    printf 'Usage: %s [--dry-run]\n' "$0"
    printf '%s\n' "Restart the current user's SSH Codex app-server and proxy."
    exit 0
    ;;
  *)
    printf 'Unknown option: %s\n' "$1" >&2
    exit 2
    ;;
esac
if (( $# > 1 )); then
  printf 'Only one option is supported.\n' >&2
  exit 2
fi

command -v codex >/dev/null
command -v python3 >/dev/null

codex_home_path="${CODEX_HOME:-${HOME}/.codex}"
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
sync_script="${CODEX_MODEL_SYNC_SCRIPT:-$project_root/bin/sync-codex-models.sh}"
if ! "$dry_run"; then
  if [[ ! -x "$sync_script" ]]; then
    printf 'Model sync script is missing or not executable: %s\n' "$sync_script" >&2
    exit 1
  fi
  "$sync_script"
fi

catalog_path="$(python3 - "$codex_home_path/config.toml" <<'PY'
import pathlib
import sys
import tomllib

with open(sys.argv[1], "rb") as configuration_file:
    configuration = tomllib.load(configuration_file)
catalog = pathlib.Path(configuration["model_catalog_json"])
if not catalog.is_file():
    raise SystemExit("Codex model catalog is missing: " + str(catalog))
print(catalog)
PY
)"

if ! "$dry_run" && [[ -z "${NEW_API_KEY:-}" ]]; then
  gateway_key_file="$project_root/.codex-sync/config/api-key"
  if [[ ! -r "$gateway_key_file" ]]; then
    printf 'NEW_API_KEY is unset and the gateway key file is unavailable.\n' >&2
    exit 1
  fi
  NEW_API_KEY="$(<"$gateway_key_file")"
  if [[ -z "$NEW_API_KEY" || "$NEW_API_KEY" == *$'\n'* || "$NEW_API_KEY" == *$'\r'* ]]; then
    printf 'The gateway key file is empty or invalid.\n' >&2
    exit 1
  fi
  export NEW_API_KEY
fi

printf 'Codex home: %s\nModel catalog: %s\n' "$codex_home_path" "$catalog_path"

if ! "$dry_run"; then
  daemon_status="$(codex app-server daemon version 2>/dev/null || true)"
  if python3 -c 'import json, sys; sys.exit(json.load(sys.stdin).get("backend") != "pid")' <<< "$daemon_status" 2>/dev/null; then
    exec codex app-server daemon restart
  fi
fi

python3 - "$codex_home_path" "$dry_run" <<'PY'
import os
from pathlib import Path
import signal
import sys
import time

codex_home = str(Path(sys.argv[1]).resolve())
dry_run = sys.argv[2] == "true"
current_uid = os.getuid()


def codex_processes():
    matches = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != current_uid:
                continue
            arguments = [argument.decode(errors="replace") for argument in process.joinpath("cmdline").read_bytes().split(b"\0") if argument]
            if not arguments or not any("codex" in Path(argument).name for argument in arguments[:2]):
                continue
            if "app-server" not in arguments:
                continue
            index = arguments.index("app-server")
            role = arguments[index + 1:]
            if role[:1] == ["proxy"]:
                kind = "proxy"
            elif role[:2] == ["--listen", "unix://"]:
                kind = "server"
            else:
                continue
            environment = dict(item.split(b"=", 1) for item in process.joinpath("environ").read_bytes().split(b"\0") if b"=" in item)
            process_home = environment.get(b"CODEX_HOME")
            if process_home is None:
                process_home = environment.get(b"HOME", b"") + b"/.codex"
            if str(Path(os.fsdecode(process_home)).resolve()) != codex_home:
                continue
            fields = process.joinpath("stat").read_text().split()
            matches.append((int(process.name), fields[21], kind))
        except (OSError, ValueError, IndexError):
            continue
    return matches


targets = codex_processes()
for process_id, _, kind in targets:
    print(f"{kind}: PID {process_id}")
if dry_run:
    raise SystemExit(0)

for kind in ("proxy", "server"):
    for process_id, started_at, candidate_kind in targets:
        if candidate_kind != kind:
            continue
        process = Path("/proc") / str(process_id)
        try:
            if process.joinpath("stat").read_text().split()[21] == started_at:
                os.kill(process_id, signal.SIGTERM)
        except (OSError, IndexError):
            continue

deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    if not codex_processes():
        break
    time.sleep(0.2)
else:
    raise SystemExit("Codex proxy or app-server is still running; close the SSH Codex client and try again")

print("Previous Codex SSH processes stopped")
PY

if "$dry_run"; then
  exit 0
fi

log_path="$codex_home_path/app-server-control/manual-restart.log"
umask 077
mkdir -p -- "$(dirname -- "$log_path")"
nohup codex -c features.code_mode_host=true app-server --listen unix:// \
  </dev/null >>"$log_path" 2>&1 &

for ((attempt = 0; attempt < 30; attempt++)); do
  daemon_status="$(codex app-server daemon version 2>/dev/null || true)"
  if python3 -c 'import json, sys; sys.exit(json.load(sys.stdin).get("status") != "running")' <<< "$daemon_status" 2>/dev/null; then
    printf 'Codex app-server restarted. Reconnect the SSH Codex client.\n'
    exit 0
  fi
  sleep 0.5
done

printf 'Codex app-server did not become ready. Check %s\n' "$log_path" >&2
exit 1
