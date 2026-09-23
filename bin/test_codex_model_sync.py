import copy
import http.server
import json
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
