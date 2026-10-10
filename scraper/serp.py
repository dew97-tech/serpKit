"""Playwright collector for Google search pages.

Waits for a specific container instead of network idle, reuses one browser
context across pagination, and extracts data with structure-based selectors.
Challenge pages are classified and cleared through CapSolver when configured:
Google /sorry/ serves reCAPTCHA v2 Enterprise, so the token is solved
out-of-band and injected into the same context (docs/CAPSOLVER.md).

Only this file and maps.py own browser calls. Everything else consumes the
frozen schemas.py contract.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Page, sync_playwright

from .capsolver import CapSolverClient, CapSolverError
from .config import (
    PAA_CLICK_DELAY_MAX_MS,
    PAA_CLICK_DELAY_MIN_MS,
    SearchParams,
    build_init_script,
    build_search_url,
    jitter_delay_seconds,
    preset_accept_languages,
)
from .textutils import parse_count

_SELECTORS = json.loads((Path(__file__).parent / "selectors.json").read_text("utf-8"))

DESKTOP_VIEWPORT = {"width": 1366, "height": 900}

SOLVABLE_KINDS = {"captcha_v2", "captcha_image"}


def emit_progress(on_progress, stage: str, state: str, detail: str = "") -> None:
    """Best-effort progress event for the viewer; never affects scraping."""
    if on_progress is None:
        return
    try:
        on_progress({"type": "stage", "stage": stage, "state": state,
                     "detail": detail})
    except Exception:
        pass

# Bandwidth saver: abort heavy assets (serp HTML stays parseable; proxy GB drops).
_BLOCKED_RESOURCE_TYPES = {"image", "font", "media"}

AI_PANEL_MARKERS = ("AI Mode", "AI Overview", "AI による概要", "AI モード")
PAA_ERROR_MARKERS = ("An error has occurred", "エラーが発生しました")
# Google's AI panel sometimes embeds translation-failure placeholders.
AI_TRANSLATION_MARKERS = ("Error translating content", "翻訳エラー",
                          "翻訳できません", "コンテンツを翻訳")
# AI-Mode filter chips leak into weak PAA selectors ("More", "Tools", ...).
PAA_JUNK_LABELS = {"more", "tools", "any time", "all results", "all", "images",
                   "videos", "news", "shopping", "forums", "web", "maps",
                   "short videos", "more results"}


def browser_user_agent(browser) -> str:
    """Engine's real UA with the headless token removed.

    New-headless Chrome appends "HeadlessChrome"; stripping only that token
    keeps the UA consistent with Sec-CH-UA / userAgentData, which still
    report the true Chrome version (verified 154 locally).
    """
    try:
        page = browser.new_page()
        try:
            ua = page.evaluate("navigator.userAgent") or ""
        finally:
            page.close()
        return ua.replace("HeadlessChrome", "Chrome")
    except Exception:
        return ""


def launch_chromium(playwright, headless: bool, proxy: dict | None = None):
    """Launch the most convincing Chromium available.

    Prefers system Chrome (`channel="chrome"`: real UA + traits) and falls
    back to Playwright's bundled Chromium. New headless is requested via the
    explicit `--headless=new` flag: Playwright's own headless option uses the
    old mode whose UA leaks "HeadlessChrome". `--enable-automation` is
    removed; HTTP/1.1 only (HTTP/2 stalls through the residential proxy —
    verified 0/6 vs 6/6).
    """
    args = ["--disable-http2", "--disable-blink-features=AutomationControlled"]
    if headless:
        args.append("--headless=new")
    base: dict = {
        # Playwright adds its own old `--headless` when this is True — running
        # "headed" plus the explicit flag keeps the real Chrome UA.
        "headless": False,
        "args": args,
        "ignore_default_args": ["--enable-automation"],
    }
    if proxy:
        base["proxy"] = proxy
    channels = [os.environ.get("BROWSER_CHANNEL", "").strip() or "chrome", "chromium"]
    seen: list[str] = []
    last_error: Exception | None = None
    for channel in channels:
        if channel in seen:
            continue
        seen.append(channel)
        try:
            return playwright.chromium.launch(channel=channel, **base)
        except Exception as e:  # noqa: BLE001 — fall back to bundled Chromium
            last_error = e
    raise last_error if last_error else RuntimeError("no chromium channel available")


class BlockDetected(Exception):
    """Raised when Google serves a challenge/consent wall instead of results."""

    def __init__(self, info: dict):
        self.info = info
        super().__init__(f"{info.get('kind')}: {info.get('url', '')}")


class RunDeadlineExceeded(Exception):
    """Raised when the per-run wall-clock deadline (RUN_TIMEOUT_S) is hit."""


# ---------------------------------------------------------------------------
# Block classification — structured, fail-loud
# ---------------------------------------------------------------------------


def classify_block(page: Page, html: str) -> dict | None:
    """Identify challenge/consent/soft-block pages with solver-relevant detail.

    Returns None when the page looks like a normal SERP. Kinds:
      captcha_v2 | captcha_image | captcha_unknown | consent |
      js_required | unusual_traffic
    """
    url = page.url
    try:
        if "consent.google.com" in (urlparse(url).hostname or ""):
            return {"kind": "consent", "url": url}
        if "/sorry/" in url or page.locator("form#captcha-form").count():
            info = {
                "kind": "captcha_unknown",
                "url": url,
                "sitekey": "",
                "data_s": "",
                "enterprise": "recaptcha/enterprise" in html,
                "image": False,
            }
            # reCAPTCHA v2 widget (normal or enterprise)
            div = re.search(r'<div[^>]*class="g-recaptcha"[^>]*>', html)
            if div:
                tag = div.group(0)
                m = re.search(r'data-sitekey="([^"]+)"', tag)
                if m:
                    info["sitekey"] = m.group(1)
                m = re.search(r'data-s="([^"]+)"', tag)
                if m:
                    info["data_s"] = m.group(1)
            if not info["sitekey"]:
                m = re.search(r'data-sitekey="([^"]+)"', html)
                info["sitekey"] = m.group(1) if m else ""
            if info["sitekey"]:
                info["kind"] = "captcha_v2"
            # Classic text-image captcha (fallback variant)
            if (page.locator("input#captcha, img#captcha-img").count()
                    or re.search(r'src="[^"]*sorry/image', html)):
                info["kind"] = "captcha_image"
                info["image"] = True
            return info
        markers = _SELECTORS["block_markers"]
        for needle in markers["js_required"]:
            if needle in html:
                return {"kind": "js_required", "url": url, "detail": needle}
        for needle in markers["unusual_traffic"]:
            if needle in html:
                return {"kind": "unusual_traffic", "url": url, "detail": needle}
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Solve policy — budget, circuit breaker, telemetry (no secrets logged)
# ---------------------------------------------------------------------------


def task_type_for(block: dict, proxy_string: str) -> str:
    enterprise = bool(block.get("enterprise"))
    if enterprise:
        return "ReCaptchaV2EnterpriseTask" if proxy_string \
            else "ReCaptchaV2EnterpriseTaskProxyLess"
    return "ReCaptchaV2Task" if proxy_string else "ReCaptchaV2TaskProxyLess"


class SolvePolicy:
    """Decides when to spend a solve; keeps the run honest when it can't."""

    def __init__(
        self,
        client: CapSolverClient | None = None,
        max_solves: int = 2,
        min_interval_s: float = 5.0,
        proxy_string: str = "",
        requested: bool = True,
    ):
        self.client = client
        self.max_solves = max_solves
        self.min_interval_s = min_interval_s
        self.proxy_string = proxy_string
        self.requested = requested
        self.solves: list[dict] = []
        self.fatal = False
        self._circuit_until = 0.0

    @property
    def configured(self) -> bool:
        return self.client is not None and not self.fatal

    def can_solve(self, block: dict) -> tuple[bool, str]:
        if not self.requested:
            return False, "solving disabled for this run (solve=0)"
        if self.client is None:
            return False, "solver_not_configured (set CAPSOLVER_API_KEY)"
        if self.fatal:
            return False, "solver_fatal"
        if block.get("kind") not in SOLVABLE_KINDS:
            return False, f"unsolvable_kind:{block.get('kind')}"
        if len(self.solves) >= self.max_solves:
            return False, "solve_budget_exhausted"
        if time.time() < self._circuit_until:
            return False, "solver_circuit_open"
        if self.solves and time.time() - self.solves[-1]["_ts"] < self.min_interval_s:
            return False, "solve_throttled"
        return True, ""

    # -- browser-side solve ---------------------------------------------------

    def solve(self, page: Page, block: dict) -> bool:
        """Solve the challenge on `page` in-place. Returns True when cleared."""
        can, why = self.can_solve(block)
        if not can:
            self._record(block, outcome="skipped", error=why, ms=0)
            return False

        task_type = task_type_for(block, self.proxy_string)
        t0 = time.time()
        task_id, error = "", ""
        try:
            if block["kind"] == "captcha_image":
                task_type = "ImageToTextTask"
                img = page.locator("img#captcha-img, img[src*='sorry/image']").first
                text = self.client.solve_image(img.screenshot())
                page.locator("input#captcha, input[name='captcha']").first.fill(text)
                page.locator("input[type=submit], button[type=submit]").first.click()
                outcome = self._await_clear(page)
            else:
                solution = self.client.solve_recaptcha_v2(
                    sitekey=block["sitekey"],
                    page_url=block["url"] or page.url,
                    data_s=block.get("data_s", ""),
                    enterprise=bool(block.get("enterprise")),
                    proxy=self.proxy_string,
                    user_agent=self._page_user_agent(page),
                )
                task_id = str(solution.get("_taskId", ""))
                token = solution.get("gRecaptchaResponse", "")
                if not token:
                    raise CapSolverError("ERROR_CAPTCHA_UNSOLVABLE",
                                         "no gRecaptchaResponse in solution")
                self._apply_solution_cookies(page, solution)
                injected = page.evaluate(
                    """(t) => {
                        const ta = document.getElementById('g-recaptcha-response');
                        if (ta) { ta.value = t; }
                        const f = document.getElementById('captcha-form');
                        if (f) { f.submit(); return true; }
                        return false;
                    }""",
                    token,
                )
                outcome = self._await_clear(page) if injected else "rejected"
            self._record(block, outcome=outcome, error=error,
                         ms=int((time.time() - t0) * 1000), task_id=task_id,
                         task_type=task_type)
            return outcome == "solved"
        except CapSolverError as e:
            if e.fatal:
                self.fatal = True
            if e.circuit:
                from .capsolver import CIRCUIT_COOLDOWN_S

                self._circuit_until = time.time() + CIRCUIT_COOLDOWN_S
            self._record(block, outcome="error", error=e.code,
                         ms=int((time.time() - t0) * 1000), task_id=task_id,
                         task_type=task_type)
            return False
        except Exception as e:  # noqa: BLE001 — solver/network failure
            # Keep the run honest: record, then let the caller raise
            # BlockDetected with the challenge detail (not a generic error).
            self._record(block, outcome="error", error=str(e)[:200],
                         ms=int((time.time() - t0) * 1000), task_id=task_id,
                         task_type=task_type)
            return False

    @staticmethod
    def _await_clear(page: Page, timeout_ms: int = 25_000) -> str:
        try:
            page.wait_for_selector("#search, form#captcha-form", timeout=timeout_ms)
        except Exception:
            pass
        page.wait_for_timeout(1_000)
        if page.locator("#search").count() and "/sorry/" not in page.url:
            return "solved"
        return "rejected"

    @staticmethod
    def _page_user_agent(page: Page) -> str:
        try:
            return page.evaluate("navigator.userAgent") or ""
        except Exception:
            return ""

    @staticmethod
    def _apply_solution_cookies(page: Page, solution: dict) -> None:
        """Set recaptcha-ca-t/-e session cookies returned by the solver —
        Google /sorry/ validates them alongside the token (CapSolver docs)."""
        cookies = []
        for name in ("recaptcha-ca-t", "recaptcha-ca-e"):
            if solution.get(name):
                cookies.append({
                    "name": name, "value": str(solution[name]),
                    "domain": ".google.com", "path": "/",
                })
        if cookies:
            try:
                page.context.add_cookies(cookies)
            except Exception:
                pass

    def _record(self, block: dict, outcome: str, error: str, ms: int,
                task_id: str = "", task_type: str = "") -> None:
        self.solves.append({
            "kind": block.get("kind", ""),
            "task_type": task_type,
            "task_id": task_id,
            "ms": ms,
            "outcome": outcome,
            "error": error,
            "_ts": time.time(),
        })

    def public_log(self) -> list[dict]:
        return [{k: v for k, v in s.items() if not k.startswith("_")}
                for s in self.solves]


