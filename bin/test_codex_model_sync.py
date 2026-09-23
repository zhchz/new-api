import copy
import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

from codex_model_sync import build_catalog, sync_once


class ModelServer(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Authorization")))
        self.send_response(self.server.response_status)
        if self.server.response_status == 302:
            self.send_header("Location", "/unexpected-redirect")
        self.end_headers()
        self.wfile.write(json.dumps(self.server.payload).encode())

    def log_message(self, *_):
        pass


class ModelSyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.key = self.root / "api-key"
        self.key.write_text("test-gateway-token")
        self.output = self.root / "catalog" / "models.json"
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ModelServer)
        self.server.requests = []
        self.server.response_status = 200
        self.server.payload = {"data": [{"id": "model-a"}]}
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def test_sync_additions_removals_duplicates_and_empty_list(self):
        self.server.payload = {"data": [{"id": "model-b"}, {"id": "model-a"}, {"id": "model-b"}]}
        self.assertEqual(sync_once(self.base_url, self.key, self.output), (2, True))
        catalog = json.loads(self.output.read_text())
        self.assertEqual([model["slug"] for model in catalog["models"]], ["model-a", "model-b"])
        self.assertEqual(self.server.requests, [("/v1/models", "Bearer test-gateway-token")])
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("test-gateway-token", self.output.read_text())
        self.assertTrue(self.output.with_suffix(".last-success").exists())
        modified = self.output.stat().st_mtime_ns
        self.assertEqual(sync_once(self.base_url, self.key, self.output), (2, False))
        self.assertEqual(self.output.stat().st_mtime_ns, modified)
        self.server.payload = {"data": [{"id": "model-c"}]}
        sync_once(self.base_url, self.key, self.output)
        self.assertEqual([model["slug"] for model in json.loads(self.output.read_text())["models"]], ["model-c"])
        self.server.payload = {"data": []}
        sync_once(self.base_url, self.key, self.output)
        self.assertEqual(json.loads(self.output.read_text()), {"models": []})

    def test_only_advertised_responses_or_unspecified_endpoints(self):
        self.server.payload = {"data": [
            {"id": "responses", "supported_endpoint_types": ["openai-response"]},
            {"id": "image", "supported_endpoint_types": ["image-generation"]},
            {"id": "legacy"},
            {"id": "unspecified", "supported_endpoint_types": []},
        ]}
        sync_once(self.base_url, self.key, self.output)
        self.assertEqual([model["slug"] for model in json.loads(self.output.read_text())["models"]], ["legacy", "responses", "unspecified"])

    def test_preserves_exact_model_metadata_without_copying_to_unknown_models(self):
        template = {"models": [{
            "slug": "known", "context_window": 123456,
            "supported_reasoning_levels": [{"effort": "high", "description": "High"}],
            "visibility": "hide", "supported_in_api": False,
            "base_instructions": "Existing instructions", "custom": {"value": 1},
        }]}
        original = copy.deepcopy(template)
        catalog = build_catalog(["unknown", "known"], template)
        known, unknown = catalog["models"]
        self.assertEqual(known["context_window"], 123456)
        self.assertEqual(known["base_instructions"], "Existing instructions")
        self.assertEqual(known["visibility"], "list")
        self.assertTrue(known["supported_in_api"])
        self.assertNotIn("context_window", unknown)
        self.assertEqual(unknown["supported_reasoning_levels"], [])
        known["custom"]["value"] = 2
        self.assertEqual(template, original)

    def test_step_5_preview_exposes_supported_reasoning_levels(self):
        catalog = build_catalog(["step-5-preview", "unknown"], {"models": []})
        step, unknown = catalog["models"]
        self.assertEqual(step["default_reasoning_level"], "medium")
        self.assertEqual(
            [level["effort"] for level in step["supported_reasoning_levels"]],
            ["low", "medium", "high"],
        )
        self.assertEqual(unknown["supported_reasoning_levels"], [])

    def test_failure_preserves_catalog_and_success_timestamp(self):
        sync_once(self.base_url, self.key, self.output)
        original = self.output.read_bytes()
        modified = self.output.with_suffix(".last-success").stat().st_mtime_ns
        cases = [
            (401, {"error": "unauthorized"}),
            (500, {"error": "unavailable"}),
            (200, {"success": False, "data": []}),
            (200, {}),
            (200, {"data": [{"id": "good"}, {"id": None}]}),
            (200, {"data": [{"id": ""}]}),
            (200, {"data": [{"id": "good", "supported_endpoint_types": "openai-response"}]}),
        ]
        for status, payload in cases:
            with self.subTest(status=status, payload=payload):
                self.server.response_status = status
                self.server.payload = payload
                with self.assertRaises((ValueError, urllib.error.HTTPError)):
                    sync_once(self.base_url, self.key, self.output)
                self.assertEqual(self.output.read_bytes(), original)
                self.assertEqual(self.output.with_suffix(".last-success").stat().st_mtime_ns, modified)

    def test_redirect_does_not_forward_api_key(self):
        self.server.response_status = 302
        with self.assertRaises(urllib.error.HTTPError):
            sync_once(self.base_url, self.key, self.output)
        self.assertEqual(len(self.server.requests), 1)
        self.assertFalse(self.output.exists())

    def test_key_rotation_is_used_on_next_sync(self):
        sync_once(self.base_url, self.key, self.output)
        self.key.write_text("rotated-test-token")
        sync_once(self.base_url, self.key, self.output)
        self.assertEqual(self.server.requests[-1][1], "Bearer rotated-test-token")

    def test_failed_atomic_replace_preserves_previous_catalog(self):
        sync_once(self.base_url, self.key, self.output)
        original = self.output.read_bytes()
        self.server.payload = {"data": [{"id": "replacement"}]}
        with patch("codex_model_sync.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                sync_once(self.base_url, self.key, self.output)
        self.assertEqual(self.output.read_bytes(), original)
        self.assertEqual(list(self.output.parent.glob(".models-*")), [])


class ManualSyncCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.calls = self.root / "calls"
        self.environment = {
            **os.environ,
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "MOCK_CALLS": str(self.calls),
            "MOCK_DAEMON_STATUS": '{"backend":"pid"}',
        }
        commands = {
            "docker": '#!/bin/sh\nprintf "docker %s\\n" "$*" >> "$MOCK_CALLS"\nexit "${MOCK_DOCKER_STATUS:-0}"\n',
            "codex": '#!/bin/sh\nprintf "codex %s\\n" "$*" >> "$MOCK_CALLS"\nif [ "$*" = "app-server daemon restart --help" ]; then exit 0; fi\nif [ "$*" = "app-server daemon version" ]; then printf "%s\\n" "$MOCK_DAEMON_STATUS"; exit 0; fi\nexit "${MOCK_CODEX_STATUS:-0}"\n',
        }
        for name, content in commands.items():
            executable = self.root / name
            executable.write_text(content)
            executable.chmod(0o700)

    def run_sync(self, *arguments):
        script = Path(__file__).resolve().with_name("sync-codex-models.sh")
        return subprocess.run(
            ["bash", str(script), *arguments], cwd=self.root,
            env=self.environment, text=True, capture_output=True, timeout=10,
        )

    def test_plain_sync_does_not_restart_codex(self):
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("docker compose run --rm --no-deps -T codex-model-sync "))
        self.assertIn(" --once ", calls[0])

    def test_explicit_restart_runs_after_successful_sync(self):
        result = self.run_sync("--restart-codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0], "codex app-server daemon restart --help")
        self.assertTrue(calls[1].startswith("docker compose run "))
        self.assertEqual(calls[2], "codex app-server daemon version")
        self.assertEqual(calls[3], "codex app-server daemon restart")

    def test_unmanaged_server_is_not_restarted(self):
        self.environment["MOCK_DAEMON_STATUS"] = '{"status":"running","backend":null}'
        result = self.run_sync("--restart-codex")
        self.assertEqual(result.returncode, 3)
        self.assertIn("unmanaged app-server", result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1], "codex app-server daemon version")

    def test_home_restart_syncs_before_restart(self):
        sync_script = self.root / "sync"
        sync_script.write_text('#!/bin/sh\nprintf "sync\\n" >> "$MOCK_CALLS"\nexit "${MOCK_SYNC_STATUS:-0}"\n')
        sync_script.chmod(0o700)
        self.environment["CODEX_MODEL_SYNC_SCRIPT"] = str(sync_script)
        self.environment["NEW_API_KEY"] = "test-gateway-token"
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        catalog = self.root / "catalog.json"
        catalog.write_text('{"models": []}')
        (codex_home / "config.toml").write_text('model_catalog_json = ' + json.dumps(str(catalog)))
        self.environment["CODEX_HOME"] = str(codex_home)
        script = Path(__file__).resolve().with_name("restart-codex-app-server.sh")
        result = subprocess.run(
            ["bash", str(script)], cwd=self.root, env=self.environment,
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), [
            "sync", "codex app-server daemon version", "codex app-server daemon restart",
        ])

    def test_home_restart_stops_before_disrupting_sessions_if_sync_fails(self):
        sync_script = self.root / "sync"
        sync_script.write_text('#!/bin/sh\nprintf "sync\\n" >> "$MOCK_CALLS"\nexit 17\n')
        sync_script.chmod(0o700)
        self.environment["CODEX_MODEL_SYNC_SCRIPT"] = str(sync_script)
        script = Path(__file__).resolve().with_name("restart-codex-app-server.sh")
        result = subprocess.run(
            ["bash", str(script)], cwd=self.root, env=self.environment,
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 17)
        self.assertEqual(self.calls.read_text().splitlines(), ["sync"])

    def test_failed_sync_does_not_restart_codex(self):
        self.environment["MOCK_DOCKER_STATUS"] = "17"
        result = self.run_sync("--restart-codex")
        self.assertEqual(result.returncode, 17)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("codex app-server daemon restart", calls)

    def test_restart_failure_is_reported(self):
        self.environment["MOCK_CODEX_STATUS"] = "18"
        result = self.run_sync("--restart-codex")
        self.assertEqual(result.returncode, 18)

    def test_help_and_invalid_option_do_not_sync_or_restart(self):
        self.assertEqual(self.run_sync("--help").returncode, 0)
        self.assertEqual(self.run_sync("--invalid").returncode, 2)
        self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
