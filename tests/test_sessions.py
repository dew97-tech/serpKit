"""Session manager + runner helpers — temp dir, no network."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.runner import _has_run_timeout, _is_blocked, _session_key  # noqa: E402
from scraper.schemas import ScrapeResult  # noqa: E402
from scraper.sessions import SessionManager, session_ttl_s  # noqa: E402


class TestSessionManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = {k: os.environ.get(k) for k in
                       ("SESSION_DIR", "SESSION_TTL_S")}
        os.environ["SESSION_DIR"] = self.tmp.name
        os.environ["SESSION_TTL_S"] = "1500"

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_roundtrip(self):
        manager = SessionManager()
        session = manager.create("en-us")
        session.state = {"cookies": [{"name": "SID", "value": "x"}]}
        session.exit_ip = "203.0.113.9"
        session.exit_geo = "Dhaka, BD"
        session.user_agent = "Mozilla/5.0 (Windows NT 10.0) Chrome/154.0.0.0"
        manager.save(session)
        loaded = manager.load("en-us")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.sessid, session.sessid)
        self.assertEqual(loaded.state["cookies"][0]["value"], "x")
        self.assertEqual(loaded.exit_ip, "203.0.113.9")
        self.assertEqual(loaded.exit_geo, "Dhaka, BD")
        self.assertEqual(loaded.user_agent, "Mozilla/5.0 (Windows NT 10.0) Chrome/154.0.0.0")

    def test_summary(self):
        manager = SessionManager()
        session = manager.create("en-bd")
        session.runs = 3
        session.successes = 3
        session.state = {"cookies": [{"name": "a", "value": "b"}]}
        manager.save(session)
        summary = manager.summary("en-bd")
        self.assertTrue(summary["active"])
        self.assertEqual(summary["runs"], 3)
        self.assertEqual(summary["cookies"], 1)
        self.assertGreater(summary["expires_in_s"], 0)
        self.assertLessEqual(summary["expires_in_s"], summary["ttl_s"])
        inactive = manager.summary("ja-jp")
        self.assertFalse(inactive["active"])

    def test_expired_returns_none(self):
        manager = SessionManager()
        session = manager.create("en-us")
        session.created_at = time.time() - 10_000
        manager.save(session)
        self.assertIsNone(manager.load("en-us"))

    def test_corrupt_returns_none(self):
        manager = SessionManager()
        (Path(self.tmp.name) / "en-us.meta.json").write_text("not json")
        self.assertIsNone(manager.load("en-us"))

    def test_missing_returns_none(self):
        self.assertIsNone(SessionManager().load("en-us"))

    def test_clear(self):
        manager = SessionManager()
        manager.save(manager.create("en-us"))
        manager.clear("en-us")
        self.assertIsNone(manager.load("en-us"))

    def test_ttl_env(self):
        os.environ["SESSION_TTL_S"] = "120"
        self.assertEqual(session_ttl_s(), 120.0)


class TestRunnerHelpers(unittest.TestCase):
    def test_session_key(self):
        from scraper.config import SearchParams

        params = SearchParams(q="q", hl="en", gl="bd")
        self.assertEqual(_session_key(params), "en-bd")

    def test_is_blocked(self):
        blocked = ScrapeResult(query="q", locale={}, pages_fetched=0,
                               errors=[{"code": "captcha_v2"}])
        self.assertTrue(_is_blocked(blocked))
        ok = ScrapeResult(query="q", locale={}, pages_fetched=1,
                          errors=[], organic=[{"position": 1}])
        self.assertFalse(_is_blocked(ok))
        partial = ScrapeResult(query="q", locale={}, pages_fetched=1,
                               errors=[{"code": "captcha_v2"}],
                               organic=[{"position": 1}])
        self.assertFalse(_is_blocked(partial))
        tarpit = ScrapeResult(query="q", locale={}, pages_fetched=1,
                              errors=[{"code": "zero_result"}])
        self.assertTrue(_is_blocked(tarpit))

    def test_has_run_timeout(self):
        timed_out = ScrapeResult(query="q", locale={},
                                 errors=[{"code": "run_timeout"}])
        self.assertTrue(_has_run_timeout(timed_out))
        other = ScrapeResult(query="q", locale={},
                             errors=[{"code": "captcha_v2"}])
        self.assertFalse(_has_run_timeout(other))


if __name__ == "__main__":
    unittest.main(verbosity=2)