# ---------------------------------------------------------------------------
# Parsers (unchanged: organic / related / PAA / AI / local / pagination)
# ---------------------------------------------------------------------------


def clean_result_url(href: str) -> str:
    """Unwrap Google's /url?q= redirect to the advertiser/organic target."""
    if not href:
        return ""
    parsed = urlparse(href)
    if parsed.path == "/url":
        target = parse_qs(parsed.query).get("q", [""])[0]
        return target or href
    return href


def cite_to_url(cite: str) -> str:
    """Best-effort URL from Google's display cite (`https://host › path › …`)."""
    if not cite:
        return ""
    text = cite.replace(" › ", "/").replace("›", "/").replace(" ", "")
    text = text.rstrip(".")
    return text if text.startswith("http") else ""


def resolve_goto_link(context: BrowserContext, google_domain: str, href: str) -> str:
    """Resolve Google's wrapped `/goto?url=` links (seen 2026-10: the value is
    an opaque blob, no decodable URL) via a redirect request that shares the
    browser context's cookies and exit IP. Returns "" when unresolvable."""
    if not href or not href.startswith("/"):
        return ""
    try:
        resp = context.request.get(
            f"https://{google_domain}{href}", max_redirects=0, timeout=10_000
        )
        location = resp.headers.get("location", "")
        return location if location.startswith("http") else ""
    except Exception:
        return ""


