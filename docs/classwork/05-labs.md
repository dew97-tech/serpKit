# 05 Labs — hands-on

Run in order. No proxy/solver key needed for 1-2.

## Lab 1 — mock contract (10 min)

- `python -m uvicorn api:app --host 127.0.0.1 --port 8000`
- Open `http://127.0.0.1:8000/?mock=1`, try `q=best pizza in new york`.
- `GET /api/scrape?q=best+pizza&mock=1` → check `organic`, `related_searches`, `ai_overview`, `mock:true`.
- `GET /api/scrape?q=新宿 ラーメン&preset=japan&mock=1` → `hl:ja` sample.
- Exercise: map one field to `docs/MAPPING.md` consumer.

## Lab 2 — unit tests, no network (10 min)

- `python -m unittest discover tests -v`
- Covers: `test_parsers.py` (organic/PAA/AI/local), `test_config.py` (uule, URLs, presets), `test_sessions.py` (TTL, save/clear), `test_api_stream.py`, `test_capsolver.py`, `test_i18n.py`.

## Lab 3 — stream trace (15 min)

- `GET /api/scrape/stream?q=...&mock=1` in browser or curl. Observe `stage` events: `session/autocomplete/organic/ai/paa/related/maps/finalize` (`ui/app.js:190-193`).
- Exercise: correlate each stage to `scraper/runner.py:268-336`.

## Lab 4 — live run, pages=1 (needs egress, 20 min)

- Set `.env` from `.env.example` (`PROXY_*`, `CAPSOLVER_API_KEY`).
- `GET /api/scrape?q=best+pizza+in+new+york&preset=us&pages=1&include_maps=0`
- Check `/api/session?preset=us` (exit IP, TTL), `/api/solver?balance=1`, `solves[]`, `timing_ms`, `errors[]`.
- Exercise: force blocked path — observe fresh-session retry note in stream.

## Lab 5 — illustration QA (15 min)

- Open `assets/serpkit-illustrations/` — six SVGs + `shot-list.md`.
- Apply `QA.md`: 10-second test (name the stages in order without reading), English-only, arrow labels, mono for code, no overlap, palette meanings, xiaohei as accent only.
- Cross-check one figure against its mermaid block in the matching chapter — labels must match.
- Exercise: find one label you would rename to match the codebase even better.

## Pass criteria

- Mock JSON matches `ScrapeResult` shape. Tests green. Stream stages explained. Live run errors classified correctly. One illustration QA done.
