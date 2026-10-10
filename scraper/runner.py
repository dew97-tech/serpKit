"""Run orchestrator producing the frozen contract.

Order: autocomplete over plain HTTP, search pages 1 to 3 in one browser
context with the CapSolver loop when configured, then a best-effort Maps
pass. Egress is provider-agnostic; Bright Data zones are blocked for Google
by vendor policy and skipped with a loud error unless ALLOW_BD_GOOGLE=1 is
set for evidence runs. mock_result() serves the frozen samples with no
network.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .autocomplete import fetch_autocomplete
from .capsolver import CapSolverClient
from .config import (
    SOLVE_MAX_PER_RUN,
    SOLVE_MIN_INTERVAL_S,
    SOLVE_TIMEOUT_S,
    ProxyConfig,
    SearchParams,
    load_capsolver_proxy,
    load_proxy_from_env,
    new_run_id,
    reload_env_if_changed,
    run_timeout_s,
    validate_proxy_config,
    validate_proxy_for_google,
)
from .maps import collect_maps
from .schemas import ScrapeResult
from .serp import SolvePolicy, collect_search, emit_progress
from .sessions import SessionManager, session_reuse_enabled

_SAMPLES_DIR = Path(__file__).parent.parent / "samples"


def _session_key(params: SearchParams) -> str:
    return f"{params.hl}-{params.gl}"


def _run_retry_enabled() -> bool:
    return os.environ.get("RUN_RETRY", "1").lower() not in ("0", "false", "no", "")


def _warmup_enabled() -> bool:
    return os.environ.get("SESSION_WARMUP", "1").lower() not in ("0", "false", "no", "")


def _ip_check_enabled() -> bool:
    return os.environ.get("SESSION_IP_CHECK", "1").lower() not in ("0", "false", "no", "")


def _midrun_ip_check_enabled() -> bool:
    return os.environ.get("SESSION_MIDRUN_IP_CHECK", "1").lower() not in ("0", "false", "no", "")


def _session_ip_check(
    proxy_cfg: ProxyConfig, sessid: str, country: str
) -> dict | None:
    """Exit-IP pin check through the session's own proxy.

    Residential peers can die mid-window and DataImpulse silently swaps in a
    new IP. Cookies would then belong to a different IP. Returns
    {"ip", "geo"} (never raises; None when the check is unavailable).
    """
    try:
        proxy_url = proxy_cfg.httpx_proxy_url(sessid, country)
        timeout = httpx.Timeout(connect=4.0, read=8.0, write=8.0, pool=4.0)
        with httpx.Client(proxy=proxy_url, timeout=timeout) as client:
            data = client.get("https://ipinfo.io/json").json()
        ip = str(data.get("ip", ""))
        if not ip:
            return None
        geo = ", ".join(p for p in (data.get("city"), data.get("country")) if p)
        return {"ip": ip, "geo": geo}
    except Exception:
        return None


def _session_ip_check_light(
    proxy_cfg: ProxyConfig, sessid: str, country: str
) -> str | None:
    """Cheap exit-IP check between pages (ipify, a few bytes)."""
    try:
        proxy_url = proxy_cfg.httpx_proxy_url(sessid, country)
        timeout = httpx.Timeout(connect=4.0, read=6.0, write=6.0, pool=4.0)
        with httpx.Client(proxy=proxy_url, timeout=timeout) as client:
            resp = client.get("https://api.ipify.org")
        ip = resp.text.strip()
        return ip or None
    except Exception:
        return None


# -- DataImpulse proxy-error taxonomy (docs/Errors) --------------------------
# THREADS_EXHAUSTED = too many concurrent connections -> backoff, same sessid.
# TRAFFIC_EXHAUSTED / USER_RATE_LIMIT_EXCEEDED = quota gone -> fail fast.
# NO_RAY / NO_HOST_CONNECTION / PORT_* = targeting/port problem -> surface.

_PROXY_THREAD_MARKS = ("threads_exhausted", "thread exhausted", "407")
_PROXY_TRAFFIC_MARKS = ("traffic_exhausted", "traffic exhausted",
                        "user_rate_limit_exceeded", "requests_exhausted")
_PROXY_TARGET_MARKS = ("no_ray", "no_host_connection", "port_blocked",
                       "port_not_allowed", "no-user", "no_user",
                       "user_blocked")


def _error_blob(result: ScrapeResult) -> str:
    parts = []
    for err in result.errors:
        parts.append(str(err.get("code", "")))
        parts.append(str(err.get("message", "")))
        detail = err.get("detail")
        if detail:
            parts.append(str(detail))
    return " ".join(parts).lower()


def _has_proxy_threads_exhausted(result: ScrapeResult) -> bool:
    blob = _error_blob(result)
    return any(mark in blob for mark in _PROXY_THREAD_MARKS)


def _has_proxy_traffic_exhausted(result: ScrapeResult) -> bool:
    blob = _error_blob(result)
    return any(mark in blob for mark in _PROXY_TRAFFIC_MARKS)


def _has_proxy_targeting_error(result: ScrapeResult) -> bool:
    blob = _error_blob(result)
    return any(mark in blob for mark in _PROXY_TARGET_MARKS)


def _proxy_thread_backoff_s() -> float:
    try:
        return max(1.0, float(os.environ.get("PROXY_THREAD_BACKOFF_S", "8")))
    except ValueError:
        return 8.0


def _ip_cache_ttl_s() -> float:
    try:
        return max(0.0, float(os.environ.get("SESSION_IP_CACHE_S", "60")))
    except ValueError:
        return 60.0


_IP_CHECK_CACHE: dict[str, tuple[float, dict | None]] = {}


def _cached_session_ip_check(proxy_cfg: ProxyConfig, sessid: str,
                             country: str) -> dict | None:
    """IP-pin check with a short TTL cache (default 60s).

    The check is one extra proxied request per run; caching avoids hammering
    ipinfo.io when queries arrive back-to-back on the same sessid, while a
    silent peer swap is still caught within a minute / mid-run check.
    """
    ttl = _ip_cache_ttl_s()
    if ttl > 0:
        hit = _IP_CHECK_CACHE.get(sessid)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
    info = _session_ip_check(proxy_cfg, sessid, country)
    if ttl > 0:
        # Cap cache size: sessions are per-preset (few keys), drop oldest.
        if len(_IP_CHECK_CACHE) > 32:
            oldest = min(_IP_CHECK_CACHE.items(),
                         key=lambda kv: kv[1][0])[0]
            _IP_CHECK_CACHE.pop(oldest, None)
        _IP_CHECK_CACHE[sessid] = (time.time(), info)
    return info


_BLOCK_CODES = {"captcha_v2", "captcha_image", "captcha_unknown", "consent",
                "unusual_traffic", "zero_result"}


def _has_run_timeout(result: ScrapeResult) -> bool:
    return any(str(e.get("code")) == "run_timeout" for e in result.errors)


def _is_blocked(result: ScrapeResult) -> bool:
    """True when nothing useful came through: either no page loaded, or the
    first page was not a real SERP (bot tarpit redirect / parser drift)."""
    if result.organic:
        return False
    if result.pages_fetched == 0:
        return bool(result.errors)
    codes = {str(e.get("code", "")) for e in result.errors}
    return bool(codes & _BLOCK_CODES)


def _pace_session(session) -> None:
    """Small gap between queries on the same session (don't hammer one IP)."""
    try:
        delay = float(os.environ.get("SESSION_QUERY_DELAY_S", "8"))
    except ValueError:
        delay = 8.0
    wait = delay - (time.time() - session.last_used)
    if wait > 0:
        time.sleep(min(wait, delay))


def build_solve_policy(
    proxy_cfg: ProxyConfig | None, run_id: str, country: str, solve: bool
) -> SolvePolicy:
    """CapSolver policy: enabled only when `solve` is on AND a key is present."""
    api_key = os.environ.get("CAPSOLVER_API_KEY", "")
    if not solve:
        return SolvePolicy(client=None, requested=False)
    if not api_key:
        return SolvePolicy(client=None)
    proxy_string = ""
    if proxy_cfg:
        proxy_string = proxy_cfg.capsolver_proxy_string(run_id, country)
    return SolvePolicy(
        client=CapSolverClient(api_key, timeout_s=SOLVE_TIMEOUT_S,
                               proxy_url=load_capsolver_proxy()),
        max_solves=SOLVE_MAX_PER_RUN,
        min_interval_s=SOLVE_MIN_INTERVAL_S,
        proxy_string=proxy_string,
    )


def _execute_scrape(
    params: SearchParams,
    pages: int,
    include_maps: bool,
    proxy_cfg: ProxyConfig | None,
    headless: bool,
    solve: bool,
    proxy_run_id: str,
    storage_state: dict | None,
    warmup: bool = False,
    on_progress=None,
    ip_check=None,
    expected_exit_ip: str = "",
) -> tuple[ScrapeResult, dict | None, bool, str, str]:
    started = time.time()
    deadline = started + run_timeout_s()
    timing: dict = {}
    errors: list[dict] = []

    # Egress guard: Bright Data zones cannot target Google (vendor policy).
    allowed, reason = validate_proxy_for_google(proxy_cfg)
    if not allowed:
        errors.append({"code": "egress_policy_blocked", "message": reason})
        proxy_cfg = None

    country = (params.gl if (proxy_cfg and (proxy_cfg.targeting_template
                                            or proxy_cfg.username_template))
               else "")
    playwright_proxy = (proxy_cfg.playwright_proxy(proxy_run_id, country)
                        if proxy_cfg else None)
    # Sticky-shape advisory (wrong port/param mix costs blocks or 2x rate).
    if proxy_cfg is not None:
        for warn in validate_proxy_config(proxy_cfg)["warnings"]:
            errors.append({"code": "egress_template_warn", "message": warn})

    # 1. Autocomplete (HTTP, same sessid egress when proxied).
    emit_progress(on_progress, "autocomplete", "running")
    t0 = time.time()
    try:
        proxy_url = (proxy_cfg.httpx_proxy_url(proxy_run_id, country)
                     if proxy_cfg else None)
        autocomplete = fetch_autocomplete(params, proxy_url=proxy_url)
    except Exception as e:  # noqa: BLE001, surfaced and not fatal
        autocomplete = []
        errors.append({"code": "autocomplete_error", "message": str(e)[:300]})
    timing["autocomplete_ms"] = int((time.time() - t0) * 1000)
    emit_progress(on_progress, "autocomplete", "done",
                  f"{len(autocomplete)} suggestions")

    # 2. Search (browser) with optional CapSolver loop.
    policy = build_solve_policy(proxy_cfg, proxy_run_id, country, solve)
    search = collect_search(params, pages=pages, proxy=playwright_proxy,
                            headless=headless, policy=policy,
                            storage_state=storage_state, warmup=warmup,
                            on_progress=on_progress, deadline=deadline,
                            ip_check=ip_check, expected_exit_ip=expected_exit_ip)
    timing.update(search.pop("timing_ms", {}))
    errors.extend(search.pop("errors", []))
    solves = search.pop("solves", [])
    state_out = search.pop("storage_state", None)
    ua_out = search.pop("user_agent", "")
    exit_ip_out = search.pop("exit_ip", expected_exit_ip)

    # 3. Maps (browser, best-effort).
    maps_results: list[dict] = []
    if include_maps and time.time() >= deadline:
        emit_progress(on_progress, "note", "note",
                      "Run deadline reached, skipping Maps")
    elif include_maps:
        emit_progress(on_progress, "maps", "running")
        t0 = time.time()
        maps_results, maps_errors = collect_maps(params, proxy=playwright_proxy,
                                                 headless=headless)
        timing["maps_ms"] = int((time.time() - t0) * 1000)
        if search.get("local_map") is None:
            # No local intent on the SERP either, so an empty Maps feed is a
            # legitimate "no local results" rather than selector drift.
            maps_errors = [e for e in maps_errors
                           if e.get("code") != "maps_zero_result"]
        errors.extend(maps_errors)
        emit_progress(on_progress, "maps", "done",
                      f"{len(maps_results)} places")

    emit_progress(on_progress, "finalize", "running")
    timing["total_ms"] = int((time.time() - started) * 1000)
    timing["run_id"] = new_run_id()
    result = ScrapeResult(
        query=params.q,
        locale=params.to_dict(),
        organic=search["organic"],
        related_searches=search["related_searches"],
        related_questions=search["related_questions"],
        ai_overview=search["ai_overview"],
        local_map=search["local_map"],
        local_results=search["local_results"],
        autocomplete=autocomplete,
        maps_local_results=maps_results,
        has_next=search["has_next"],
        pages_fetched=search["pages_fetched"],
        solves=solves,
        timing_ms=timing,
        errors=errors,
        mock=False,
    )
    emit_progress(on_progress, "finalize", "done")
    return (result, state_out, _is_blocked(result), str(ua_out or ""),
            str(exit_ip_out or ""))


def run_full_scrape(
    params: SearchParams,
    pages: int = 3,
    include_maps: bool = True,
    proxy_cfg: ProxyConfig | None = None,
    headless: bool = True,
    solve: bool = True,
    on_progress=None,
) -> ScrapeResult:
    """Session-aware orchestration (sticky sessid + cookies).

    While a stored session works it is reused (sticky IP + cookies) for every
    query of its preset; on TTL expiry or a blocked run it rotates and is
    re-stored after success. `RUN_RETRY=1` retries a blocked run once with a
    fresh session. Proxy-quota errors are NOT rotated: THREADS_EXHAUSTED
    backs off and retries the SAME sessid (a fresh sessid would grab another
    thread), TRAFFIC/NO_RAY fail fast with a loud error.
    """
    reload_env_if_changed()
    if not (session_reuse_enabled() and proxy_cfg is not None):
        emit_progress(on_progress, "session", "done", "session reuse disabled")
        result, _, _, _, _ = _execute_scrape(params, pages, include_maps, proxy_cfg,
                                             headless, solve, new_run_id(), None,
                                             on_progress=on_progress)
        return result

    emit_progress(on_progress, "session", "running")
    key = _session_key(params)
    manager = SessionManager()
    country = (params.gl if (proxy_cfg.targeting_template
                             or proxy_cfg.username_template) else "")
    with SessionManager.lock(key):
        session = manager.load(key)
        fresh = session is None
        if fresh:
            session = manager.create(key)
        else:
            _pace_session(session)

        if _ip_check_enabled():
            info = _cached_session_ip_check(proxy_cfg, session.sessid, country)
            if info:
                if session.exit_ip and info["ip"] != session.exit_ip:
                    # Silent proxy failover replaced the peer mid-window —
                    # cookies belong to the old IP, rotate before scraping.
                    manager.clear(key)
                    session = manager.create(key)
                    fresh = True
                session.exit_ip = info["ip"]
                session.exit_geo = info["geo"]

        warmup = fresh and _warmup_enabled()
        emit_progress(on_progress, "session", "done",
                      (session.exit_geo or "ready")
                      + (" (new)" if fresh else ""))
        midrun_check = None
        if _midrun_ip_check_enabled():
            midrun_check = lambda: _session_ip_check_light(
                proxy_cfg, session.sessid, country)
        result, state_out, blocked, ua_out, exit_ip_out = _execute_scrape(
            params, pages, include_maps, proxy_cfg, headless, solve,
            session.sessid, session.state, warmup=warmup,
            on_progress=on_progress, ip_check=midrun_check,
            expected_exit_ip=session.exit_ip)
        if exit_ip_out:
            session.exit_ip = exit_ip_out

        # Proxy-quota paths first (no rotation).
        if _has_proxy_traffic_exhausted(result):
            emit_progress(on_progress, "note", "note",
                          "Proxy quota exhausted, not retrying")
            session.solve_failures += 1
            manager.clear(key)
            return result
        if _has_proxy_targeting_error(result):
            emit_progress(on_progress, "note", "note",
                          "Proxy targeting/port error, not retrying")
            session.solve_failures += 1
            manager.clear(key)
            return result
        if _has_proxy_threads_exhausted(result) and _run_retry_enabled():
            backoff = _proxy_thread_backoff_s()
            emit_progress(on_progress, "note", "note",
                          f"Proxy threads exhausted, backing off {backoff:g}s "
                          "and retrying same session")
            time.sleep(backoff)
            result, state_out, blocked, ua_out, exit_ip_out = _execute_scrape(
                params, pages, include_maps, proxy_cfg, headless, solve,
                session.sessid, session.state, warmup=False,
                on_progress=on_progress, ip_check=midrun_check,
                expected_exit_ip=session.exit_ip)
            if exit_ip_out:
                session.exit_ip = exit_ip_out
            if _has_proxy_threads_exhausted(result):
                session.solve_failures += 1
                return result

        if blocked and _run_retry_enabled() and not _has_run_timeout(result):
            emit_progress(on_progress, "note", "note",
                          "Blocked, retrying with a fresh session")
            manager.clear(key)
            session = manager.create(key)
            if _ip_check_enabled():
                info = _cached_session_ip_check(proxy_cfg, session.sessid,
                                                country)
                if info:
                    session.exit_ip = info["ip"]
                    session.exit_geo = info["geo"]
            result, state_out, blocked, ua_out, exit_ip_out = _execute_scrape(
                params, pages, include_maps, proxy_cfg, headless, solve,
                session.sessid, None, warmup=_warmup_enabled(),
                on_progress=on_progress, ip_check=midrun_check,
                expected_exit_ip=session.exit_ip)
            if exit_ip_out:
                session.exit_ip = exit_ip_out

        session.runs += 1
        session.last_used = time.time()
        if result.organic:
            session.successes += 1
        if ua_out:
            session.user_agent = ua_out
        if state_out is not None and not blocked:
            session.state = state_out
        if blocked:
            session.solve_failures += 1
            manager.clear(key)  # start fresh next time
        else:
            manager.save(session)
        return result


def solver_status(check_balance: bool = False) -> dict:
    """Configured? Balance? (never exposes the key).

    `check_balance=False` is cheap and network-free (used by /api/health).
    `/api/solver?balance=1` attempts a live balance call capped by a
    wall-clock deadline (blocked networks can hang OS-level connects).
    """
    reload_env_if_changed()
    api_key = os.environ.get("CAPSOLVER_API_KEY", "")
    status: dict = {"configured": bool(api_key), "balance": None,
                    "proxy": bool(os.environ.get("CAPSOLVER_PROXY_SERVER", ""))}
    if not api_key:
        status["note"] = "set CAPSOLVER_API_KEY (env or .env)"
        return status
    if not check_balance:
        status["note"] = "balance via /api/solver?balance=1"
        return status

    import concurrent.futures

    client = CapSolverClient(api_key, proxy_url=load_capsolver_proxy())
    # Note: do NOT use the executor as a context manager — its __exit__
    # joins the worker, which defeats the wall-clock cap on stuck connects.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(client.get_balance, 0)
        try:
            status["balance"] = future.result(timeout=10.0)
            status["note"] = ""
        except concurrent.futures.TimeoutError:
            status["note"] = "balance check timed out (network blocked?)"
        except Exception as e:  # noqa: BLE001 — advisory endpoint
            status["note"] = str(e)[:200]
    finally:
        pool.shutdown(wait=False)
    if status["note"] and not status["proxy"]:
        status["note"] += (" — if the network blocks capsolver.com, set "
                           "CAPSOLVER_PROXY_SERVER/USERNAME/PASSWORD")
    elif status["note"] and ("ip_forbidden" in status["note"]
                             or "407" in status["note"]):
        status["note"] += (" — the proxy zone has an IP allowlist; add this "
                           "machine's public IP in the Bright Data dashboard")
    return status


def egress_status() -> dict:
    """Proxy configured? (never exposes credentials)."""
    cfg = load_proxy_from_env()
    if not cfg:
        return {"configured": False, "host": None, "provider": None,
                "mode": "none", "sticky_ok": False, "warnings": []}
    if cfg.is_brightdata():
        provider = "brightdata"
    elif cfg.is_dataimpulse():
        provider = "dataimpulse"
    else:
        provider = "generic"
    allowed, reason = validate_proxy_for_google(cfg)
    shape = validate_proxy_config(cfg)
    notes = [reason] if reason else []
    notes.extend(shape["warnings"])
    return {"configured": True, "host": cfg.server, "provider": provider,
            "google_allowed": allowed, "note": " | ".join(notes),
            "mode": shape["mode"], "port": shape["port"],
            "sticky_ok": shape["sticky_ok"], "warnings": shape["warnings"]}


def mock_result(query: str = "best pizza in new york", hl: str = "en") -> ScrapeResult:
    """Deterministic demo payload — same contract, no network.

    Picks the sample matching the request language (`ja` → Japanese
    sample, anything else → neutral English sample).
    """
    name = "sample_result.ja.json" if (hl or "").lower().startswith("ja") \
        else "sample_result.en.json"
    sample = json.loads((_SAMPLES_DIR / name).read_text("utf-8"))
    sample["query"] = query
    sample["mock"] = True
    sample.setdefault("timing_ms", {}).update(
        {"total_ms": 0, "note": "mock — no live fetch performed"}
    )
    return ScrapeResult(**{k: v for k, v in sample.items()
                           if k in ScrapeResult.__dataclass_fields__})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
