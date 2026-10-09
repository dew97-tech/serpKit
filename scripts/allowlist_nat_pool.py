"""Allowlist the office NAT pool in a Bright Data zone via the Account API.

Why: the zone dashboard accepted `103.29.124.0/24` but its enforcement does
not match CIDR entries (verified: 407 for .9/.18/.24/... all inside the /24,
while an exact `.30` entry worked). The Account API documents support for
ranges and IP arrays, so this script adds either form:

    set BRIGHTDATA_API_KEY=...          # Admin/Ops role (brightdata.com/cp/setting/users)
    python scripts/allowlist_nat_pool.py range    # one entry: a-b
    python scripts/allowlist_nat_pool.py exact    # 254 individual IPs, one API call

The API key is read from env/.env only and never printed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.config import load_dotenv  # noqa: E402

API_URL = "https://api.brightdata.com/zone/whitelist"
ZONE = os.environ.get("BRIGHTDATA_ALLOWLIST_ZONE", "isp_proxy1")
PREFIX = "103.29.124."
RANGE = f"{PREFIX}1-{PREFIX}254"
EXACT = [f"{PREFIX}{i}" for i in range(1, 255)]


def main() -> int:
    load_dotenv()
    key = os.environ.get("BRIGHTDATA_API_KEY", "")
    if not key:
        print("Missing BRIGHTDATA_API_KEY (Admin/Ops key from "
              "https://brightdata.com/cp/setting/users). Add it to .env.")
        return 1
    mode = (sys.argv[1] if len(sys.argv) > 1 else "range").lower()
    if mode == "range":
        ip: object = RANGE
        desc = RANGE
    elif mode == "exact":
        ip = EXACT
        desc = f"{len(EXACT)} individual IPs {PREFIX}1..254"
    else:
        print(f"Unknown mode '{mode}' — use 'range' or 'exact'.")
        return 1

    print(f"zone: {ZONE} | adding: {desc}")
    resp = httpx.post(
        API_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={"zone": ZONE, "ip": ip},
        timeout=60.0,
    )
    print(f"status: {resp.status_code}")
    print(f"body: {resp.text[:300]}")
    return 0 if resp.status_code in (200, 201, 204) else 1


if __name__ == "__main__":
    raise SystemExit(main())
