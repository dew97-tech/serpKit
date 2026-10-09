"""Google Maps top list, collected in a separate browser pass.

Feeds the maps and MEO flow. Best-effort structural parse with a zero-result
guard: Maps markup drifts faster than search markup. A missing Maps block
never fails the whole scrape. Title, rating, reviews, address, hours and
phone come from the list feed; website and place_id need a place-page pass.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .config import SearchParams, build_init_script, build_maps_search_url, preset_accept_languages
from .serp import block_media_route, browser_user_agent, launch_chromium
from .textutils import parse_count

MAPS_VIEWPORT = {"width": 1366, "height": 900}

MAPS_TITLE_SELECTORS = ["div.fontHeadlineSmall", "div.qBF1Pd", "a h3",
                        "div[role='heading']"]
MAPS_HOURS_RE = re.compile(r"^(Open|Closed|Opens|Closes|Reopens|営業中|営業時間|一時閉店)")


def parse_maps_cards(html: str, limit: int = 10) -> list[dict]:
    """Best-effort extraction from the Maps list feed (`div[role=feed]`)."""
    soup = BeautifulSoup(html, "lxml")
    feed = soup.select_one("div[role='feed']")
    scope = feed or soup
    cards = scope.select("div[role='article']")
    if not cards:
        cards = scope.select("a[href*='/maps/place']")
    results: list[dict] = []
    seen: set[str] = set()
    for card in cards:
        title = ""
        for sel in MAPS_TITLE_SELECTORS:
            node = card.select_one(sel)
            if node and node.get_text(strip=True):
                title = node.get_text(" ", strip=True)
                break
        if not title:
            anchor = card if card.name == "a" else card.select_one("a[aria-label]")
            if anchor:
                title = (anchor.get("aria-label")
                         or anchor.get_text(" ", strip=True)
                         or "").replace(". Opens in new tab.", "").strip()
        if not title or title in seen or len(title) > 120:
            continue
        seen.add(title)
        rating, reviews = None, None
        rated = card.select_one("[aria-label*='stars'], [aria-label^='Rated']")
        if rated:
            label = rated.get("aria-label", "")
            match = re.search(r"([\d.]+)\s*(?:stars|out of 5)", label)
            if match:
                rating = float(match.group(1))
            match = re.search(r"([\d.,KM]+)\s*(?:Reviews|reviews)", label)
            if match:
                reviews = parse_count(match.group(1))
        address, hours, phone = "", "", ""
        candidates: list[str] = []
        for line in card.select("div.W4Efsd"):
            value = line.get_text(" ", strip=True)
            if not value:
                continue
            if not hours and MAPS_HOURS_RE.search(value):
                hours = value[:140]
                phone_match = re.search(r"(\(?\+?\d[\d\s\-\(\)]{6,}\d)", hours)
                if phone_match:
                    phone = phone_match.group(1).strip()
                    hours = (hours[:phone_match.start()]
                             + hours[phone_match.end():])
                hours = re.sub(r"[ \u00b7\u22c5]+$", "", hours).strip()
                continue
            if (re.match(r"^\d+(\.\d+)?\s*\(", value)  # rating row "4.7 (382)"
                    or re.match(r"^\d+\.\d+$", value)
                    or re.match(r"^[\(+]?[\d\s\-\(\)]{7,}$", value)  # phone
                    or "·" in value or "⋅" in value
                    or value in ("Website", "Directions", "Call", "Sponsored")):
                continue
            candidates.append(value)
        for candidate in candidates:
            if "," in candidate or re.search(r"\d", candidate):
                address = candidate
                break
        results.append({
            "position": len(results) + 1,
            "title": title,
            "address": address,
            "rating": rating,
            "reviews": reviews,
            "website": "",
            "hours": hours,
            "phone": phone,
        })
        if len(results) >= limit:
            break
    return results


def _dump_maps_html(html: str, url: str, force: bool = False) -> None:
    target = os.environ.get("SERP_DEBUG_HTML_DIR", "")
    if not target and force:
        target = str(Path(__file__).resolve().parent.parent / "samples" / "html")
    if not target:
        return
    try:
        out_dir = Path(target)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (out_dir / f"maps-{stamp}.html").write_text(html, encoding="utf-8")
        (out_dir / f"maps-{stamp}.url.txt").write_text(url, encoding="utf-8")
    except Exception:
        pass


def collect_maps(
    params: SearchParams,
    limit: int = 10,
    proxy: dict | None = None,
    headless: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Returns (results, errors). Never raises — Maps is best-effort."""
    errors: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = launch_chromium(p, headless, proxy=proxy)
            try:
                context_kwargs: dict = {
                    "viewport": MAPS_VIEWPORT,
                    "locale": f"{params.hl}-{params.gl.upper()}",
                    "ignore_https_errors": bool(proxy),  # proxy MITM certs
                }
                ua = browser_user_agent(browser)
                if ua:
                    context_kwargs["user_agent"] = ua
                context = browser.new_context(**context_kwargs)
                context.add_init_script(
                    build_init_script(preset_accept_languages(params))
                )
                context.route("**/*", block_media_route)
                page = context.new_page()
                try:
                    page.goto(build_maps_search_url(params),
                              wait_until="domcontentloaded", timeout=30_000)
                    page.wait_for_timeout(4000)  # Maps hydrates async
                    html = page.content()
                    _dump_maps_html(html, page.url)
                finally:
                    page.close()
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 — best-effort surface
        return [], [{"code": "maps_error", "message": str(e)[:300]}]

    results = parse_maps_cards(html, limit=limit)
    if not results:
        errors.append({"code": "maps_zero_result",
                       "message": "Maps parse returned 0 entries — markup may have drifted."})
    elif sum(1 for r in results if not r.get("address")) >= max(1, len(results) // 2):
        # Address extraction looks weak — capture evidence for selector work.
        _dump_maps_html(html, "", force=True)
    return results, errors
