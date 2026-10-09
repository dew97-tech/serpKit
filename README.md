# SERP KIT

In-house Google SERP collector, JSON API, and viewer. It replaces paid
per-call SERP APIs for the Laravel `seo_api` SEO platform and returns a frozen
JSON contract the existing consumers already understand.

## Why

- Per-call SERP APIs are the largest recurring cost in the SEO pipeline.
- The Laravel side needs specific blocks (organic, PAA, AI Overview, local,
  Maps, autocomplete), not a generic search response.
- The contract in `scraper/schemas.py` mirrors the current SerpAPI subset, so
  swapping the transport does not touch Laravel business logic.

## What it collects

| Block | Contract field |
|---|---|
| Organic results, pages 1 to 3 | `organic[{position,title,link,snippet}]` |
| Related searches | `related_searches[]` |
| People Also Ask questions | `related_questions[{question,snippet,link}]` |
| AI Overview or AI Mode reply | `ai_overview{text_blocks,links[,source]}` |
| Local pack | `local_map` and `local_results[]` |
| Autocomplete suggestions | `autocomplete[]` |
| Google Maps top list | `maps_local_results[]` with rating, reviews, hours, phone |
| Pagination | `has_next`, `pages_fetched` |

## How it works

1. `GET /api/scrape` resolves the locale preset and starts one run.
2. Autocomplete is fetched over plain HTTP.
3. Search pages 1 to 3 load in one Chromium context with a fixed desktop
   fingerprint (real Chrome UA, pre-load patches, HTTP/1.1 only).
4. Challenge pages are classified. When Google serves a CAPTCHA, CapSolver
   solves it in-context with up to three attempts, each retry reloading for a
   fresh challenge.
5. Organic, related searches, PAA, AI Overview or AI Mode, and the local pack
   are parsed from the captured page. Off-query AI-Mode results trigger an
   automatic classic Web-mode (`udm=14`) refetch.
6. Maps is collected as a separate best-effort pass.
7. Sessions reuse one sticky residential IP plus stored cookies for up to 25
   minutes. A per-run exit IP check rotates the session when the proxy swaps
   peers silently. A run without organic results retries once with a fresh
   session.
8. `mock=1` serves `samples/sample_result.en.json` or `.ja.json` with zero
   network, for demos and integration work.

## Quick start

```bash
git clone https://github.com/dew97-tech/serpKit.git
cd serpKit
pip install -r requirements.txt
python -m playwright install chromium

# Optional but recommended: copy .env.example to .env and fill in
# CAPSOLVER_API_KEY, PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD.
cp .env.example .env

python -m uvicorn api:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000/
```

Without a proxy or solver key the service still runs: use `mock=1` in the UI
or the API to serve the frozen sample contract.

Tests:

```bash
python -m unittest discover tests -v
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/scrape` | Full collection run. Params: `q`, `preset`, `hl`, `gl`, `domain`, `location`, `city`, `state`, `country`, `uule`, `pages` (1 to 3), `include_maps`, `mock`, `solve`, `headless` |
| `GET /api/scrape/stream` | Same parameters, Server-Sent Events with real stage updates, used by the viewer |
| `GET /api/autocomplete` | Plain HTTP suggestions for one query |
| `GET /api/locales` | Locale presets (`us`, `japan`, `bangladesh`) |
| `GET /api/demo-keywords` | Demo keyword matrix per preset |
| `GET /api/session` | Session life per preset: remaining TTL, exit IP, geo, runs, user agent |
| `GET /api/solver` | CapSolver status, `?balance=1` adds a live balance check |
| `GET /api/health` | Service, egress, solver, and session summary |

## Configuration

All variables are optional. See `.env.example` for the full list.

| Variable | Purpose |
|---|---|
| `CAPSOLVER_API_KEY` | Enables CAPTCHA solving |
| `CAPSOLVER_PROXY_SERVER/USERNAME/PASSWORD` | Route solver API calls when the network blocks capsolver.com |
| `PROXY_SERVER/USERNAME/PASSWORD` | Residential egress for Google |
| `PROXY_USERNAME_TEMPLATE` | Full username template, for example `{username}__cr.{country};sessid.{run_id};sessttl.25` |
| `SESSION_REUSE`, `SESSION_TTL_S`, `SESSION_WARMUP`, `SESSION_IP_CHECK`, `RUN_RETRY` | Session lifecycle controls |
| `RUN_TIMEOUT_S` | Per-run wall-clock budget in seconds, default 600 |
| `SOLVE_MAX_PER_RUN`, `SOLVE_MIN_INTERVAL_S`, `SOLVE_TIMEOUT_S` | Solver budget and limits |
| `BROWSER_CHANNEL` | `chrome` (default) or `chromium` |
| `SERP_DEBUG_HTML_DIR` | Saves captured HTML for selector debugging |

## Project layout

- `api.py`: FastAPI service, JSON API plus the single-page viewer.
- `scraper/`: collector. `config.py` (presets, URLs, egress), `serp.py`
  (search collection, block handling, parsers), `maps.py`, `autocomplete.py`,
  `capsolver.py`, `sessions.py`, `schemas.py`, `selectors.json`.
- `ui/`: dependency-free viewer (EN and JP chrome, tabs, progress, raw JSON).
- `scripts/`: `benchmark.py`, `check_egress.py`, `allowlist_nat_pool.py`.
- `docs/`: feasibility, contract mapping, provider notes, solver notes, PHP
  port guide.
- `tests/`: unit tests, no network required.

## Current limitations

- PAA answer text is served behind an async endpoint that Google currently
  rejects for fresh automated sessions. Questions are collected; answers stay
  empty rather than guessing.
- AI Overview availability varies per query and session by Google design.
- CAPTCHA solve acceptance is probabilistic. Sessions amortize it to roughly
  one solve per 25 minute window.
- Selector upkeep is ongoing. Zero-result runs auto-capture HTML for repairs.

## Documentation

- `docs/FEASIBILITY.md`: reliability, anti-bot handling, cost comparison.
- `docs/MAPPING.md`: block to consumer table for `seo_api`.
- `docs/PROVIDERS.md`: egress provider policy and options.
- `docs/CAPSOLVER.md`: solver integration and error handling.
- `docs/PHP_PORT.md`: sidecar recommendation for the Laravel end state.
