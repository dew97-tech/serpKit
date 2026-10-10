"""Egress evidence tool — run with proxy creds in env.

Probes through the configured PROXY_* egress:
  1. neutral (ipify) — proves proxy works + shows exit IP
  2. geo (ipinfo.io/json) — proves country targeting (__cr.xx)
  3. google search — vendor policy check (200 vs 403/block)
Validates the sticky shape (sessid on :823/:824, sessttl on 10000+).
No secrets are printed — only presence flags + redacted usernames.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.config import (  # noqa: E402
    load_proxy_from_env,
    new_run_id,
    validate_proxy_config,
)

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
    shape = validate_proxy_config(cfg)
    print(f"mode: {shape['mode']} | port: {shape['port']} | "
          f"sticky_ok: {shape['sticky_ok']}")
    for warn in shape["warnings"]:
        print(f"WARN: {warn}")
    run_id = new_run_id()
    for label in ("us", "bd", "jp"):
        try:
            print(f"  sessid user [{label}]: "
                  f"{cfg.redacted_username(run_id, label)[:120]}")
        except Exception as e:  # noqa: BLE001 — template errors surface here
            print(f"  sessid user [{label}]: ERROR {e}")
            return 1
    proxy_url = cfg.httpx_proxy_url(run_id, "us")
    with httpx.Client(proxy=proxy_url, headers=HEADERS,
                      verify=False) as client:  # proxy MITM certs
        probe(client, "neutral-ipify", "https://api.ipify.org")
        probe(client, "geo-ipinfo", "https://ipinfo.io/json")
        probe(client, "google", "https://www.google.com/search?q=egress+check&hl=en&gl=us")
    print("\nSticky note: re-run within 25min with the same sessid should "
          "return the same neutral-ipify IP. A change means DataImpulse "
          "swapped the peer (runner rotates automatically).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
