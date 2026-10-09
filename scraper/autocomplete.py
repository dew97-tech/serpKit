"""Google autocomplete suggestions — plain HTTP, no browser needed.

Endpoint: /complete/search?q=&hl=ja&gl=jp&client=firefox -> JSON array
`[query, [suggestions...]]`. Feeds the article prompt var `suggestions`.
"""
from __future__ import annotations

import json

import httpx

from .config import SearchParams, build_autocomplete_url


def fetch_autocomplete(params: SearchParams, timeout_s: float = 10.0) -> list[str]:
    url = build_autocomplete_url(params)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) "
                      "Gecko/20100101 Firefox/131.0",
        "Accept-Language": f"{params.hl}-{params.gl.upper()},{params.hl};q=0.9",
    }
    resp = httpx.get(url, headers=headers, timeout=timeout_s)
    resp.raise_for_status()
    try:
        data = resp.json()
    except json.JSONDecodeError:
        # Some locales return JSONP-ish padding; strip defensively.
        text = resp.text.strip().lstrip("(").rstrip(");")
        data = json.loads(text)
    if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list):
        return [str(s) for s in data[1][:10]]
    return []
