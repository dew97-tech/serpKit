"""Unit tests: CapSolver client, solve policy, egress config/guard.

Stdlib unittest + mocked HTTP — no network calls, no real keys.
Run:  python -m unittest discover tests -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import capsolver  # noqa: E402
from scraper.capsolver import CapSolverClient, CapSolverError  # noqa: E402
from scraper.config import (  # noqa: E402
    ProxyConfig,
    SearchParams,
    load_proxy_from_env,
    validate_proxy_for_google,
)
from scraper.serp import SolvePolicy, task_type_for  # noqa: E402


class ScriptedClient(CapSolverClient):
    """CapSolverClient with scripted _post responses."""

    def __init__(self, responses: list[dict]):
        super().__init__(api_key="test-key", poll_interval_s=0.0, timeout_s=5.0)
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def _post(self, path: str, payload: dict) -> dict:
        self.calls.append((path, payload))
        data = self.responses.pop(0)
        capsolver.raise_for_error(data)
        return data


class TestCapSolverClient(unittest.TestCase):
    def test_solve_task_polls_until_ready(self):
        client = ScriptedClient([
            {"errorId": 0, "taskId": "t1"},                      # createTask
            {"errorId": 0, "status": "processing"},               # poll 1
            {"errorId": 0, "status": "ready",
             "solution": {"gRecaptchaResponse": "TOKEN"}},        # poll 2
        ])
        solution = client.solve_task({"type": "X"})
        self.assertEqual(solution["gRecaptchaResponse"], "TOKEN")
        self.assertEqual(solution["_taskId"], "t1")
        self.assertEqual([p for p, _ in client.calls],
                         ["/createTask", "/getTaskResult", "/getTaskResult"])

    def test_solve_task_sync_image(self):
        client = ScriptedClient([
            {"errorId": 0, "status": "ready", "solution": {"text": "ab12c"}},
        ])
        self.assertEqual(client.solve_image(b"png-bytes"), "ab12c")

    def test_error_raises_with_code(self):
        client = ScriptedClient([
            {"errorId": 1, "errorCode": "ERROR_ZERO_BALANCE",
             "errorDescription": "no funds"},
        ])
        with self.assertRaises(CapSolverError) as ctx:
            client.solve_task({"type": "X"})
        self.assertTrue(ctx.exception.fatal)
        self.assertEqual(ctx.exception.code, "ERROR_ZERO_BALANCE")

    def test_error_semantics(self):
        self.assertTrue(CapSolverError("ERROR_ZERO_BALANCE").fatal)
        self.assertTrue(CapSolverError("ERROR_IP_BANNED").circuit)
        self.assertTrue(CapSolverError("ERROR_CAPTCHA_UNSOLVABLE").retryable)
        self.assertFalse(CapSolverError("ERROR_TASK_TIMEOUT").retryable)

    def test_enterprise_task_payload(self):
        client = ScriptedClient([
            {"errorId": 0, "taskId": "t2"},
            {"errorId": 0, "status": "ready",
             "solution": {"gRecaptchaResponse": "T"}},
        ])
        client.solve_recaptcha_v2(
            sitekey="6Ld-xxx", page_url="https://www.google.com/sorry/index",
            data_s="S-VALUE", enterprise=True,
        )
        _, payload = client.calls[0]
        task = payload["task"]
        self.assertEqual(task["type"], "ReCaptchaV2EnterpriseTaskProxyLess")
        self.assertEqual(task["enterprisePayload"], {"s": "S-VALUE"})
        self.assertNotIn("recaptchaDataSValue", task)

    def test_non_enterprise_uses_data_s_value(self):
        client = ScriptedClient([
            {"errorId": 0, "taskId": "t3"},
            {"errorId": 0, "status": "ready",
             "solution": {"gRecaptchaResponse": "T"}},
        ])
        client.solve_recaptcha_v2(sitekey="k", page_url="u", data_s="S")
        task = client.calls[0][1]["task"]
        self.assertEqual(task["type"], "ReCaptchaV2TaskProxyLess")
        self.assertEqual(task["recaptchaDataSValue"], "S")

    def test_proxy_bound_task_type(self):
        client = ScriptedClient([
            {"errorId": 0, "taskId": "t4"},
            {"errorId": 0, "status": "ready",
             "solution": {"gRecaptchaResponse": "T"}},
        ])
        client.solve_recaptcha_v2(sitekey="k", page_url="u", enterprise=True,
                                  proxy="http:host:1:user:pass")
        task = client.calls[0][1]["task"]
        self.assertEqual(task["type"], "ReCaptchaV2EnterpriseTask")
        self.assertEqual(task["proxy"], "http:host:1:user:pass")

    def test_session_context_fields(self):
        client = ScriptedClient([
            {"errorId": 0, "taskId": "t5"},
            {"errorId": 0, "status": "ready",
             "solution": {"gRecaptchaResponse": "T"}},
        ])
        client.solve_recaptcha_v2(sitekey="k", page_url="u",
                                  user_agent="UA/1")
        task = client.calls[0][1]["task"]
        self.assertEqual(task["userAgent"], "UA/1")
        self.assertNotIn("cookies", task)


class TestTaskTypeSelection(unittest.TestCase):
    def test_matrix(self):
        block_e = {"enterprise": True}
        block_n = {"enterprise": False}
        self.assertEqual(task_type_for(block_e, ""),
                         "ReCaptchaV2EnterpriseTaskProxyLess")
        self.assertEqual(task_type_for(block_e, "http:p:1:u:p"),
                         "ReCaptchaV2EnterpriseTask")
        self.assertEqual(task_type_for(block_n, ""), "ReCaptchaV2TaskProxyLess")
        self.assertEqual(task_type_for(block_n, "http:p:1:u:p"),
                         "ReCaptchaV2Task")


class TestSolvePolicy(unittest.TestCase):
    def test_not_configured_never_solves(self):
        policy = SolvePolicy(client=None)
        can, why = policy.can_solve({"kind": "captcha_v2"})
        self.assertFalse(can)
        self.assertIn("CAPSOLVER_API_KEY", why)

    def test_budget_and_throttle(self):
        policy = SolvePolicy(client=ScriptedClient([]), max_solves=1,
                             min_interval_s=0)
        can, _ = policy.can_solve({"kind": "captcha_v2"})
        self.assertTrue(can)
        policy._record({"kind": "captcha_v2"}, outcome="solved", error="", ms=1)
        can, why = policy.can_solve({"kind": "captcha_v2"})
        self.assertFalse(can)
        self.assertEqual(why, "solve_budget_exhausted")

    def test_unsolvable_kind(self):
        policy = SolvePolicy(client=ScriptedClient([]))
        can, why = policy.can_solve({"kind": "unusual_traffic"})
        self.assertFalse(can)
        self.assertIn("unsolvable_kind", why)

    def test_circuit_opens_on_fatal(self):
        policy = SolvePolicy(client=ScriptedClient([]))
        policy.fatal = True
        can, why = policy.can_solve({"kind": "captcha_v2"})
        self.assertFalse(can)
        self.assertEqual(why, "solver_fatal")

    def test_public_log_strips_internal(self):
        policy = SolvePolicy(client=ScriptedClient([]))
        policy._record({"kind": "captcha_v2"}, outcome="solved", error="", ms=5,
                       task_id="t", task_type="ReCaptchaV2EnterpriseTaskProxyLess")
        entry = policy.public_log()[0]
        self.assertNotIn("_ts", entry)
        self.assertEqual(entry["outcome"], "solved")


class TestProxyConfig(unittest.TestCase):
    def test_username_templates(self):
        cfg = ProxyConfig(server="host:1", username="base", password="pw",
                          session_template="-session-{run_id}",
                          targeting_template="-country-{country}")
        user = cfg.build_username("abc123", "us")
        self.assertEqual(user, "base-session-abc123-country-us")
        self.assertEqual(cfg.capsolver_proxy_string("abc123", "us"),
                         "http:host:1:base-session-abc123-country-us:pw")

    def test_brightdata_guard(self):
        bd = ProxyConfig(server="brd.superproxy.io:44445", username="u")
        allowed, reason = validate_proxy_for_google(bd)
        self.assertFalse(allowed)
        self.assertIn("SERP API", reason)
        os.environ["ALLOW_BD_GOOGLE"] = "1"
        try:
            allowed, _ = validate_proxy_for_google(bd)
            self.assertTrue(allowed)
        finally:
            del os.environ["ALLOW_BD_GOOGLE"]

    def test_generic_proxy_allowed(self):
        allowed, _ = validate_proxy_for_google(
            ProxyConfig(server="gw.example.com:823", username="u"))
        self.assertTrue(allowed)

    def test_env_mapping(self):
        env = {
            "BRIGHTDATA_PROXY_HOST": "brd.superproxy.io",
            "BRIGHTDATA_PROXY_PORT": "44445",
            "BRIGHTDATA_PROXY_USER": "brd-customer-x-zone-y",
            "BRIGHTDATA_PROXY_PASSWORD": "pw",
        }
        shadowed = ["PROXY_SERVER", "PROXY_USERNAME", "PROXY_PASSWORD",
                    "PROXY_SESSION_TEMPLATE", "PROXY_TARGETING_TEMPLATE",
                    "PROXY_USERNAME_TEMPLATE"]
        saved = {k: os.environ.pop(k, None) for k in shadowed}
        saved.update({k: os.environ.get(k) for k in env})
        os.environ.update(env)
        try:
            cfg = load_proxy_from_env()
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg.server, "brd.superproxy.io:44445")
            self.assertTrue(cfg.is_brightdata())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main(verbosity=2)
