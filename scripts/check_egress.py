"""Egress evidence tool (Phase 0) — run with proxy creds in env.

Captures what the configured proxy actually does for:
  1. a neutral target  (http://brdtest.com/myip.json) — proves proxy works
  2. a Google target   (google.com/search)            — vendor policy check
and prints status + relevant headers (`x-brd-ip`, `x-brd-err-code`,
`x-luminati-error`, `server`). This produces the evidence quoted in
docs/PROVIDERS.md / docs/FEASIBILITY.md for the PM report.

Usage:
    set BRIGHTDATA_PROXY_HOST=brd.superproxy.io & set BRIGHTDATA_PROXY_PORT=44445
    set BRIGHTDATA_PROXY_USER=... & set BRIGHTDATA_PROXY_PASSWORD=...
    python scripts/check_egress.py
No secrets are printed — only presence flags.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.config import load_proxy_from_env  # noqa: E402

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

INTERESTING = ["x-brd-ip", "x-brd-err-code", "x-brd-error", "x-brd-err-msg",
               "proxy-status", "x-luminati-error", "server", "content-type"]


def probe(client: httpx.Client, name: str, url: str) -> None:
    print(f"\n=== {name}: {url}")
    try:
        r = client.get(url, timeout=30, follow_redirects=False)
        print(f"status: {r.status_code}  final: {str(r.url)[:120]}")
        for h in INTERESTING:
            if h in r.headers:
                print(f"  {h}: {r.headers[h][:200]}")
        body = r.text[:200].replace("\n", " ")
        print(f"  body[:200]: {body}")
    except Exception as e:  # noqa: BLE001 — evidence tool
        print(f"ERROR: {type(e).__name__}: {str(e)[:200]}")


def main() -> int:
    cfg = load_proxy_from_env()
    if not cfg:
        print("No proxy configured (PROXY_SERVER or BRIGHTDATA_PROXY_* env vars).")
        return 1
    print(f"proxy host: {cfg.server} | user set: {bool(cfg.username)} | "
          f"password set: {bool(cfg.password)}")
    proxy_url = cfg.server if "://" in cfg.server else f"http://{cfg.server}"
    if cfg.username:
        from urllib.parse import quote

        proxy_url = proxy_url.replace(
            "://", f"://{quote(cfg.username, safe='')}:{quote(cfg.password, safe='')}@", 1
        )
    with httpx.Client(proxy=proxy_url, headers=HEADERS,
                      verify=False) as client:  # proxy MITM certs
        probe(client, "neutral", "http://brdtest.com/myip.json")
        probe(client, "google", "https://www.google.com/search?q=egress+check&hl=en&gl=us")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
