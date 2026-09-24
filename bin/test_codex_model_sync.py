import copy
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch
import urllib.error

from codex_model_sync import SYNC_INTERVAL, build_catalog, sync_delay, sync_once
from configure_codex_sync import configure


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
        with self.assertRaisesRegex(ValueError, "no usable models"):
            sync_once(self.base_url, self.key, self.output)
        self.assertEqual([model["slug"] for model in json.loads(self.output.read_text())["models"]], ["model-c"])

    def test_pending_key_and_empty_models_never_write_catalog(self):
        self.key.write_text("REPLACE_WITH_NEW_API_KEY\n")
        with self.assertRaises(ValueError):
            sync_once(self.base_url, self.key, self.output)
        self.assertEqual(self.server.requests, [])
        self.key.write_text("test-gateway-token")
        self.server.payload = {"data": []}
        with self.assertRaises(ValueError):
            sync_once(self.base_url, self.key, self.output)
        self.assertFalse(self.output.exists())

    def test_plain_http_to_remote_host_is_rejected_before_sending_key(self):
        with self.assertRaisesRegex(ValueError, "invalid gateway base URL"):
            sync_once("http://example.com/v1", self.key, self.output)
        self.assertEqual(self.server.requests, [])

    def test_success_marker_limits_automatic_sync_across_restarts(self):
        now = 1_800_000_000
        self.assertEqual(sync_delay(self.output, SYNC_INTERVAL, now), 0)
        marker = self.output.with_suffix(".last-success")
        marker.parent.mkdir(parents=True)
        self.output.write_text('{"models":[]}')
        marker.touch()
        os.utime(marker, (now - 3600, now - 3600))
        self.assertEqual(sync_delay(self.output, SYNC_INTERVAL, now), 3 * 3600)
        self.assertEqual(sync_delay(self.output, SYNC_INTERVAL, now + 3 * 3600), 0)
        self.output.unlink()
        self.assertEqual(sync_delay(self.output, SYNC_INTERVAL, now), 0)

    def test_deployment_sync_waits_four_hours_after_success(self):
        sync_once(self.base_url, self.key, self.output)
        command = [sys.executable, str(Path(__file__).with_name("codex_model_sync.py")),
                   "--base-url", self.base_url, "--api-key-file", str(self.key),
                   "--output", str(self.output), "--once", "--if-due"]
        recent = subprocess.run(command, text=True, capture_output=True, timeout=10)
        self.assertEqual(recent.returncode, 0, recent.stderr)
        self.assertEqual(len(self.server.requests), 1)
        marker = self.output.with_suffix(".last-success")
        old = marker.stat().st_mtime - SYNC_INTERVAL - 1
        os.utime(marker, (old, old))
        due = subprocess.run(command, text=True, capture_output=True, timeout=10)
        self.assertEqual(due.returncode, 0, due.stderr)
        self.assertEqual(len(self.server.requests), 2)

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

    def test_documented_gpt_efforts_fill_missing_template_levels(self):
        models = ["gpt-5.5", "gpt-5.6", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
                  "gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-reserve"]
        template = {"models": [
            {"slug": "gpt-6-astra", "default_reasoning_level": "none",
             "supported_reasoning_levels": []},
            {"slug": "gpt-5.6", "default_reasoning_level": "none",
             "supported_reasoning_levels": [{"effort": "high", "description": "Custom"}]},
            {"slug": "gpt-reserve", "default_reasoning_level": "high",
             "supported_reasoning_levels": [{"effort": "high", "description": "Custom"}]},
        ]}
        catalog = {model["slug"]: model for model in build_catalog(models, template)["models"]}
        self.assertEqual([level["effort"] for level in catalog["gpt-5.5"]["supported_reasoning_levels"]],
                         ["none", "low", "medium", "high", "xhigh"])
        for name in ("gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
                     "gpt-6-luna", "gpt-6-sol"):
            self.assertEqual([level["effort"] for level in catalog[name]["supported_reasoning_levels"]],
                             ["none", "low", "medium", "high", "xhigh", "max"])
        self.assertEqual([level["effort"] for level in catalog["gpt-6-astra"]["supported_reasoning_levels"]],
                         ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(catalog["gpt-6-astra"]["default_reasoning_level"], "medium")
        self.assertEqual([level["effort"] for level in catalog["gpt-5.6"]["supported_reasoning_levels"]],
                         ["high"])
        self.assertEqual(catalog["gpt-5.6"]["default_reasoning_level"], "high")
        self.assertEqual(catalog["gpt-reserve"]["supported_reasoning_levels"],
                         [{"effort": "high", "description": "Custom"}])

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
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        source = Path(__file__).resolve().parent
        for name in ("sync-codex-models.sh", "restart-codex-app-server.sh",
                     "start-codex-with-new-api-key.sh", "configure_codex_sync.py",
                     "codex_model_sync.py"):
            target = bin_dir / name
            target.write_bytes((source / name).read_bytes())
            if name.endswith(".sh"):
                target.chmod(0o700)
        key = self.root / ".codex-sync/config/api-key"
        key.parent.mkdir(parents=True)
        key.write_text("test-gateway-token\n")
        self.calls = self.root / "calls"
        self.environment = {
            **os.environ,
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "MOCK_CALLS": str(self.calls),
            "MOCK_DAEMON_STATUS": '{"backend":"pid"}',
            "NEW_API_KEY": "test-gateway-token",
            "CODEX_HOME": str(self.root / "codex-home"),
        }
        self.environment.pop("MOCK_COMPOSE_JSON", None)
        self.environment.pop("NEW_API_CODEX_BASE_URL", None)
        commands = {
            "docker": '#!/bin/sh\nprintf "docker %s\\n" "$*" >> "$MOCK_CALLS"\n'
                      'if [ "$*" = "compose config --format json" ]; then '
                      'if [ -n "$MOCK_COMPOSE_JSON" ]; then printf "%s\\n" "$MOCK_COMPOSE_JSON"; '
                      'else printf \'{"services":{"new-api":{"ports":[{"published":"9876","protocol":"tcp"}]}}}\\n\'; fi; fi\n'
                      'case "$*" in compose\\ run*) exit "${MOCK_DOCKER_SYNC_STATUS:-0}" ;; esac\n'
                      'exit "${MOCK_DOCKER_STATUS:-0}"\n',
            "codex": '#!/bin/sh\nprintf "codex %s\\n" "$*" >> "$MOCK_CALLS"\nif [ "$*" = "app-server daemon restart --help" ]; then exit 0; fi\nif [ "$*" = "app-server daemon version" ]; then printf "%s\\n" "$MOCK_DAEMON_STATUS"; exit 0; fi\nexit "${MOCK_CODEX_STATUS:-0}"\n',
        }
        for name, content in commands.items():
            executable = self.root / name
            executable.write_text(content)
            executable.chmod(0o700)
        python = self.root / "python3"
        python.write_text(
            '#!/bin/sh\ncase "$1" in\n'
            '  */codex_model_sync.py) exit "${MOCK_READY_STATUS:-0}" ;;\n'
            f'esac\nexec "{sys.executable}" "$@"\n'
        )
        python.chmod(0o700)

    def run_sync(self, *arguments):
        script = self.root / "bin/sync-codex-models.sh"
        return subprocess.run(
            ["bash", str(script), *arguments], cwd=self.root,
            env=self.environment, text=True, capture_output=True, timeout=10,
        )

    def test_plain_sync_does_not_restart_codex(self):
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], "docker compose config --format json")
        self.assertTrue(calls[1].startswith("docker compose run --rm --no-deps -T codex-model-sync "))
        self.assertIn(" --once ", calls[1])
        self.assertEqual(calls[2], "docker compose --profile codex up -d --no-deps codex-model-sync")
        self.assertIn('base_url = "http://127.0.0.1:9876/v1"',
                      (self.root / "codex-home/config.toml").read_text())
        self.assertFalse(any("up -d --no-deps new-api" in call for call in calls))

    def test_sudo_docker_keeps_current_user_for_catalog_files(self):
        sudo = self.root / "sudo"
        sudo.write_text('#!/bin/sh\nprintf "sudo %s\\n" "$*" >> "$MOCK_CALLS"\nshift 2\nexec "$@"\n')
        sudo.chmod(0o700)
        self.environment["DOCKER_WITH_SUDO"] = "1"
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertTrue(any(f"CODEX_SYNC_UID={os.getuid()}" in call for call in calls))
        self.assertTrue(any(f"CODEX_SYNC_GID={os.getgid()}" in call for call in calls))
        self.assertTrue(any(" docker compose run " in call for call in calls))

    def test_explicit_restart_runs_after_successful_sync(self):
        result = self.run_sync("--restart-codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 6)
        self.assertEqual(calls[0], "codex app-server daemon restart --help")
        self.assertEqual(calls[1], "docker compose config --format json")
        self.assertTrue(calls[2].startswith("docker compose run "))
        self.assertNotIn("--if-due", calls[2])
        self.assertEqual(calls[3], "docker compose --profile codex up -d --no-deps codex-model-sync")
        self.assertEqual(calls[4], "codex app-server daemon version")
        self.assertEqual(calls[5], "codex app-server daemon restart")

    def test_unmanaged_server_is_not_restarted(self):
        self.environment["MOCK_DAEMON_STATUS"] = '{"status":"running","backend":null}'
        result = self.run_sync("--restart-codex")
        self.assertEqual(result.returncode, 3)
        self.assertIn("unmanaged app-server", result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[-1], "codex app-server daemon version")

    def test_home_restart_syncs_before_restart(self):
        sync_script = self.root / "sync"
        sync_script.write_text('#!/bin/sh\nprintf "sync %s\\n" "$*" >> "$MOCK_CALLS"\nexit "${MOCK_SYNC_STATUS:-0}"\n')
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
            "sync ", "codex app-server daemon version", "codex app-server daemon restart",
        ])

    def test_home_restart_stops_before_disrupting_sessions_if_sync_fails(self):
        sync_script = self.root / "sync"
        sync_script.write_text('#!/bin/sh\nprintf "sync %s\\n" "$*" >> "$MOCK_CALLS"\nexit 17\n')
        sync_script.chmod(0o700)
        self.environment["CODEX_MODEL_SYNC_SCRIPT"] = str(sync_script)
        script = Path(__file__).resolve().with_name("restart-codex-app-server.sh")
        result = subprocess.run(
            ["bash", str(script)], cwd=self.root, env=self.environment,
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 17)
        self.assertEqual(self.calls.read_text().splitlines(), ["sync "])

    def test_failed_sync_does_not_restart_codex(self):
        self.environment["MOCK_DOCKER_SYNC_STATUS"] = "17"
        result = self.run_sync("--restart-codex")
        self.assertEqual(result.returncode, 17)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 3)
        self.assertNotIn("codex app-server daemon restart", calls)

    def test_unready_models_leave_codex_and_gateway_running(self):
        self.environment["MOCK_READY_STATUS"] = "1"
        result = self.run_sync("--restart-codex")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / "codex-home/config.toml").exists())
        self.assertEqual(self.calls.read_text().splitlines(), [
            "codex app-server daemon restart --help",
            "docker compose config --format json",
        ])

    def test_explicit_url_supports_unpublished_gateway(self):
        self.environment["MOCK_COMPOSE_JSON"] = json.dumps({
            "services": {"new-api": {"ports": []}}
        })
        self.environment["NEW_API_CODEX_BASE_URL"] = "https://proxy.example/v1"
        result = self.run_sync("--if-due")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('base_url = "https://proxy.example/v1"',
                      (self.root / "codex-home/config.toml").read_text())

    def test_restart_failure_is_reported(self):
        self.environment["MOCK_CODEX_STATUS"] = "18"
        result = self.run_sync("--restart-codex")
        self.assertEqual(result.returncode, 18)

    def test_help_and_invalid_option_do_not_sync_or_restart(self):
        self.assertEqual(self.run_sync("--help").returncode, 0)
        self.assertEqual(self.run_sync("--invalid").returncode, 2)
        self.assertFalse(self.calls.exists())

    def test_linux_codex_launcher_reads_only_the_key_file(self):
        bin_dir = self.root / "bin"
        launcher = bin_dir / "start-codex-with-new-api-key.sh"
        sync_script = bin_dir / "sync-codex-models.sh"
        sync_script.write_text('#!/bin/sh\nprintf "sync %s\\n" "$*" >> "$MOCK_CALLS"\n')
        sync_script.chmod(0o700)
        codex = self.root / "codex"
        codex.write_text('#!/bin/sh\nprintf "%s\\n" "$NEW_API_KEY"\n')
        codex.chmod(0o700)
        result = subprocess.run(["bash", str(launcher)], env=self.environment,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "test-gateway-token")
        self.assertEqual(self.calls.read_text().splitlines(), ["sync "])


