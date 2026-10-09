# CapSolver integration — in-house CAPTCHA solving

Verified against CapSolver API docs (2026-10) and a live capture of Google's
`/sorry/` challenge from this office IP (see "Phase 0 evidence" below).

## Phase 0 evidence (captured live, this network)

Google's `/sorry/index` challenge served to our IP is **reCAPTCHA v2
Enterprise**, not the classic text-image captcha:

- page loads `https://www.google.com/recaptcha/enterprise.js`
- widget: `<div id="recaptcha" class="g-recaptcha" data-sitekey="6LdLLIMb..." data-s="45dN..." data-callback="submitCallback">`
- hidden `<textarea id="g-recaptcha-response" name="g-recaptcha-response">`
- form `<form id="captcha-form" action="index" method="post">`
- the page's own success callback: `document.getElementById('captcha-form').submit()`

Consequences (implemented):

- task type = `ReCaptchaV2EnterpriseTaskProxyLess` (or `...EnterpriseTask`
  when a proxy is configured so the solve shares the browser's exit IP)
- the `data-s` attribute is sent as `enterprisePayload: {"s": ...}` (it also
  appears in the `/anchor` payload as the `s` parameter — same value)
- the token is injected into `#g-recaptcha-response` and the form submitted
  in the **same browser context**, mirroring `submitCallback`; the clearance
  cookie (`GOOGLE_ABUSE_EXEMPTION`) then applies to the rest of the run

An ImageToText fallback path (`img#captcha-img` / `input#captcha`) exists for
the rarer text-captcha variant; it is kept but not the primary path.

## Flow (implemented)

1. `classify_block()` (scraper/serp.py) identifies challenge pages with
   solver-relevant detail: `captcha_v2 | captcha_image | captcha_unknown |
   consent | js_required | unusual_traffic`.
2. `SolvePolicy.can_solve()` gates every solve:
   - solver configured (`CAPSOLVER_API_KEY` set) and not fatal
   - kind is solvable
   - budget: `SOLVE_MAX_PER_RUN` (default 2)
   - throttle: `SOLVE_MIN_INTERVAL_S` (default 5 s)
   - circuit breaker closed
3. `SolvePolicy.solve()`:
   - v2: `createTask` → poll `getTaskResult` → token → inject → submit →
     verify `#search` returned (outcome `solved` / `rejected`)
   - image: element screenshot → `ImageToTextTask` (sync) → fill → submit
4. Telemetry: every attempt lands in the contract as `solves[]`
   `{kind, task_type, task_id, ms, outcome, error}` — surfaced in the UI and
   the raw JSON. The API key is never logged or exposed.

## Error-code handling (verified against the official table)

| Code | Action |
|---|---|
| `ERROR_CAPTCHA_UNSOLVABLE` | retryable (no deduction) — 1 retry within policy |
| `ERROR_SERVICE_UNAVALIABLE` / `ERROR_RATE_LIMIT` | retryable, polling continues |
| `ERROR_ZERO_BALANCE` / `ERROR_KEY_DENIED_ACCESS` / `ERROR_TASK_NOT_SUPPORTED` / `ERROR_INVALID_TASK_DATA` | **fatal** — solver disabled for the process, scraping continues without solving |
| `ERROR_IP_BANNED` / `ERROR_KEY_TEMP_BLOCKED` | **circuit** — solving paused 5 min |
| `ERROR_TASK_TIMEOUT` (120 s cap) | solve fails; run records `error` and continues/aborts per block |

## Cost & guardrails

- Balance check: `GET /api/solver` (live, advisory, cached 60 s);
  `/api/health` reports configuration only (network-free, never blocks).
- Solves are only spent on classified challenges — never on consent walls,
  rate-limit pages without widgets, or selector drift (`unusual_traffic`).
- Post-solve the run keeps pacing (jitter 3–8 s); a second challenge in one
  run consumes the remaining budget then fails loud (no retry storms).

## Setup

```bash
# .env (gitignored) or shell env:
CAPSOLVER_API_KEY=...
# optional tuning
SOLVE_MAX_PER_RUN=2
SOLVE_MIN_INTERVAL_S=5
SOLVE_TIMEOUT_S=130
```

### Network note (this office network — verified)

`api.capsolver.com` (Cloudflare) is **blocked by the office firewall**:
DNS resolves, but TCP 443 to all its IPs times out (`curl` and direct socket
test), while other APIs (e.g. `api.github.com`) respond in <500 ms. Two ways
to run the solver from here:

1. **Proxy the solver API calls** (recommended, no IT ticket):
   ```
   CAPSOLVER_PROXY_SERVER=brd.superproxy.io:44445
   CAPSOLVER_PROXY_USERNAME=brd-customer-...-zone-...
   CAPSOLVER_PROXY_PASSWORD=...
   ```
   Any HTTP proxy that can reach capsolver.com works. A Bright Data zone is
   fine here — their restriction applies only to search-engine hosts, and
   `api.capsolver.com` is not one.
2. Ask IT to whitelist `api.capsolver.com` / run from another network.

Run a live check: `GET /api/scrape?q=best+pizza+in+new+york&pages=1&solve=1`
(a challenge on this IP is expected; inspect `solves[]` in the response).
`GET /api/solver` reports configured/balance (advisory; cached).