def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el and el.get_text(strip=True):
            return el.get_text(" ", strip=True)
    return ""


def _find_result_card(anchor) -> object | None:
    """Walk ancestors to the result card (cite + snippet container).

    Google dropped `div.g` (verified 2026-10); the card is now an anonymous
    div (`data-hveid`) with the anchor nested a couple of levels deep.
    """
    node = anchor
    fallback = None
    for _ in range(8):
        node = node.parent
        if node is None or getattr(node, "name", None) != "div":
            continue
        has_cite = node.select_one("cite") is not None
        has_snip = node.select_one(".VwiC3b, [data-sncf]") is not None
        if has_cite and has_snip:
            return node
        if fallback is None and (has_cite or has_snip):
            fallback = node
    return fallback


def parse_organic(html: str, base_position: int = 0, link_resolver=None) -> list[dict]:
    """Extract organic results anchored on `a:has(h3)` (ads live outside #search).

    Google now serves opaque `/goto?url=` hrefs (verified 2026-10); when a
    `link_resolver` is provided it is called with the raw href and should
    return the real URL. Falls back to the card's `<cite>` display URL.
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    results: list[dict] = []
    anchors = []
    for sel in _SELECTORS["organic_link"]:
        try:
            anchors = soup.select(sel)
        except Exception:
            continue
        if anchors:
            break
    for a in anchors:
        title_el = a.select_one("h3")
        if not title_el:
            continue
        title = title_el.get_text(" ", strip=True)
        raw_href = a.get("href", "")
        link = clean_result_url(raw_href)
        snippet, cite = "", ""
        card = _find_result_card(a)
        if card is not None:
            for sel in _SELECTORS["snippet"]:
                try:
                    s = card.select_one(sel)
                except Exception:
                    s = None
                if s and s.get_text(strip=True):
                    snippet = s.get_text(" ", strip=True)
                    break
            cite_el = card.select_one("cite")
            if cite_el:
                cite = cite_el.get_text(" ", strip=True)
        if not link.startswith("http") and link_resolver is not None:
            link = link_resolver(raw_href) or link
        if not link.startswith("http") and cite:
            link = cite_to_url(cite)
        if not link.startswith("http") or link in seen:
            continue
        seen.add(link)
        results.append(
            {
                "position": base_position + len(results) + 1,
                "title": title,
                "link": link,
                "snippet": snippet,
            }
        )
    return results


def _search_chip_text(anchor) -> str:
    text = anchor.get_text(" ", strip=True)
    href = anchor.get("href", "")
    if not text or not href or "/search" not in href:
        return ""
    if "start=" in href:
        return ""
    if re.search(r"[?&](udm|tbm)=(?!14)", href):
        return ""
    if len(text) < 3 or len(text) > 120:
        return ""
    if re.fullmatch(r"[\d\s,.、]+", text):
        return ""
    if text.lower() in {"next", "previous", "次へ", "前へ"}:
        return ""
    if text.lower() in PAA_JUNK_LABELS:
        return ""
    return text


def parse_related_searches(html: str) -> list[str]:
    """Bottom related-search chips (pagination and nav labels filtered out)."""
    soup = BeautifulSoup(html, "lxml")
    for sel in _SELECTORS["related_searches_link"]:
        try:
            anchors = soup.select(sel)
        except Exception:
            continue
        candidates: list[str] = []
        for a in anchors:
            text = _search_chip_text(a)
            if text and text not in candidates:
                candidates.append(text)
        if candidates:
            return candidates[:8]
    return []


def parse_people_also_search_for(html: str) -> list[str]:
    """Mid-page "People also search for" chips (verified 2026-10 layouts)."""
    soup = BeautifulSoup(html, "lxml")
    headings = _SELECTORS.get("people_also_search_for", {}).get("heading", [])
    node = None
    for needle in headings:
        node = soup.find(string=lambda s, n=needle: s and n in s)
        if node is not None:
            break
    if node is None:
        return []
    container = node.parent
    for _ in range(5):
        if container is None:
            return []
        if getattr(container, "name", None) == "div":
            names: list[str] = []
            for a in container.select("a[href*='/search']"):
                text = _search_chip_text(a)
                if text and text not in names:
                    names.append(text)
            if 1 <= len(names) <= 12:
                return names
        container = container.parent
    return []


def _merged_chips(*lists) -> list[str]:
    merged: list[str] = []
    for items in lists:
        for item in items:
            if item not in merged:
                merged.append(item)
    return merged[:8]


def detect_module_heads(html: str) -> dict:
    return {
        "paa": any(m in html for m in ("People also ask", "関連する質問",
                                       "他の人はこちらも質問")),
        "pasf": any(m in html for m in ("People also search for",
                                        "他の人はこちらも検索")),
        "related": any(m in html for m in ("Related searches", "関連する検索")),
    }


def parse_paa(html: str) -> list[dict]:
    """Questions (+ answers when the pre-parse click expansion succeeded).

    Google sometimes answers with an inline "An error has occurred" note;
    those stay snippet-less rather than returning junk.
    """
    soup = BeautifulSoup(html, "lxml")
    pairs = soup.select("div.related-question-pair")
    if not pairs:
        for sel in _SELECTORS["paa_question"]:
            try:
                pairs = soup.select(sel)
            except Exception:
                continue
            if pairs:
                break
    questions: list[dict] = []
    seen: set[str] = set()
    for pair in pairs[:8]:
        question = (pair.get("data-q") or "").strip()
        if not question:
            heading = pair.select_one("[role=heading]")
            question = (heading.get_text(" ", strip=True) if heading
                        else pair.get_text(" ", strip=True).split("\n")[0].strip())
        if not question or question in seen or len(question) > 200:
            continue
        if question.lower() in PAA_JUNK_LABELS or len(question) < 4:
            continue
        seen.add(question)
        snippet, link = "", ""
        body = None
        for sel in _SELECTORS.get("paa_answer", []):
            body = pair.select_one(sel)
            if body is not None:
                break
        if body is None:
            body = pair
        text = body.get_text(" ", strip=True)
        if text.startswith(question):
            text = text[len(question):].strip()
        if (len(text) > 20
                and not any(marker in text for marker in PAA_ERROR_MARKERS)):
            snippet = text[:600]
            anchor = body.select_one("a[href^='http']")
            if anchor:
                link = anchor.get("href", "")
        questions.append({"question": question, "snippet": snippet, "link": link})
    return questions


def _ai_text_blocks(container, cap: int = 6, max_len: int = 2000) -> list[str]:
    """Content paragraphs from an AI panel (classic overview or AI Mode).

    Keeps the innermost divs with meaningful text so nested wrappers don't
    duplicate their children.
    """
    candidates: list[tuple[object, str]] = []
    for node in container.find_all("div"):
        text = node.get_text(" ", strip=True)
        if 40 <= len(text) <= max_len:
            if any(marker in text for marker in AI_TRANSLATION_MARKERS):
                continue  # translation-failure placeholder, not content
            candidates.append((node, text))
    blocks: list[str] = []
    for node, text in candidates:
        if any(other is not node and node in other.parents
               for other, _ in candidates):
            continue  # a more specific descendant covers this node
        blocks.append(text[:max_len])
        if len(blocks) >= cap:
            break
    if not blocks:
        text = container.get_text(" ", strip=True)
        if len(text) > 40:
            blocks = [text[:max_len]]
    return blocks


def _ai_external_links(container, cap: int = 10) -> list[dict]:
    links: list[dict] = []
    seen: set[str] = set()
    if container is None:
        return links
    for anchor in container.select("a[href^='http']"):
        href = anchor.get("href", "")
        host = urlparse(href).hostname or ""
        if not host or "google." in host or href in seen:
            continue
        seen.add(href)
        title = (anchor.get_text(" ", strip=True)
                 or (anchor.get("aria-label") or "").replace(". Opens in new tab.", ""))
        links.append({"title": title[:120], "link": href})
        if len(links) >= cap:
            break
    return links


def parse_ai_overview(html: str) -> dict | None:
    """AI Overview or AI Mode reply; None when absent.

    Classic overviews win when both exist; AI Mode replies carry
    `source: ai_mode` (their links are Google-internal, so links stays empty
    in practice). Contract rule stands: non-empty text_blocks or None.
    """
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    if any(marker in text for marker in ("AI Overview", "AI による概要")):
        candidates = []
        for sel in _SELECTORS["ai_overview"]:
            try:
                nodes = soup.select(sel)
            except Exception:
                continue
            for node in nodes:
                node_text = node.get_text(" ", strip=True)
                if len(node_text) >= 200:
                    candidates.append((len(node_text), node))
            if candidates:
                break
        if candidates:
            candidates.sort(key=lambda item: item[0])
            node = candidates[0][1]
            blocks = _ai_text_blocks(node)
            if blocks:
                return {"text_blocks": blocks[:4], "links": _ai_external_links(node)}

    if any(marker in text for marker in ("AI Mode", "AI モード")):
        heading = soup.find(
            lambda tag: tag.name in ("h1", "h2", "h3")
            and "AI Mode" in tag.get_text()
        )
        node = heading
        for _ in range(6):
            node = node.parent if node is not None else None
            if node is None:
                break
            if len(node.get_text(" ", strip=True)) > 300:
                blocks = _ai_text_blocks(node, cap=12)
                if blocks:
                    return {"text_blocks": blocks[:10],
                            "links": _ai_external_links(node),
                            "source": "ai_mode"}
                break
    return None


def parse_local_pack(html: str) -> tuple[dict | None, list[dict]]:
    """Local pack from `div.VkpGBb` cards (verified 2026-10).

    Cards carry name, rating/reviews (aria-label) and address; phone/website/
    place_id are not present in card markup and stay empty.
    """
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("div.VkpGBb")
    if not cards:
        text = soup.get_text(" ", strip=True)
        present = "Map" in text or "マップ" in text or "営業時間" in text
        return ({"present": True} if present else None), []
    results: list[dict] = []
    for card in cards[:10]:
        heading = (card.select_one("[role=heading] .OSrXXb")
                   or card.select_one("[role=heading]"))
        title = heading.get_text(" ", strip=True) if heading else ""
        if not title:
            continue
        rating, reviews = None, None
        rated = card.select_one("span[aria-label^='Rated']")
        if rated:
            match = re.search(r"Rated ([\d.]+) out of 5(?:, ([^ ]+) user)?",
                              rated.get("aria-label", ""))
            if match:
                rating = float(match.group(1))
                reviews = parse_count(match.group(2))
        address = ""
        details = card.select_one(".rllt__details")
        if details:
            for line in details.find_all("div", recursive=False):
                classes = line.get("class") or []
                if (line.get("role") == "heading"
                        or line.select_one("[role=heading]")
                        or "pJ3Ci" in classes
                        or line.select_one("span[aria-label^='Rated']")):
                    continue
                value = line.get_text(" ", strip=True)
                if not value or "·" in value or value in ("Sponsored", "スポンサー"):
                    continue
                if re.match(r"^\d+\+?\s*years?\b", value, re.I):
                    continue
                if re.match(r"^(Open|Closed|Opens|Closes)\b", value):
                    continue
                address = value
                break
        results.append({
            "position": len(results) + 1,
            "title": title,
            "address": address,
            "phone": "",
            "rating": rating,
            "reviews": reviews,
            "website": "",
            "place_id": "",
        })
    return {"present": True, "count": len(results)}, results


def parse_has_next(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    for sel in _SELECTORS["has_next"]:
        try:
            if soup.select_one(sel):
                return True
        except Exception:
            continue
    return False


def _click_paa(page: Page, max_clicks: int) -> None:
    """Prefer real PAA pairs; fall back to selector-based clicking."""
    try:
        pairs = page.locator("div.related-question-pair")
        pair_count = min(pairs.count(), max_clicks)
    except Exception:
        pair_count = 0
    if pair_count:
        for i in range(pair_count):
            try:
                pair = pairs.nth(i)
                target = pair.locator("[role=button]").first
                if target.count():
                    target.click(timeout=2000)
                else:
                    pair.click(timeout=2000)
                page.wait_for_timeout(
                    random.randint(PAA_CLICK_DELAY_MIN_MS, PAA_CLICK_DELAY_MAX_MS)
                )
            except Exception:
                continue
        return

    clicked = 0
    for sel in _SELECTORS["paa_question"]:
        try:
            nodes = page.locator(sel)
            count = nodes.count()
        except Exception:
            continue
        if not count:
            continue
        for i in range(min(count, max_clicks - clicked)):
            try:
                nodes.nth(i).click(timeout=2000)
                page.wait_for_timeout(
                    random.randint(PAA_CLICK_DELAY_MIN_MS, PAA_CLICK_DELAY_MAX_MS)
                )
                clicked += 1
            except Exception:
                continue
        break


def _await_paa_answers(page: Page, timeout_ms: int = 8000) -> None:
    """Wait out the PAA answer async fetch ("Generating" spinner)."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            if not page.locator("[aria-label='Generating']").count():
                break
        except Exception:
            break
        page.wait_for_timeout(500)


