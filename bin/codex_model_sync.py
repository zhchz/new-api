import argparse
import copy
import json
import logging
import os
from pathlib import Path
import signal
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def fetch_models(base_url, api_key_file):
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid gateway base URL")
    api_key = Path(api_key_file).read_text().strip()
    if not api_key or "\n" in api_key or "\r" in api_key:
        raise ValueError("missing or invalid gateway API key")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=15) as response:
        payload = response.read(8 * 1024 * 1024 + 1)
    if len(payload) > 8 * 1024 * 1024:
        raise ValueError("model response exceeds size limit")
    data = json.loads(payload)
    if (
        not isinstance(data, dict)
        or data.get("success") is False
        or not isinstance(data.get("data"), list)
    ):
        raise ValueError("invalid model list response")
    names = set()
    for model in data["data"]:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            raise ValueError("invalid model entry")
        name = model["id"].strip()
        if not name or any(ord(character) < 32 for character in name):
            raise ValueError("invalid model identifier")
        endpoints = model.get("supported_endpoint_types")
        if endpoints is not None:
            if not isinstance(endpoints, list) or any(
                not isinstance(endpoint, str) for endpoint in endpoints
            ):
                raise ValueError("invalid model endpoint list")
            if endpoints and "openai-response" not in endpoints:
                continue
        names.add(name)
    return sorted(names)


def build_catalog(names, template):
    if not isinstance(template, dict) or not isinstance(template.get("models"), list):
        raise ValueError("invalid template catalog")
    profiles = {}
    for profile in template["models"]:
        if not isinstance(profile, dict) or not isinstance(profile.get("slug"), str):
            raise ValueError("invalid template model")
        profiles[profile["slug"]] = profile
    models = []
    for priority, name in enumerate(sorted(set(names))):
        profile = copy.deepcopy(profiles.get(name))
        if profile is None:
            profile = {
                "slug": name,
                "display_name": name,
                "description": "Model available through New API",
                "default_reasoning_level": None,
                "supported_reasoning_levels": [],
                "shell_type": "unified_exec",
                "base_instructions": "You are a coding assistant. Follow the user's instructions and the project's conventions.",
                "supports_reasoning_summaries": False,
                "support_verbosity": False,
                "supports_parallel_tool_calls": False,
                "input_modalities": ["text"],
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "experimental_supported_tools": [],
            }
        profile.update(visibility="list", supported_in_api=True, priority=priority)
        models.append(profile)
    return {"models": models}


def sync_once(base_url, api_key_file, output, template_file=None):
    names = fetch_models(base_url, api_key_file)
    template = {"models": []}
    if template_file is not None:
        template = json.loads(Path(template_file).read_text())
    catalog = build_catalog(names, template)
    encoded = (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    changed = not output.exists() or output.read_bytes() != encoded
    if changed:
        descriptor, temporary = tempfile.mkstemp(prefix=".models-", dir=output.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, output)
        finally:
            Path(temporary).unlink(missing_ok=True)
    output.with_suffix(".last-success").touch(mode=0o600)
    return len(names), changed


def main():
    parser = argparse.ArgumentParser(description="Sync New API models to a Codex model catalog")
    parser.add_argument("--base-url", default="http://127.0.0.1:9000/v1")
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--template", help="Existing Codex catalog supplying per-model metadata")
    parser.add_argument("--interval", type=int, default=3600)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Model sync started; interval=%d seconds", args.interval)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    while not stopped.is_set():
        try:
            count, changed = sync_once(
                args.base_url, args.api_key_file, args.output, args.template
            )
            if changed or args.once:
                logging.info("Synced %d models; restart Codex to load the catalog", count)
        except (OSError, ValueError, urllib.error.URLError) as error:
            detail = f"HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else type(error).__name__
            logging.error("Model sync failed (%s); previous catalog retained", detail)
            if args.once:
                return 1
        if args.once:
            return 0
        stopped.wait(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
