import argparse
import copy
import json
import logging
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


DOCUMENTED_REASONING_EFFORTS = {
    "gpt-5.5": ("none", "low", "medium", "high", "xhigh"),
    "gpt-5.6": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-5.6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-5.6-sol": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-5.6-terra": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-6-astra": ("low", "medium", "high", "xhigh", "max"),
    "gpt-6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-6-sol": ("none", "low", "medium", "high", "xhigh", "max"),
}
REASONING_EFFORT_DESCRIPTIONS = {
    "none": "No reasoning",
    "low": "Faster responses with lighter reasoning",
    "medium": "Balanced reasoning for everyday tasks",
    "high": "Deeper reasoning for complex tasks",
    "xhigh": "Extended reasoning for difficult tasks",
    "max": "Maximum reasoning for the hardest tasks",
}
SYNC_INTERVAL = 4 * 60 * 60
KEY_PLACEHOLDER = "REPLACE_WITH_NEW_API_KEY"


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
        or (parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1", "localhost", "new-api"))
    ):
        raise ValueError("invalid gateway base URL")
    key_bytes = Path(api_key_file).read_bytes()
    if len(key_bytes) > 4096:
        raise ValueError("gateway API key file is too large")
    api_key = key_bytes.decode("utf-8-sig").strip()
    if not api_key or api_key == KEY_PLACEHOLDER or "\n" in api_key or "\r" in api_key:
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
    if not names:
        raise ValueError("no usable models are available for this key")
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
            if name == "step-5-preview":
                profile["default_reasoning_level"] = "medium"
                profile["supported_reasoning_levels"] = [
                    {"effort": "low", "description": "Faster responses with lighter reasoning"},
                    {"effort": "medium", "description": "Balanced reasoning for everyday tasks"},
                    {"effort": "high", "description": "Deeper reasoning for complex tasks"},
                ]
        documented_efforts = DOCUMENTED_REASONING_EFFORTS.get(name)
        if documented_efforts:
            if not profile.get("supported_reasoning_levels"):
                profile["supported_reasoning_levels"] = [
                    {"effort": effort, "description": REASONING_EFFORT_DESCRIPTIONS[effort]}
                    for effort in documented_efforts
                ]
            available = [level["effort"] for level in profile["supported_reasoning_levels"]]
            if profile.get("default_reasoning_level") not in available:
                profile["default_reasoning_level"] = "medium" if "medium" in available else available[0]
        profile.update(visibility="list", supported_in_api=True, priority=priority)
        models.append(profile)
    return {"models": models}


def sync_once(base_url, api_key_file, output, template_file=None):
    names = fetch_models(base_url, api_key_file)
    template = {"models": []}
    if template_file is not None:
        try:
            template = json.loads(Path(template_file).read_text())
        except FileNotFoundError:
            pass
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


def sync_delay(output, interval, now):
    if not Path(output).is_file():
        return 0
    try:
        last_success = Path(output).with_suffix(".last-success").stat().st_mtime
    except FileNotFoundError:
        return 0
    return max(0, min(interval, interval - (now - last_success)))


def main():
    parser = argparse.ArgumentParser(description="Sync New API models to a Codex model catalog")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--template", help="Existing Codex catalog supplying per-model metadata")
    parser.add_argument("--interval", type=int, default=SYNC_INTERVAL)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--if-due", action="store_true", help="With --once, skip a successful sync less than four hours old")
    parser.add_argument("--check-ready", action="store_true", help="Validate the key and available models without writing a catalog")
    args = parser.parse_args()
    if args.interval < SYNC_INTERVAL:
        parser.error("--interval must be at least four hours")
    if args.once and args.check_ready:
        parser.error("--once and --check-ready cannot be combined")
    if args.if_due and not args.once:
        parser.error("--if-due requires --once")
    if args.check_ready:
        try:
            fetch_models(args.base_url, args.api_key_file)
            return 0
        except (OSError, ValueError, urllib.error.URLError):
            return 1
    if args.if_due and sync_delay(args.output, args.interval, time.time()) > 0:
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Model sync started; interval=%d seconds", args.interval)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    while not stopped.is_set():
        if not args.once:
            remaining = sync_delay(args.output, args.interval, time.time())
            if remaining > 0:
                stopped.wait(remaining)
                if stopped.is_set():
                    break
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
            if isinstance(error, (FileNotFoundError, ValueError, urllib.error.HTTPError)):
                logging.info("Model sync inactive until the service is restarted")
                stopped.wait()
                return 0
        if args.once:
            return 0
        stopped.wait(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