class CodexConfigurationTests(unittest.TestCase):
    def test_fresh_install_generates_empty_template_and_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / ".codex-sync/config/api-key"
            key.parent.mkdir(parents=True)
            key.write_text("test-gateway-token\n")
            codex_home = root / "home"
            catalog = configure(root, codex_home, "http://127.0.0.1:9000/v1")
            self.assertEqual(json.loads((root / ".codex-sync/config/template.json").read_text()),
                             {"models": []})
            self.assertEqual(catalog, root / ".codex-sync/catalog/models.json")
            self.assertIn('model_provider = "new_api_sync"',
                          (codex_home / "config.toml").read_text())

    def test_initialization_preserves_metadata_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / ".codex-sync/config/api-key"
            key.parent.mkdir(parents=True)
            key.write_text("test-gateway-token\n")
            codex_home = root / "home"
            codex_home.mkdir()
            previous_catalog = root / "previous.json"
            previous_catalog.write_text('{"models":[{"slug":"known","context_window":12345}]}')
            config = codex_home / "config.toml"
            config.write_text(
                'model_catalog_json = ' + json.dumps(str(previous_catalog)) + '\n'
                'model_provider = "old"\n[features]\nfoo = true\n'
            )
            catalog = configure(root, codex_home, "http://127.0.0.1:9000/v1")
            self.assertEqual(catalog, root / ".codex-sync/catalog/models.json")
            template = root / ".codex-sync/config/template.json"
            self.assertEqual(json.loads(template.read_text())["models"][0]["context_window"], 12345)
            self.assertEqual((codex_home / "config.toml.codex-sync-backup").read_text(),
                             'model_catalog_json = ' + json.dumps(str(previous_catalog)) + '\n'
                             'model_provider = "old"\n[features]\nfoo = true\n')
            first = config.read_text()
            self.assertIn('env_key = "NEW_API_KEY"', first)
            self.assertIn('foo = true', first)
            self.assertNotIn("test-gateway-token", first)
            self.assertEqual(configure(root, codex_home, "http://127.0.0.1:9000/v1"), catalog)
            self.assertEqual(config.read_text(), first)
            self.assertEqual(json.loads(template.read_text())["models"][0]["context_window"], 12345)

    def test_missing_key_leaves_codex_configuration_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "home"
            codex_home.mkdir()
            config = codex_home / "config.toml"
            config.write_text('model = "existing"\n')
            with self.assertRaisesRegex(ValueError, "Create the gateway key"):
                configure(root, codex_home, "http://127.0.0.1:9000/v1")
            self.assertEqual(config.read_text(), 'model = "existing"\n')
            key = root / ".codex-sync/config/api-key"
            key.parent.mkdir(parents=True)
            key.write_text("REPLACE_WITH_NEW_API_KEY\n")
            with self.assertRaisesRegex(ValueError, "Create the gateway key"):
                configure(root, codex_home, "http://127.0.0.1:9000/v1")
            self.assertEqual(config.read_text(), 'model = "existing"\n')


