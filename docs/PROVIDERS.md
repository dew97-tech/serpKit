# Egress providers (IP layer) — in-house scraper policy & options

The scraper is provider-agnostic: any HTTP(S)/SOCKS proxy can be plugged via
env (`.env.example`). This file records the verified policy situation and the
selection criteria, so procurement is a config change, not a code change.

## Bright Data — verified: cannot target Google on proxy zones

From Bright Data's own docs/FAQ (fetched 2026-10):

- **Datacenter & ISP zones:** request denied —
  `HTTP/1.1 403 Search engine host is not allowed`
  (`X-Luminati-Error: ... use the SERP API product for targeting this URL`).
- **Residential & Mobile zones:** request **bypasses the peer** and is sent
  from their superproxy instead (`x-brd-ip: superproxy bypass`), i.e. the
  residential IP is never used for search engines.
- Their documented answer to "Can I target Google SERPs from the Residential,
  Datacenter, or ISP network?" is simply **No** — the SERP API product is
  required, which this in-house scraper intentionally does not use.

Our code enforces this with a guard: a Bright Data host + Google target
raises `egress_policy_blocked` with the explanation and skips the proxy.
Override `ALLOW_BD_GOOGLE=1` exists **only** to capture evidence.

Evidence tool (run when BD creds are present — prints status + `x-brd-*`
headers, no secrets):

```bash
set BRIGHTDATA_PROXY_HOST=... & set BRIGHTDATA_PROXY_PORT=... & set BRIGHTDATA_PROXY_USER=... & set BRIGHTDATA_PROXY_PASSWORD=...
python scripts/check_egress.py
```

Bright Data credits remain useful for **non-Google** targets (e.g. fetching
competitor/result pages in later enrichment phases) — keep them in `.env`.

## What the in-house scraper needs from an egress provider

| Requirement | Why |
|---|---|
| Rotating **residential or mobile** IPs | Google blocks datacenter ranges on sight; residential is the practical default, mobile the highest-trust (2–4× price) |
| **Sticky sessions** (same IP across pages 1–3 + solve) | pagination coherence + CapSolver token/IP consistency |
| **Country/city targeting** | locale-accurate SERPs (with `gl/hl/uule` we already send) |
| **IP-layer only** — no bundled SERP API required | in-house parsing/selection is the point |
| HTTP(S) or SOCKS5 + simple user/pass auth | works with our Playwright + CapSolver proxy binding |

## Config templates (provider-agnostic)

Canonical DataImpulse sticky (recommended — rotating transport + sessid pin,
same IP ~30min, one sessid per preset):

```
PROXY_SERVER=gw.dataimpulse.com:823
PROXY_USERNAME=base-user
PROXY_PASSWORD=...
PROXY_USERNAME_TEMPLATE={username}__cr.{country};sessid.{run_id}
```

True port-sticky alternative (IP bound to port 10000-20000):

```
PROXY_SERVER=gw.dataimpulse.com:10001
PROXY_USERNAME_TEMPLATE={username}__cr.{country};sessttl.25
```

Do NOT mix `sessid` (ports 823/824) with `sessttl` (ports 10000+) — the
runner warns (`egress_template_warn`) and `scripts/check_egress.py` validates.
Legacy suffix templates remain for Bright Data (`-session-{run_id}`).

`{run_id}` is the per-session sessid (sticky for pages 1–3 + Maps + solve);
the same proxy string is passed to CapSolver so the solve shares the
browser's IP (`ProxyConfig.capsolver_proxy_string`). Closing the browser
after each task frees the DataImpulse thread; the session survives because
sessid+cookies are persisted, not the socket.

Threads: one open connection = one thread (plan limit 2000, else
`407 THREADS_EXHAUSTED`). The runner backs off `PROXY_THREAD_BACKOFF_S`
(default 8s) and retries the SAME sessid — a fresh sessid would grab another
thread. `TRAFFIC_EXHAUSTED` / `NO_RAY` fail fast, never rotate.

## Candidate providers (IP-layer only, permit Google SERP scraping)

Benchmarks and vendor docs (2026) indicate this class of provider — rotating
residential at roughly **$1/GB**, no KYC for entry tiers, sticky sessions,
country targeting included, city/ZIP sometimes at 2× — e.g. **DataImpulse**
(`gw.dataimpulse.com:823`, `_country-us`, `_session-...`; publishes a
Playwright + Google reference flow), and the mid-market options compared in
independent tests (Decodo/Webshare/IPRoyal-class). Verify current terms and
success rate on **our** keywords before committing:

- instrument: success %, CAPTCHA/1k, avg ms, bytes per SERP, cost per
  successful fetch (not per GB)
- block images/fonts/media (our Playwright context can add route blocking)
- start with the smallest tier ($5–10) and run the demo keyword matrix

## Cost math (for the feasibility note)

- HTML-only SERP ≈ 300 KB; 3 pages + Maps ≈ ~1 MB/keyword with media blocked.
- At $1/GB: ≈ $0.001/keyword in bandwidth + CapSolver solves only when
  challenged (sub-cent at typical challenge rates) + server ≈ negligible.
- SerpAPI equivalent (3 calls/keyword) ≈ $0.045/keyword at list pricing.
- In-house wins at scale; the reliability caveats are measured, not assumed
  (see `FEASIBILITY.md` metrics table once the benchmark run lands).

## PHP (Laravel) end-state

Egress + solver are pure HTTP/SOCKS — portable to Guzzle/cURL. Browser
automation (PAA/AI expansion) is the only Python-bound part; the recommended
end-state is a self-hosted sidecar (still in-house) or a later
Panther/Selenium port. See `PHP_PORT.md`.
