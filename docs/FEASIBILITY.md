# Feasibility note: manual scraping vs SerpAPI

Target surface (neutral default): `www.google.com` desktop, `hl=en / gl=us`.
`japan` preset (`google.co.jp`, `hl=ja / gl=jp / lr=lang_ja`, Tokyo) is kept
and re-enabled later — the collector is fully parameterized, no JP hardcoded.
Proven by this implementation: all 8 blocks are collectible without a SerpAPI key.
with the reliability caveats below.

## What works (and how)

| Block | Technique | Reliability |
|---|---|---|
| organic top-30 (pages 1–3, `start=0,10,20`) | Playwright, one context, `#search a:has(h3)` structure selectors | High — classic block, stable anchors |
| `related_searches` (page-1 bottom) | Rendered HTML, heading `関連する検索`/`Related searches` + link fallback | High |
| PAA questions | Static HTML (`data-q` / accordion roles) | High for questions |
| PAA answers + source links | Click-expand first ≤6 accordions, 800–1600ms jitter, re-read DOM; inline Google errors retried once, kept honest (empty snippet) | Medium — each click is extra traffic; cap depth 2 |
| AI Overview / AI Mode text (+ cited links for classic) | Bounded wait for the late-rendered panel, scroll nudge, `もっと見る`/`Show more` expansion; classic overviews preferred, AI Mode replies captured with `source:"ai_mode"`; **kept only when `text_blocks` non-empty** | Medium — streams async; AI Overview absent for many queries/sessions by design |
| local pack `local_map` + `local_results` | Rendered HTML `div.VkpGBb` cards: title, rating/reviews (aria-label), address; `phone/website/place_id` need a place-page pass | Medium — markup drifts |
| `google_autocomplete` | Plain HTTP `/complete/search` (JSONP) — no browser | Very high |
| Maps search top-10 | Separate `/maps/search` load, list-feed parse: title, rating/reviews, address, hours (website needs place-page pass) | Medium — never fails the whole scrape |
| `has_next` pagination | `#pnnext` / 次のページ presence | High |

## Anti-bot / CAPTCHA handling (implemented)

- Since Jan-2025 SearchGuard, Google requires JS + valid TLS/browser fingerprint:
  plain `requests`/Guzzle gets a JS wall or `/sorry/` CAPTCHA within ~10–50 hits.
- Mitigations: real Chromium, pre-load `init_script` fingerprint patch
  (crawl4ai anti-detection pattern) built per request locale, rotating
  desktop UA, `pws=0` (no personalization), one context per run, 3–8s jitter
  between pages.
- Chromium runs with `--disable-http2`: navigation to Google over HTTP/2
  stalls through the DataImpulse residential proxy (verified 0/6 attempts
  reach the page vs 6/6 with HTTP/1.1), while plain HTTP clients are fine.
- Media route blocking (images/fonts/media aborted in-context) cuts proxy
  bandwidth ~3×; HTML/JS still load so all parsers are unaffected.
- Run budget: every live run has a cooperative wall-clock deadline
  (`RUN_TIMEOUT_S`, default 600 s). Page fetches and solver attempts check
  the deadline, partial data returns with a `run_timeout` error, and Maps is
  skipped once the deadline passes. The viewer sizes its watchdog from
  `/api/health` (`run_timeout_s` plus one minute).
- Session reuse (`SESSION_REUSE=1`): one sticky residential IP + persisted
  Google cookies are reused across queries while they work. DataImpulse uses
  the primary sticky mechanism — sticky port 10000 + `sessttl.25`, so the IP
  rotation and our 25-min cookie TTL stay aligned (docs recommend ≤30 min;
  `sessid` on the rotating port is only the "average 30 min" alternative).
  Because dead peers are silently replaced mid-window, every run verifies the
  exit IP through the session's own proxy (`SESSION_IP_CHECK=1`) and rotates
  the session when it changed. Solves: 3 in-run attempts, each retry reloads
  the URL for a fresh Enterprise challenge instead of OCR-ing the escalated
  image captcha; a run that ends without organic (captcha or bot-tarpit
  redirect) retries once with a fresh session (`RUN_RETRY=1`).
