"""CapSolver HTTP client for CAPTCHA solving.

Verified against the CapSolver API: createTask plus getTaskResult polling,
reCAPTCHA v2 and Enterprise task types, and synchronous ImageToTextTask.
Google /sorry/ serves reCAPTCHA v2 Enterprise with an `s` parameter, which is
sent as enterprisePayload (docs/CAPSOLVER.md). Error codes are classified as
fatal, circuit, or retryable. Solver policy and browser injection live in
serp.py; this module only talks to the API.
"""
from __future__ import annotations

import base64
import time

import httpx

API_BASE = "https://api.capsolver.com"

# Error codes with handling semantics.
FATAL_CODES = {  # stop solving for the whole process
    "ERROR_KEY_DENIED_ACCESS",
    "ERROR_ZERO_BALANCE",
    "ERROR_TASK_NOT_SUPPORTED",
    "ERROR_INVALID_TASK_DATA",
    "ERROR_BAD_REQUEST",
}
CIRCUIT_CODES = {  # pause solving for a while (5 min), keep scraping
    "ERROR_IP_BANNED",
    "ERROR_KEY_TEMP_BLOCKED",
}
RETRYABLE_CODES = {  # one retry is allowed by policy
    "ERROR_CAPTCHA_UNSOLVABLE",
    "ERROR_SERVICE_UNAVALIABLE",
    "ERROR_RATE_LIMIT",
}
CIRCUIT_COOLDOWN_S = 300.0


class CapSolverError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message

    @property
    def fatal(self) -> bool:
        return self.code in FATAL_CODES

    @property
    def circuit(self) -> bool:
        return self.code in CIRCUIT_CODES

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


def raise_for_error(data: dict) -> None:
    """Normalize CapSolver error responses into CapSolverError."""
    if data.get("errorId"):
        raise CapSolverError(
            data.get("errorCode") or "ERROR", data.get("errorDescription") or ""
        )


class CapSolverClient:
    """Minimal, testable CapSolver API client."""

    def __init__(
        self,
        api_key: str,
        timeout_s: float = 130.0,
        poll_interval_s: float = 2.0,
        http_client: httpx.Client | None = None,
        proxy_url: str | None = None,
    ):
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        if http_client is not None:
            self._http = http_client
        else:
            # Short connect timeout: blocked networks retry each resolved IP,
            # so a large connect budget multiplies into multi-minute hangs.
            tmo = httpx.Timeout(connect=4.0, read=30.0, write=30.0, pool=10.0)
            if proxy_url:
                # Proxy MITMs TLS (for example Bright Data), verification disabled.
                self._http = httpx.Client(timeout=tmo, proxy=proxy_url,
                                          verify=False)
            else:
                self._http = httpx.Client(timeout=tmo)
        self._balance_cache: tuple[float, float] | None = None  # (value, ts)

    # -- low level -----------------------------------------------------------

    def _post(self, path: str, payload: dict) -> dict:
        resp = self._http.post(f"{API_BASE}{path}", json=payload)
        resp.raise_for_status()
        data = resp.json()
        raise_for_error(data)
        return data

    # -- API surface ---------------------------------------------------------

    def get_balance(self, cache_s: float = 60.0) -> float | None:
        now = time.time()
        if self._balance_cache and now - self._balance_cache[1] < cache_s:
            return self._balance_cache[0]
        # Errors propagate (CapSolverError or transport) so callers can
        # surface the real reason (e.g. proxy 407 ip_forbidden).
        data = self._post("/getBalance", {"clientKey": self.api_key})
        balance = float(data.get("balance"))
        self._balance_cache = (balance, now)
        return balance

    def solve_task(self, task: dict, timeout_s: float | None = None) -> dict:
        """createTask + poll getTaskResult. Returns the `solution` dict."""
        timeout_s = timeout_s or self.timeout_s
        created = self._post("/createTask", {"clientKey": self.api_key, "task": task})
        if created.get("status") == "ready":  # sync tasks (ImageToText)
            return created.get("solution", {})
        task_id = created.get("taskId")
        if not task_id:
            raise CapSolverError("ERROR_BAD_REQUEST", "no taskId returned")

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(self.poll_interval_s)
            try:
                result = self._post(
                    "/getTaskResult", {"clientKey": self.api_key, "taskId": task_id}
                )
            except CapSolverError as e:
                if e.retryable:
                    continue  # service hiccup while polling — keep polling
                raise
            status = result.get("status")
            if status == "ready":
                solution = result.get("solution", {})
                solution["_taskId"] = task_id
                return solution
            if status == "failed":
                raise CapSolverError(
                    result.get("errorCode") or "ERROR_CAPTCHA_UNSOLVABLE",
                    result.get("errorDescription") or "task failed",
                )
        raise CapSolverError("ERROR_TASK_TIMEOUT", f"{timeout_s}s exceeded")

    # -- task helpers ----------------------------------------------------------

    def solve_recaptcha_v2(
        self,
        sitekey: str,
        page_url: str,
        data_s: str = "",
        enterprise: bool = False,
        proxy: str = "",
        user_agent: str = "",
    ) -> dict:
        """Returns solution dict (see `gRecaptchaResponse`).

        `proxy` = "http:host:port:user:pass" — when provided, the solve is
        proxy-bound (`ReCaptchaV2Task`/`EnterpriseTask`) so the token shares
        the browser's exit IP. Otherwise ProxyLess types are used.
        `user_agent` mirrors the browser into the solve. Note: the API
        rejects a `cookies` field with ERROR_INVALID_TASK_DATA on these
        task types (verified 2026-10), so only the UA is forwarded.
        """
        if enterprise:
            task_type = "ReCaptchaV2EnterpriseTask" if proxy \
                else "ReCaptchaV2EnterpriseTaskProxyLess"
        else:
            task_type = "ReCaptchaV2Task" if proxy else "ReCaptchaV2TaskProxyLess"
        task: dict = {"type": task_type, "websiteURL": page_url,
                      "websiteKey": sitekey}
        if data_s:
            if enterprise:
                task["enterprisePayload"] = {"s": data_s}
            else:
                task["recaptchaDataSValue"] = data_s
        if proxy:
            task["proxy"] = proxy
        if user_agent:
            task["userAgent"] = user_agent
        return self.solve_task(task)

    def solve_image(self, image_bytes: bytes) -> str:
        """ImageToTextTask — synchronous. Returns the recognized text."""
        body = base64.b64encode(image_bytes).decode("ascii")
        solution = self.solve_task({"type": "ImageToTextTask", "body": body})
        text = solution.get("text", "").strip()
        if not text:
            raise CapSolverError("ERROR_CAPTCHA_UNSOLVABLE", "empty OCR text")
        return text
