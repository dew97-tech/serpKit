"""SERP KIT FastAPI service: JSON API plus the single-page viewer.

Run:
    python -m uvicorn api:app --host 127.0.0.1 --port 8000
Open:
    http://127.0.0.1:8000/                                   (viewer UI)
    http://127.0.0.1:8000/api/scrape?q=best+pizza&mock=1     (no browser)
    http://127.0.0.1:8000/api/scrape?q=best+pizza            (live scrape)

Locale presets (`us`, `japan`, `bangladesh`) set hl/gl/domain/location and
any explicit parameter overrides them. `mock=1` serves the frozen sample
contract with no network. Live mode needs Chromium and ideally a residential
egress in the target region (docs/FEASIBILITY.md).

`/api/scrape/stream` is the same collection over Server-Sent Events and
emits real stage updates for the viewer. The synchronous endpoint stays the
stable contract for API consumers.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from scraper.autocomplete import fetch_autocomplete
from scraper.config import (
    AUTO_GL_COUNTRIES,
    DEFAULT_PRESET,
    LOCALE_PRESETS,
    SearchParams,
    build_uule,
    canonical_location_uule,
    load_proxy_from_env,
    parse_location,
    resolve_preset,
)
from scraper.runner import egress_status, mock_result, run_full_scrape, solver_status
from scraper.sessions import SessionManager

app = FastAPI(title="SERP KIT")

BASE_DIR = Path(__file__).parent
UI_DIR = BASE_DIR / "ui"


@app.middleware("http")
async def no_cache_ui(request, call_next):
    """UI assets are never cached so code updates always take effect."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# Demo matrix per preset: AI x PAA x local coverage.
DEMO_KEYWORDS = {
    "us": [
        {"q": "best pizza in new york", "expects": "local pack + PAA, AI varies"},
        {"q": "dentist new york", "expects": "local pack + Maps strong"},
        {"q": "how to file taxes", "expects": "AI Overview + PAA, no local"},
        {"q": "best VPN comparison", "expects": "AI Overview + PAA, no local"},
    ],
    "japan": [
        {"q": "新宿 ラーメン おすすめ", "expects": "local pack + PAA, AI varies"},
        {"q": "東京 歯医者 おすすめ", "expects": "local pack + Maps strong"},
        {"q": "確定申告 やり方", "expects": "AI Overview + PAA, no local"},
        {"q": "VPN おすすめ 比較", "expects": "AI Overview + PAA, no local"},
    ],
    "bangladesh": [
        {"q": "best burgers in dhaka", "expects": "local pack + Maps strong"},
        {"q": "best restaurant in dhaka", "expects": "local pack + PAA"},
        {"q": "top hospitals in dhaka", "expects": "local pack + Maps strong"},
        {"q": "how to file taxes in bangladesh", "expects": "AI Overview + PAA, no local"},
    ],
}


def _resolve_params(
    preset: str,
    q: str,
    hl: str = "",
    gl: str = "",
    domain: str = "",
    city: str = "",
    state: str = "",
    country: str = "",
    uule: str = "",
    location: str = "",
) -> SearchParams:
    base = resolve_preset(preset)
    city_v = city or base.get("city", "")
    state_v = state or base.get("state", "")
    country_v = country or base.get("country", "")

    canonical, loc_code = parse_location(location)
    if canonical:
        parts = canonical.split(",")
        if len(parts) >= 3:
            city_v = city or parts[0]
            state_v = state or parts[1]
            country_v = country or parts[2]
        elif len(parts) == 2:
            city_v = city or parts[0]
            country_v = country or parts[1]

    gl_v = gl or base["gl"]
    if loc_code in AUTO_GL_COUNTRIES and gl_v == base["gl"]:
        gl_v = loc_code

    if uule:
        uule_v = uule
    elif canonical:
        uule_v = build_uule(canonical)
    else:
        uule_v = canonical_location_uule(city_v, state_v, country_v)

    return SearchParams(
        q=q,
        hl=hl or base["hl"],
        gl=gl_v,
        google_domain=domain or base["google_domain"],
        lr=base.get("lr", ""),
        city=city_v,
        state=state_v,
        country=country_v,
        uule=uule_v,
    )


def _append_no_egress(result, proxy_cfg) -> None:
    if proxy_cfg is None:
        result.errors.append({
            "code": "no_egress",
            "message": (
                "Live scrape has no egress proxy (PROXY_* in .env), traffic "
                "exits from this machine's IP and will be challenged. "
                "See docs/PROVIDERS.md."
            ),
        })


def _sessions_summary() -> list[dict]:
    """Preset-name to session-key (hl-gl) summaries, e.g. bangladesh to en-bd."""
    manager = SessionManager()
    summaries = []
    for name, cfg in sorted(LOCALE_PRESETS.items()):
        key = f"{cfg['hl']}-{cfg['gl']}"
        summary = manager.summary(key)
        summary["preset"] = name
        summary["key"] = key
        summaries.append(summary)
    return summaries


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "default_preset": DEFAULT_PRESET,
        "presets": sorted(LOCALE_PRESETS),
        "solver": solver_status(),
        "egress": egress_status(),
        "sessions": _sessions_summary(),
    }