@unittest.skipUnless(os.name == "posix", "Docker deployment uses POSIX shell paths")
class DockerDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        source = Path(__file__).resolve().parent
        for name in ("deploy-codex-sync.sh", "sync-codex-models.sh",
                     "configure_codex_sync.py", "codex_model_sync.py"):
            (bin_dir / name).write_bytes((source / name).read_bytes())
            if name.endswith(".sh"):
                (bin_dir / name).chmod(0o700)
        key = self.root / ".codex-sync/config/api-key"
        key.parent.mkdir(parents=True)
        key.write_text("test-gateway-token\n")
        self.calls = self.root / "calls"
        docker = self.root / "docker"
        docker.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$MOCK_CALLS"\n'
            'case "$*" in\n'
            '  "compose config --format json") if [ -n "$MOCK_COMPOSE_JSON" ]; then printf "%s\\n" "$MOCK_COMPOSE_JSON"; else printf \'{"services":{"new-api":{"image":"new-api:test","ports":[{"published":"9876","target":3000,"protocol":"tcp"}]}}}\\n\'; fi ;;\n'
            '  "compose ps -q new-api") printf "container-id\\n" ;;\n'
            '  inspect*) printf "healthy\\n" ;;\n'
            '  build*) exit "${MOCK_BUILD_STATUS:-0}" ;;\n'
            'esac\n'
        )
        docker.chmod(0o700)
        python = self.root / "python3"
        python.write_text(
            '#!/bin/sh\ncase "$1" in\n'
            '  */codex_model_sync.py) exit "${MOCK_READY_STATUS:-0}" ;;\n'
            f'esac\nexec "{sys.executable}" "$@"\n'
        )
        python.chmod(0o700)
        self.environment = {
            **os.environ,
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "CODEX_HOME": str(self.root / "codex-home"),
            "MOCK_CALLS": str(self.calls),
        }
        self.environment.pop("NEW_API_CODEX_BASE_URL", None)
        self.environment.pop("MOCK_COMPOSE_JSON", None)

    def test_deploy_builds_before_replacing_gateway_without_touching_dependencies(self):
        result = subprocess.run(
            ["bash", str(self.root / "bin/deploy-codex-sync.sh")],
            env=self.environment, text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertFalse((self.root / "codex-home/config.toml").exists())
        self.assertEqual(calls[:5], [
            "compose config --format json",
            "build --pull --build-arg GOPROXY=https://goproxy.cn,direct -t new-api:test .",
            "compose --profile codex stop codex-model-sync",
            "compose up -d --no-deps --force-recreate new-api",
            "compose ps -q new-api",
        ])
        self.assertEqual(calls[5], "inspect -f {{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}} container-id")
        self.assertEqual(len(calls), 6)

    def test_first_deploy_without_key_starts_only_gateway(self):
        (self.root / ".codex-sync/config/api-key").unlink()
        result = subprocess.run(
            ["bash", str(self.root / "bin/deploy-codex-sync.sh")],
            env=self.environment, text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restart Codex", result.stdout)
        self.assertFalse((self.root / "codex-home/config.toml").exists())
        self.assertEqual(len(self.calls.read_text().splitlines()), 6)

    def test_failed_build_keeps_running_gateway_untouched(self):
        self.environment["MOCK_BUILD_STATUS"] = "17"
        result = subprocess.run(
            ["bash", str(self.root / "bin/deploy-codex-sync.sh")],
            env=self.environment, text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 17)
        self.assertEqual(self.calls.read_text().splitlines(), [
            "compose config --format json",
            "build --pull --build-arg GOPROXY=https://goproxy.cn,direct -t new-api:test .",
        ])

    def test_deploy_does_not_require_codex_port_configuration(self):
        self.environment["MOCK_COMPOSE_JSON"] = json.dumps({
            "services": {"new-api": {"image": "new-api:test", "ports": []}}
        })
        result = subprocess.run(
            ["bash", str(self.root / "bin/deploy-codex-sync.sh")],
            env=self.environment, text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "codex-home/config.toml").exists())
        self.assertEqual(len(self.calls.read_text().splitlines()), 6)


@unittest.skipUnless(os.name == "nt", "Windows PowerShell deployment")
class WindowsExeDeploymentTests(unittest.TestCase):
    def test_codex_provider_reads_gateway_key_without_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            source = Path(__file__).resolve().parent
            for name in ("configure-codex-sync.ps1", "read-codex-api-key.ps1"):
                (bin_dir / name).write_bytes((source / name).read_bytes())
            key = root / ".codex-sync/config/api-key"
            key.parent.mkdir(parents=True)
            key.write_text("test-gateway-token\n")
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                '# BEGIN new-api model sync\n'
                'model_provider = "new_api_sync"\n'
                '# END new-api model sync\n'
                'model = "gpt-6-sol"\n\n'
                '# BEGIN new-api model sync provider\n'
                '[model_providers.new_api_sync]\n'
                'name = "New API"\n'
                'base_url = "http://127.0.0.1:9900/v1"\n'
                'env_key = "NEW_API_KEY"\n'
                'wire_api = "responses"\n'
                '# END new-api model sync provider\n'
            )
            environment = {**os.environ, "CODEX_HOME": str(codex_home)}
            environment.pop("NEW_API_KEY", None)
            configured = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(bin_dir / "configure-codex-sync.ps1"), "-Port", "9900"],
                env=environment, text=True, capture_output=True, timeout=20,
            )
            self.assertEqual(configured.returncode, 0, configured.stderr)
            config_text = (codex_home / "config.toml").read_text()
            config = tomllib.loads(config_text)
            provider = config["model_providers"]["new_api_sync"]
            self.assertEqual(config["model_provider"], "new_api_sync")
            self.assertEqual(config["model"], "gpt-6-sol")
            self.assertNotIn("env_key", provider)
            self.assertEqual(provider["auth"]["command"], "powershell.exe")
            self.assertEqual(provider["auth"]["args"][-1], str(bin_dir / "read-codex-api-key.ps1"))
            self.assertNotIn("test-gateway-token", config_text)
            self.assertTrue(Path(config["model_catalog_json"]).is_file())

            reader = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-File", str(bin_dir / "read-codex-api-key.ps1")]
            read = subprocess.run(reader, env=environment, text=True, capture_output=True, timeout=20)
            self.assertEqual(read.returncode, 0, read.stderr)
            self.assertEqual(read.stdout, "test-gateway-token")
            key.write_text("REPLACE_WITH_NEW_API_KEY\n")
            rejected = subprocess.run(reader, env=environment, text=True, capture_output=True, timeout=20)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertNotIn("test-gateway-token", rejected.stdout + rejected.stderr)

    def test_first_deploy_bootstraps_placeholder_without_requiring_a_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            (root / "web").mkdir()
            script = root / "bin/deploy-codex-sync.ps1"
            script.write_bytes(Path(__file__).resolve().with_name(script.name).read_bytes())
            codex_home = root / "codex-home"
            environment = {**os.environ, "CODEX_HOME": str(codex_home)}
            command = (
                "function bun { $global:LASTEXITCODE = 0 }; "
                "function go { $global:LASTEXITCODE = 0 }; "
                f"& '{str(script).replace(chr(39), chr(39) * 2)}' -NoStart -Port 9901"
            )
            args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-Command", command]
            first = subprocess.run(args, env=environment, text=True,
                                   capture_output=True, timeout=20)
            self.assertEqual(first.returncode, 0, first.stderr)
            key = root / ".codex-sync/config/api-key"
            self.assertEqual(key.read_text().strip(), "REPLACE_WITH_NEW_API_KEY")
            self.assertEqual((root / ".codex-sync/config/gateway-port").read_text().strip(), "9901")
            self.assertFalse((root / ".codex-sync/catalog/models.json").exists())
            self.assertFalse((root / ".codex-sync/catalog/models.last-success").exists())
            self.assertFalse((codex_home / "config.toml").exists())

            enabled = root / ".codex-sync/config/auto-sync-enabled"
            enabled.touch()

            key.write_text("test-gateway-token\n")
            changed_port = [*args[:-1], command.replace("-Port 9901", "-Port 9902")]
            second = subprocess.run(changed_port, env=environment, text=True,
                                    capture_output=True, timeout=20)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(key.read_text(), "test-gateway-token\n")
            self.assertEqual((root / ".codex-sync/config/gateway-port").read_text().strip(), "9902")
            self.assertFalse((codex_home / "config.toml").exists())
            self.assertFalse(enabled.exists())
            self.assertFalse((root / ".codex-sync/catalog/models.last-success").exists())

            key.write_text("")
            third = subprocess.run(changed_port, env=environment, text=True,
                                   capture_output=True, timeout=20)
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertEqual(key.read_text(), "")
            launcher = root / "bin/start-codex-with-new-api-key.ps1"
            launcher.write_bytes(Path(__file__).resolve().with_name(launcher.name).read_bytes())
            rejected = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(launcher)],
                env=environment, text=True, capture_output=True, timeout=20,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Invalid gateway key", rejected.stderr)

            key.write_text("test-gateway-token\n")
            start_command = (
                "function Start-Process { param($FilePath, $WorkingDirectory, $WindowStyle, [switch]$PassThru) "
                "if ($WindowStyle -ne 'Normal') { throw 'Gateway terminal is hidden' }; "
                "[pscustomobject]@{HasExited=$false} }; "
                'function Invoke-WebRequest { [pscustomobject]@{StatusCode=200; Content=\'{"success":true,"data":[{"id":"model-a"}]}\'} }; '
                + command.replace("-NoStart ", "")
            )
            started = subprocess.run(
                [*args[:-1], start_command], env=environment, text=True,
                capture_output=True, timeout=20,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertFalse((codex_home / "config.toml").exists())
            self.assertFalse((root / ".codex-sync/catalog/models.json").exists())
            self.assertEqual((root / ".codex-sync/config/gateway-port").read_text().strip(), "9901")
            self.assertFalse((root / ".codex-sync/catalog/models.last-success").exists())


if __name__ == "__main__":
    unittest.main()