def expand_paa(page: Page, max_clicks: int = 6) -> None:
    """Click PAA accordions so answers inject into the DOM before capture.

    Waits briefly for hydration, waits out the async answer fetch (the block
    starts as a "Generating" placeholder), then retries pairs that Google
    answered with its inline "An error has occurred" note.
    """
    page.wait_for_timeout(1000)  # let the answer RPC settle before clicking
    _click_paa(page, max_clicks)
    _await_paa_answers(page)
    try:
        pairs = page.locator("div.related-question-pair")
        count = min(pairs.count(), max_clicks)
    except Exception:
        return
    retry_needed = False
    for i in range(count):
        try:
            if any(marker in pairs.nth(i).inner_text(timeout=800)
                   for marker in PAA_ERROR_MARKERS):
                retry_needed = True
                break
        except Exception:
            continue
    if not retry_needed:
        return
    page.wait_for_timeout(1500)
    for i in range(count):
        try:
            pair = pairs.nth(i)
            if not any(marker in pair.inner_text(timeout=800)
                       for marker in PAA_ERROR_MARKERS):
                continue
            target = pair.locator("[role=button]").first
            if target.count():
                target.click(timeout=2000)
            else:
                pair.click(timeout=2000)
            page.wait_for_timeout(
                random.randint(PAA_CLICK_DELAY_MIN_MS, PAA_CLICK_DELAY_MAX_MS)
            )
        except Exception:
            continue
    _await_paa_answers(page)