@app.get("/health", include_in_schema=False)
def health_alias() -> dict:
    """Alias for simple monitors that poll /health."""
    return health()


@app.get("/api/locales")
def locales() -> dict:
    return {"default_preset": DEFAULT_PRESET, "presets": LOCALE_PRESETS}


@app.get("/api/demo-keywords")
def demo_keywords(preset: str = DEFAULT_PRESET) -> dict:
    key = preset if preset in DEMO_KEYWORDS else DEFAULT_PRESET
    base = resolve_preset(key)
    return {"preset": key, "keywords": DEMO_KEYWORDS[key], "locale": base}


@app.get("/api/autocomplete")
def autocomplete(
    q: str = Query(..., min_length=1),
    preset: str = DEFAULT_PRESET,
    hl: str = "",
    gl: str = "",
) -> JSONResponse:
    base = resolve_preset(preset)
    params = SearchParams(q=q, hl=hl or base["hl"], gl=gl or base["gl"],
                          google_domain=base["google_domain"])
    try:
        suggestions = fetch_autocomplete(params)
        return JSONResponse({"q": q, "suggestions": suggestions, "mock": False})
    except Exception as e:  # noqa: BLE001, the error shape is part of the API
        return JSONResponse({"q": q, "suggestions": [],
                             "error": str(e)[:300]}, status_code=502)


@app.get("/api/solver")
def solver(balance: bool = False) -> dict:
    """CapSolver status; `?balance=1` adds a live, time-capped check."""
    return solver_status(check_balance=balance)


@app.get("/api/session")
def session_status(preset: str = "") -> dict:
    """Per-preset session life: remaining TTL, exit IP, geo, runs, UA.

    `preset` accepts a preset name ("bangladesh") or a session key ("en-bd").
    """
    manager = SessionManager()
    if preset:
        cfg = LOCALE_PRESETS.get(preset)
        key = f"{cfg['hl']}-{cfg['gl']}" if cfg else preset
        summary = manager.summary(key)
        summary["key"] = key
        if cfg:
            summary["preset"] = preset
        return {"presets": [summary]}
    return {"presets": _sessions_summary()}


@app.get("/api/scrape")
async def scrape(
    q: str = Query(..., min_length=1),
    preset: str = DEFAULT_PRESET,
    hl: str = "",
    gl: str = "",
    domain: str = "",
    city: str = "",
    state: str = "",
    country: str = "",
    uule: str = "",
    location: str = "",
    pages: int = Query(3, ge=1, le=3),
    include_maps: bool = True,
    mock: bool = False,
    headless: bool = True,
    solve: bool = True,
) -> JSONResponse:
    params = _resolve_params(preset, q, hl, gl, domain, city, state, country,
                             uule, location)
    if mock:
        return JSONResponse(mock_result(params.q or q, params.hl).to_dict())
    proxy_cfg = load_proxy_from_env()
    # sync_playwright blocks, so run it off the event loop.
    result = await asyncio.to_thread(run_full_scrape, params, pages,
                                     include_maps, proxy_cfg, headless, solve)
    _append_no_egress(result, proxy_cfg)
    status = 200 if not result.errors or result.organic else 502
    return JSONResponse(result.to_dict(), status_code=status)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@app.get("/api/scrape/stream")
async def scrape_stream(
    q: str = Query(..., min_length=1),
    preset: str = DEFAULT_PRESET,
    hl: str = "",
    gl: str = "",
    domain: str = "",
    city: str = "",
    state: str = "",
    country: str = "",
    uule: str = "",
    location: str = "",
    pages: int = Query(3, ge=1, le=3),
    include_maps: bool = True,
    mock: bool = False,
    headless: bool = True,
    solve: bool = True,
) -> StreamingResponse:
    """Same collection as /api/scrape, streamed as Server-Sent Events.

    Events: {"type":"stage","stage":...,"state":...,"detail":...} while the
    run advances, then {"type":"result","result":...} or
    {"type":"error","message":...}.
    """
    params = _resolve_params(preset, q, hl, gl, domain, city, state, country,
                             uule, location)
    headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}

    if mock:
        result = mock_result(params.q or q, params.hl).to_dict()

        async def mock_stream():
            yield _sse({"type": "result", "result": result})

        return StreamingResponse(mock_stream(),
                                 media_type="text/event-stream",
                                 headers=headers)

    proxy_cfg = load_proxy_from_env()
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_progress(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def run() -> None:
        try:
            result = run_full_scrape(params, pages, include_maps, proxy_cfg,
                                     headless, solve, on_progress)
            _append_no_egress(result, proxy_cfg)
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "result", "result": result.to_dict()})
        except Exception as e:  # noqa: BLE001, reported to the client
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "error", "message": str(e)[:300]})

    task = asyncio.create_task(asyncio.to_thread(run))

    async def event_stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event)
                if event.get("type") in ("result", "error"):
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(event_stream(),
                             media_type="text/event-stream",
                             headers=headers)


if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(str(UI_DIR / "index.html"))
