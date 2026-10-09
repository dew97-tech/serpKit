# PHP port guide (Laravel end-state)

## Recommended: sidecar, not rewrite

Keep this Python service as the collector; Laravel calls it over HTTP/queue
(Horizon/Redis), caches 24h by `query+hl+gl+location`. Reasons:

- PHP has **no official Playwright**. `symfony/panther` + chromedriver or
  `php-webdriver` + Selenium grid can load pages but stealth, async AI/PAA
  expansion and context isolation are weaker and less maintained than
  Playwright + `patchright`-style patches.
- Guzzle + DomCrawler (no JS) covers only `autocomplete`, base `organic`,
  `related_searches` — it **loses** AI Overview, PAA answers, local pack,
  Maps (the PM-critical blocks).

## If a pure-PHP port is mandated

1. Port `scraper/config.py` → `App\Services\Serp\SearchParams` (same fields).
2. Reuse `scraper/selectors.json` chains verbatim with DomCrawler
   (`filter('#search a:has(h3)')` etc.) — transport changes, selectors don't.
3. Panther for `/search` pages 1–3 + PAA/AI click expansion; Guzzle for
   `/complete/search`; accept Maps degradation or keep the Python Maps endpoint.
4. Replay-test: save live HTML + `samples/sample_result.json` shape fixtures;
   PHP must reproduce the same contract, including `ai_overview=null` when no
   `text_blocks` and the `zero_result` drift alarm.
5. Budget: residential/mobile JP proxy pool, 3–8s jitter, CAPTCHA stop (no
   solvers), selector review cadence — same ops cost as Python, more eng time.
