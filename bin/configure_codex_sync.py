#!/usr/bin/env python3
"""Initialize Codex model sync files and its user configuration."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import tomllib

START = "# BEGIN new-api model sync\n"
END = "# END new-api model sync\n"
PROVIDER_START = "# BEGIN new-api model sync provider\n"
PROVIDER_END = "# END new-api model sync provider\n"
PROVIDER = "new_api_sync"


def configure(project_root, codex_home, base_url):
    key = project_root / ".codex-sync/config/api-key"
    if not key.is_file():
        raise ValueError(f"Create the gateway key file first: {key}")
    key_text = key.read_text().lstrip("\ufeff").strip()
    if not key_text or key_text == "REPLACE_WITH_NEW_API_KEY":
        raise ValueError(f"Create the gateway key file first: {key}")
    if "\n" in key_text or "\r" in key_text:
        raise ValueError("Gateway key must be one line")
    config_path = codex_home / "config.toml"
    original = config_path.read_text() if config_path.exists() else ""
    parsed = tomllib.loads(original)
    if PROVIDER in parsed.get("model_providers", {}) and START not in original:
        raise ValueError(f"Existing model provider {PROVIDER} is not managed by this script")
    if (original.count(START) != original.count(END) or original.count(START) > 1
            or original.count(PROVIDER_START) != original.count(PROVIDER_END)
            or original.count(PROVIDER_START) > 1):
        raise ValueError("Invalid managed block in Codex config")
    catalog = (project_root / ".codex-sync/catalog/models.json").resolve()
    template = project_root / ".codex-sync/config/template.json"
    template.parent.mkdir(parents=True, exist_ok=True)
    catalog.parent.mkdir(parents=True, exist_ok=True)
    if not template.exists():
        source = parsed.get("model_catalog_json")
        if source and Path(source).is_file():
            data = json.loads(Path(source).read_text())
            if not isinstance(data, dict) or not isinstance(data.get("models"), list):
                raise ValueError("Existing model catalog cannot be used as a template")
        else:
            data = {"models": []}
        with template.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    else:
        data = json.loads(template.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise ValueError("Invalid existing model template")

    for start, end in ((START, END), (PROVIDER_START, PROVIDER_END)):
        if start in original:
            before, rest = original.split(start, 1)
            _, after = rest.split(end, 1)
            original = before + after
    lines = []
    top_level = True
    for line in original.splitlines(keepends=True):
        if re.match(r"\s*\[", line):
            top_level = False
        if top_level and re.match(r"\s*(model_provider|model_catalog_json)\s*=", line):
            continue
        lines.append(line)
    remainder = "".join(lines).lstrip("\n")
    # Top-level settings must precede all tables; the provider table goes last.
    top = START + f'model_provider = "{PROVIDER}"\n' + "model_catalog_json = " + json.dumps(str(catalog), ensure_ascii=False) + "\n" + END + "\n"
    provider = (
        "\n" + PROVIDER_START + f"[model_providers.{PROVIDER}]\n"
        + 'name = "New API"\n'
        + "base_url = " + json.dumps(base_url.rstrip("/"), ensure_ascii=False) + "\n"
        + 'env_key = "NEW_API_KEY"\n'
        + 'wire_api = "responses"\n'
        + PROVIDER_END
    )
    updated = top + remainder.rstrip() + "\n" + provider
    parsed_updated = tomllib.loads(updated)
    if parsed_updated["model_catalog_json"] != str(catalog):
        raise ValueError("Failed to configure model catalog")
    if updated == (config_path.read_text() if config_path.exists() else ""):
        return catalog
    codex_home.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        backup = config_path.with_name("config.toml.codex-sync-backup")
        if not backup.exists():
            shutil.copy2(config_path, backup)
    descriptor, temporary = tempfile.mkstemp(prefix=".config-", dir=codex_home)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_path)
        os.chmod(config_path, 0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return catalog


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    print(configure(args.project_root.resolve(), args.codex_home.expanduser().resolve(), args.base_url))