- Fingerprint truth-up: launches system Chrome (`channel="chrome"`, real UA —
  the old forced `Chrome/131` string contradicted the 153/154 engine),
  removes Playwright's `--enable-automation`, adds
  `--disable-blink-features=AutomationControlled`; init patches cover
  webdriver/languages/chrome-stub/SwiftShader-WebGL/permissions. New sessions
  warm up on the Google homepage before `/search` (natural referer chain).
- **CapSolver loop (implemented, see `docs/CAPSOLVER.md`)**: live capture on
  this network showed Google `/sorry/` = reCAPTCHA v2 **Enterprise**
  (sitekey + `data-s`). The collector classifies the challenge, solves via
  CapSolver (`ReCaptchaV2EnterpriseTaskProxyLess`, proxy-bound variant when an
  egress exists), injects the token into the same browser context and submits
  the page's own form. Budget (2/run), throttle, circuit breaker and
  `solves[]` telemetry are enforced; consent walls and widget-less rate-limit
  pages are never sent to the solver.
- Egress: provider-agnostic proxy config. **Bright Data proxy zones cannot
  target Google** (vendor policy: DC/ISP → 403; Residential/Mobile →
  superproxy bypass) — guard + evidence tool in `docs/PROVIDERS.md`.

## Rate limits (expectation, no proxy)

- ~100–500 queries/day/IP with pacing before CAPTCHA pressure; burst traffic
  (>10 req/min from one IP block) flags the whole block.
- Pagination triples cost: top-30 = 3 page loads + PAA/AI clicks + Maps load
  (~6–10s + render, ~2MB each vs 60KB bare HTML). Cache 24h per
  `query+hl+gl+location`; dedupe URLs across pages.

## Cost vs SerpAPI

- DIY/headless: ~$5–15/1k queries in residential proxy bandwidth + Chromium
  infra + ongoing selector upkeep (classes like `VwiC3b` rotate; this service
  isolates them in `scraper/selectors.json` with fallback chains + zero-result
  alarming).
- SerpAPI-style API: flat per-call, no infra, parser upkeep outsourced —
  cheaper whenever volume or eng time matters.
- Recommendation: **sidecar** (this Python service behind Laravel
  HTTP/queue) beats a full PHP rewrite — PHP has no official Playwright and
  Panther/Selenium stealth is weaker (see `docs/PHP_PORT.md`). Use manual
  scraping where SerpAPI cost dominates; keep API fallback for burst/blocked
  periods.

## Observed live-run evidence (2026-10-09, office IP, no proxy)

- `GET /api/autocomplete?q=新宿 ラーメン` → **10 suggestions, HTTP 200**. Plain-HTTP path works.
- `GET /api/scrape?q=...&pages=1` (headless Chromium) → Google served
  `/sorry/index` CAPTCHA on the **first** request. Collector classified it
  (`captcha_v2`, enterprise, sitekey + `data-s` captured — see
  `docs/CAPSOLVER.md`) instead of hammering; contract + timing intact.
- With `CAPSOLVER_API_KEY` set and `solve=1`, the collector now solves the
  challenge in-context and continues (budget 2/run, telemetry in `solves[]`).
  Without a key it records `solver_not_configured` in `solves[]` and fails
  loud — exactly the honest degradation the PM asked to see.
- Content extraction at volume needs a residential/mobile egress in the
  target region (see `docs/PROVIDERS.md` for verified provider constraints).

## Demo keywords (per preset — confirm with PM)

US (neutral): `best pizza in new york`, `dentist new york`,
`how to file taxes`, `best VPN comparison`.
Japan (later): `新宿 ラーメン おすすめ`, `東京 歯医者 おすすめ`,
`確定申告 やり方`, `VPN おすすめ 比較`.
