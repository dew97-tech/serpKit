"""API endpoint tests: streamed mock runs and health shape (no network)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402


class TestStreamEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)

    def test_mock_stream_returns_result_event(self):
        with self.client.stream("GET", "/api/scrape/stream",
                                params={"q": "test query", "mock": 1}) as resp:
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/event-stream",
                          resp.headers.get("content-type", ""))
            body = "".join(resp.iter_text())
        self.assertTrue(body.startswith("data: "))
        payload = json.loads(body[len("data: "):].split("\n", 1)[0])
        self.assertEqual(payload["type"], "result")
        self.assertTrue(payload["result"]["mock"])
        self.assertEqual(payload["result"]["query"], "test query")

    def test_health_includes_sessions_block(self):
        data = self.client.get("/api/health").json()
        self.assertTrue(data["ok"])
        self.assertIn("sessions", data)
        self.assertIn("presets", data)
        self.assertIn("solver", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