def wait_for_ai_panel(page: Page, timeout_ms: int = 6000) -> None:
    """Bounded wait for late-rendered AI panels + a gentle scroll nudge."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        found = False
        for sel in ("div.FkX2oe", "text=AI Overview", "text=AI Mode",
                    "text=AI による概要"):
            try:
                if page.locator(sel).count():
                    found = True
                    break
            except Exception:
                continue
        if found:
            break
        page.wait_for_timeout(400)
    try:
        page.evaluate("window.scrollTo(0, Math.min(900, document.body.scrollHeight))")
        page.wait_for_timeout(350)
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


def expand_ai_overview(page: Page) -> None:
    for label in _SELECTORS["ai_show_more"]:
        try:
            btn = page.get_by_text(label, exact=False).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=2000)
                page.wait_for_timeout(1500)
                return
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Fetch + collect
# ---------------------------------------------------------------------------

_QUERY_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "best", "top", "new",
    "near", "me", "how", "you", "your", "what", "when", "where", "which",
    "who", "are", "was", "were", "does", "do", "can", "will", "in", "on",
    "at", "of", "to", "a", "an", "is", "it", "my",
}


def organic_off_query(query: str, organic: list[dict]) -> bool:
    """True when the organic block ignores the query.

    Google's AI-Mode layout sometimes serves a "Web results" section for the
    wrong interpretation (verified 2026-10: `best vpn` returned 'BEST' brand
    pages, `Best VPN services` returned 'services' pages). Every meaningful
    token from the query must appear somewhere in titles/snippets.
    """
    tokens = {t for t in re.findall(r"[a-z0-9]+", (query or "").lower())
              if len(t) >= 3 and t not in _QUERY_STOPWORDS}
    if not tokens or not organic:
        return False
    haystack = " ".join(
        str(o.get("title", "")) + " " + str(o.get("snippet", ""))
        for o in organic
    ).lower()
    return any(token not in haystack for token in tokens)


def _merge_organic(items: list[dict], page: int, seen: set,
                   out: list[dict]) -> int:
    added = 0
    for item in items:
        link = str(item.get("link", ""))
        if not link or link in seen:
            continue
        seen.add(link)
        item["page"] = page
        out.append(item)
        added += 1
    return added


def _collect_web_fallback(
    context: BrowserContext,
    web_params: SearchParams,
    pages: int,
    policy: SolvePolicy | None,
    resolver,
    on_progress=None,
    deadline: float | None = None,
) -> tuple[list[dict], list[str], list[dict], bool, int]:
    """Classic Web-mode (`udm=14`) refetch for off-query AI-Mode pages."""
    organic: list[dict] = []
    related: list[str] = []
    questions: list[dict] = []
    seen: set[str] = set()
    has_next = False
    fetched = 0
    for i in range(pages):
        html, _ = fetch_search_page(context, web_params, i * 10, policy,
                                    on_progress=on_progress, deadline=deadline)
        fetched += 1
        _merge_organic(parse_organic(html, link_resolver=resolver),
                       i + 1, seen, organic)
        if i == 0:
            related = parse_related_searches(html)
            questions = parse_paa(html)
        has_next = parse_has_next(html)
        if i < pages - 1:
            time.sleep(jitter_delay_seconds())
    return organic, related, questions, has_next, fetched


def fetch_search_page(
    context: BrowserContext,
    params: SearchParams,
    start: int,
    policy: SolvePolicy | None = None,
    warmup: bool = False,
    on_progress=None,
    deadline: float | None = None,
) -> tuple[str, str]:
    """Fetch one `/search` page; solves /sorry/ in-place when policy allows.

    `warmup` (new sessions only) visits the Google homepage first so the
    context arrives with cookies and a natural referer chain.

    Returns (rendered_html, final_url). Raises BlockDetected when the page
    is still a challenge/consent/soft-block after the solve attempt.
    """
    page = context.new_page()
    try:
        if deadline and time.time() > deadline:
            raise RunDeadlineExceeded()
        homepage = f"https://{params.google_domain}/"
        referer = None
        if warmup and start == 0:
            try:
                page.goto(homepage, wait_until="domcontentloaded", timeout=20_000)
                page.wait_for_timeout(random.randint(700, 1400))
                referer = homepage
            except Exception:
                referer = None
        goto_kwargs: dict = {"wait_until": "domcontentloaded", "timeout": 30_000}
        if referer:
            goto_kwargs["referer"] = referer
        page.goto(build_search_url(params, start), **goto_kwargs)
        try:
            page.wait_for_selector("#search, #rso, #botstuff, form#captcha-form",
                                   timeout=15_000)
        except Exception:
            pass

        block = classify_block(page, page.content())
        if block and block["kind"] in SOLVABLE_KINDS and policy is not None:
            if not policy.configured:
                emit_progress(on_progress, "note", "note",
                              "CAPTCHA detected, solver unavailable")
            attempts = 0
            while attempts < policy.max_solves:
                if deadline and time.time() > deadline:
                    raise RunDeadlineExceeded()
                if attempts:
                    # Reload for a FRESH Enterprise challenge instead of
                    # solving an escalated image captcha (weak OCR path).
                    time.sleep(policy.min_interval_s)
                    try:
                        page.goto(build_search_url(params, start),
                                  wait_until="domcontentloaded", timeout=30_000)
                        try:
                            page.wait_for_selector(
                                "#search, #rso, #botstuff, form#captcha-form",
                                timeout=15_000)
                        except Exception:
                            pass
                        block = classify_block(page, page.content())
                    except Exception:
                        break
                    if not block or block["kind"] not in SOLVABLE_KINDS:
                        break
                emit_progress(on_progress, "note", "note",
                              f"Solving CAPTCHA (attempt {attempts + 1} "
                              f"of {policy.max_solves})")
                if policy.solve(page, block):
                    emit_progress(on_progress, "note", "note",
                                  f"CAPTCHA cleared (attempt {attempts + 1})")
                    try:
                        page.wait_for_selector("#search", timeout=20_000)
                    except Exception:
                        pass
                attempts += 1
                block = classify_block(page, page.content())
                if not block:
                    break
                if not policy.configured:
                    break

        if block:
            raise BlockDetected(block)

        if start == 0:
            emit_progress(on_progress, "ai", "running")
            wait_for_ai_panel(page)
            expand_ai_overview(page)
            emit_progress(on_progress, "paa", "running")
            expand_paa(page)

        html = page.content()
        final_block = classify_block(page, html)
        if final_block:
            raise BlockDetected(final_block)
        _dump_html(html, page.url, start)
        return html, page.url
    finally:
        page.close()


def _dump_html(html: str, url: str, start: int, force: bool = False) -> None:
    """Save rendered HTML for selector debugging.

    Target dir = SERP_DEBUG_HTML_DIR when set; `force=True` (the zero-result
    drift alarm) falls back to samples/html so UI runs capture evidence
    without any env configuration.
    """
    import os

    target = os.environ.get("SERP_DEBUG_HTML_DIR", "")
    if not target and force:
        target = str(Path(__file__).resolve().parent.parent / "samples" / "html")
    if not target:
        return
    try:
        out_dir = Path(target)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = f"{stamp}-start{start}"
        (out_dir / f"{base}.html").write_text(html, encoding="utf-8")
        (out_dir / f"{base}.url.txt").write_text(url, encoding="utf-8")
    except Exception:
        pass


def block_media_route(route) -> None:
    """Abort heavy assets to cut proxy bandwidth (HTML/JS still load)."""
    try:
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            route.abort()
        else:
            route.continue_()
    except Exception:
        try:
            route.continue_()
        except Exception:
            pass


def collect_search(
    params: SearchParams,
    pages: int = 3,
    proxy: dict | None = None,
    headless: bool = True,
    policy: SolvePolicy | None = None,
    storage_state: dict | None = None,
    warmup: bool = False,
    on_progress=None,
    deadline: float | None = None,
    ip_check=None,
    expected_exit_ip: str = "",
) -> dict:
    """Collect pages 1..N in ONE context (session consistency). Partial contract.

    `storage_state` carries the session's persisted cookies in; the updated
    state (incl. Google clearance cookies) is returned under `storage_state`.
    """
    started = time.time()
    organic: list[dict] = []
    related_searches: list[str] = []
    related_questions: list[dict] = []
    ai_overview: dict | None = None
    local_map, local_results = None, []
    has_next = False
    errors: list[dict] = []
    pages_fetched = 0
    web_fallback_used = False
    midrun_ip_changed = False
    module_heads: dict = {}

    with sync_playwright() as p:
        browser = None
        context = None
        state_out: dict | None = None
        user_agent_out = ""
        try:
            browser = launch_chromium(p, headless, proxy=proxy)
            context_kwargs: dict = {
                "viewport": DESKTOP_VIEWPORT,
                "locale": f"{params.hl}-{params.gl.upper()}",
                "ignore_https_errors": bool(proxy),  # proxy MITM certs (BD etc.)
                "storage_state": storage_state or None,
            }
            ua = browser_user_agent(browser)
            if ua:
                context_kwargs["user_agent"] = ua
                user_agent_out = ua
            context = browser.new_context(**context_kwargs)
            context.add_init_script(
                build_init_script(preset_accept_languages(params))
            )
            context.route("**/*", block_media_route)

            def resolver(href: str) -> str:
                return resolve_goto_link(context, params.google_domain, href)

            pages_stage_used = False
            organic_seen: set[str] = set()
            dupes_dropped = 0
            for i in range(pages):
                start = i * 10
                try:
                    if deadline and time.time() > deadline:
                        raise RunDeadlineExceeded()
                    if ip_check is not None and i > 0:
                        current_ip = ip_check()
                        if (current_ip and expected_exit_ip
                                and current_ip != expected_exit_ip):
                            midrun_ip_changed = True
                            expected_exit_ip = current_ip
                            emit_progress(on_progress, "note", "note",
                                          "Exit IP changed mid-run, continuing on new IP")
                    if i == 0:
                        emit_progress(on_progress, "organic", "running",
                                      f"page 1 of {pages}")
                    else:
                        pages_stage_used = True
                        emit_progress(on_progress, "pages", "running",
                                      f"page {i + 1} of {pages}")
                    html, page_url = fetch_search_page(context, params, start,
                                                       policy, warmup=warmup,
                                                       on_progress=on_progress,
                                                       deadline=deadline)
                    pages_fetched += 1
                    page_items = parse_organic(html, link_resolver=resolver)
                    if (i > 0 and page_items and not params.web_mode
                            and organic_off_query(params.q, page_items)):
                        errors.append({
                            "code": "page_off_query",
                            "message": (f"page {i + 1} returned off-query "
                                        "results; ignored."),
                        })
                        emit_progress(on_progress, "note", "note",
                                      f"Page {i + 1} results ignored (off-query)")
                        break
                    dupes_dropped += (len(page_items)
                                      - _merge_organic(page_items, i + 1,
                                                       organic_seen, organic))
                    if i == 0:
                        emit_progress(on_progress, "organic", "done",
                                      f"{len(organic)} results")
                        emit_progress(on_progress, "related", "running")
                        related_searches = _merged_chips(
                            parse_related_searches(html),
                            parse_people_also_search_for(html))
                        related_questions = parse_paa(html)
                        ai_overview = parse_ai_overview(html)
                        local_map, local_results = parse_local_pack(html)
                        module_heads = detect_module_heads(html)
                        emit_progress(on_progress, "related", "done",
                                      f"{len(related_searches)} related, "
                                      f"{len(local_results)} local")
                        emit_progress(on_progress, "paa", "done",
                                      f"{len(related_questions)} questions")
                        emit_progress(on_progress, "ai", "done",
                                      (ai_overview or {}).get("source")
                                      or "not present")
                    has_next = parse_has_next(html)
                    if (i == 0 and organic and not params.web_mode
                            and organic_off_query(params.q, organic)):
                        web_fallback_used = True
                        emit_progress(on_progress, "note", "note",
                                      "Off-query results, refetching in Web mode")
                        try:
                            web_params = replace(params, web_mode=True)
                            (web_organic, web_related, web_questions,
                             web_has_next, web_pages) = _collect_web_fallback(
                                context, web_params, pages, policy, resolver,
                                on_progress=on_progress, deadline=deadline)
                            if web_organic:
                                organic = web_organic
                                pages_fetched = web_pages
                                has_next = web_has_next
                                # Web-tab pages often omit related/PAA blocks —
                                # keep the AI page's blocks when non-empty.
                                related_searches = web_related or related_searches
                                related_questions = web_questions or related_questions
                        except Exception as e:  # noqa: BLE001
                            errors.append({"code": "web_fallback_error",
                                           "message": str(e)[:200]})
                        break
                    if not page_items and i == 0:
                        if "/search" in page_url:
                            _dump_html(html, page_url, start, force=True)
                        errors.append({"code": "zero_result",
                                       "message": "Parser returned 0 organic — selectors may have drifted."})
                except BlockDetected as b:
                    errors.append({"code": b.info.get("kind", "blocked"),
                                   "message": str(b), "detail": b.info})
                    break  # rotating IP/solve budget handled; fail loud
                except RunDeadlineExceeded:
                    errors.append({
                        "code": "run_timeout",
                        "message": "Run deadline reached, partial data returned.",
                    })
                    emit_progress(on_progress, "note", "note",
                                  "Run deadline reached")
                    break
                except Exception as e:  # noqa: BLE001, surface every error
                    errors.append({"code": "fetch_error", "message": str(e)[:300]})
                    break
                if i < pages - 1:
                    time.sleep(jitter_delay_seconds())
            if pages_stage_used:
                emit_progress(on_progress, "pages", "done")
        finally:
            # Thread hygiene: closing the context releases pages/sockets,
            # closing the browser releases the DataImpulse thread. Session
            # survives because sessid+cookies are persisted, not the socket.
            if context is not None:
                try:
                    state_out = context.storage_state()
                except Exception:
                    state_out = None
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    organic = organic[:30]
    for idx, item in enumerate(organic, 1):
        item["position"] = idx

    return {
        "organic": organic,
        "related_searches": related_searches,
        "related_questions": related_questions,
        "ai_overview": ai_overview,
        "local_map": local_map,
        "local_results": local_results,
        "has_next": has_next,
        "pages_fetched": pages_fetched,
        "solves": policy.public_log() if policy else [],
        "errors": errors,
        "storage_state": state_out,
        "user_agent": user_agent_out,
        "exit_ip": expected_exit_ip,
        "midrun_ip_changed": midrun_ip_changed,
        "timing_ms": {"search_ms": int((time.time() - started) * 1000),
                      "web_fallback": web_fallback_used,
                      "heads": module_heads,
                      "dupes_dropped": dupes_dropped},
    }
