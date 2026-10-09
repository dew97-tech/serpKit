"""Locale presets, URL builders, and runtime configuration.

Contract note: SearchParams mirrors the location-parameterized variant of the
Laravel seo_api consumer. Keep field names stable, a future PHP port should
only replace the transport, never the contract.

Egress and solver configuration is environment driven (see .env.example).
Bright Data proxy zones cannot target Google (docs/PROVIDERS.md); the guard
lives in validate_proxy_for_google().
"""
from __future__ import annotations

import os
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus, urlencode

# ---------------------------------------------------------------------------
# .env loading (tiny, dependency-free; does not override real env vars)
# ---------------------------------------------------------------------------

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(path: Path = _ENV_FILE, override: bool = False) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


def _env_file_mtime(path: Path = _ENV_FILE) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def reload_env_if_changed(path: Path = _ENV_FILE) -> None:
    """Hot-reload `.env` when the file changes (no server restart needed)."""
    global _env_mtime
    mtime = _env_file_mtime(path)
    if mtime is None:
        return
    if _env_mtime is None:
        _env_mtime = mtime
        return
    if mtime != _env_mtime:
        load_dotenv(path, override=True)
        _env_mtime = mtime


load_dotenv()
_env_mtime = _env_file_mtime()

# ---------------------------------------------------------------------------
# Locale presets
# ---------------------------------------------------------------------------

LOCALE_PRESETS = {
    "us": {
        "google_domain": "www.google.com",
        "hl": "en",
        "gl": "us",
        "lr": "",
        "city": "",
        "state": "",
        "country": "United States",
        "accept_languages": ["en-US", "en"],
    },
    "japan": {
        "google_domain": "www.google.co.jp",
        "hl": "ja",
        "gl": "jp",
        "lr": "lang_ja",
        "city": "Tokyo",
        "state": "Tokyo",
        "country": "Japan",
        "accept_languages": ["ja-JP", "ja", "en-US", "en"],
    },
    "bangladesh": {
        "google_domain": "www.google.com.bd",
        "hl": "en",
        "gl": "bd",
        "lr": "",
        "city": "Dhaka",
        "state": "Dhaka",
        "country": "Bangladesh",
        "accept_languages": ["en-US", "en", "bn"],
    },
}
DEFAULT_PRESET = "us"
DEFAULT_LOCALE = {"preset": DEFAULT_PRESET, **LOCALE_PRESETS[DEFAULT_PRESET]}


def resolve_preset(name: str) -> dict:
    """Return the preset dict, falling back to the neutral default."""
    return LOCALE_PRESETS.get((name or "").lower(), LOCALE_PRESETS[DEFAULT_PRESET])


# Pacing: humans don't fire identical requests per second. Jitter between
# paginated fetches; block backoff happens in serp.py/runner.py.
MIN_PAGE_DELAY_S = 3.0
MAX_PAGE_DELAY_S = 8.0
PAA_CLICK_DELAY_MIN_MS = 800
PAA_CLICK_DELAY_MAX_MS = 1600

# Solver defaults (CapSolver): 3 in-run attempts (session reload between
# attempts), no daily cap; the +1 run retry lives in runner.py.
SOLVE_MAX_PER_RUN = int(os.environ.get("SOLVE_MAX_PER_RUN", "3"))
SOLVE_MIN_INTERVAL_S = float(os.environ.get("SOLVE_MIN_INTERVAL_S", "5"))
SOLVE_TIMEOUT_S = float(os.environ.get("SOLVE_TIMEOUT_S", "130"))

# Bright Data host detection (Google policy guard).
BD_PROXY_HOST_MARKER = "brd.superproxy.io"


def new_run_id() -> str:
    """Alphanumeric session id (Bright Data forbids '-' inside session tokens)."""
    return uuid.uuid4().hex[:12]


@dataclass
class SearchParams:
    """Location-parameterized search request (SerpAPI-compatible subset)."""

    q: str
    hl: str = DEFAULT_LOCALE["hl"]
    gl: str = DEFAULT_LOCALE["gl"]
    google_domain: str = DEFAULT_LOCALE["google_domain"]
    lr: str = DEFAULT_LOCALE["lr"]
    city: str = DEFAULT_LOCALE["city"]
    state: str = DEFAULT_LOCALE["state"]
    country: str = DEFAULT_LOCALE["country"]
    # `uule` (encoded location) is optional; when set it pins the local pack.
    uule: str = ""
    # Classic "Web" results (`udm=14`) — used as a fallback when Google's
    # AI-Mode layout returns off-query "Web results" (verified 2026-10).
    web_mode: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "q": self.q,
            "hl": self.hl,
            "gl": self.gl,
            "google_domain": self.google_domain,
            "lr": self.lr,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "uule": self.uule,
        }


# ---------------------------------------------------------------------------
# Egress (proxy) configuration — provider-agnostic, env-driven.
# ---------------------------------------------------------------------------


@dataclass
class ProxyConfig:
    """A plain HTTP(S) proxy with optional sticky-session and geo templates.

    The templates are appended to the username, so one config works across
    providers whose conventions differ (this is the "IP layer only" contract):
      - Bright Data:      "-session-{run_id}" / "-country-{country}"
      - DataImpulse:      "_session-{run_id}" / "_country-{country}"

    Some providers need a different parameter ORDER (e.g. DataImpulse:
    `login__cr.us;sessid.x`), which per-suffix templates can't express.
    `username_template` is a full override supporting {username}, {run_id}
    and {country}; when set it takes precedence over the two suffix fields.
    """

    server: str  # "host:port" or "http://host:port"
    username: str = ""
    password: str = ""
    session_template: str = "-session-{run_id}"
    targeting_template: str = ""
    username_template: str = ""

    def playwright_proxy(self, run_id: str, country: str = "") -> dict:
        payload: dict = {"server": self.server if "://" in self.server
                         else f"http://{self.server}"}
        user = self.build_username(run_id, country)
        if user:
            payload["username"] = user
        if self.password:
            payload["password"] = self.password
        return payload

    def build_username(self, run_id: str, country: str = "") -> str:
        if self.username_template:
            return self.username_template.format(
                username=self.username, run_id=run_id, country=country
            )
        user = self.username
        if user and self.session_template:
            user += self.session_template.format(run_id=run_id)
        if user and self.targeting_template and country:
            user += self.targeting_template.format(country=country)
        return user

    def capsolver_proxy_string(self, run_id: str, country: str = "") -> str:
        """CapSolver uses `http:host:port:user:pass` (proxy-bound solves)."""
        hostport = self.server.replace("http://", "").replace("https://", "")
        user = self.build_username(run_id, country)
        return f"http:{hostport}:{user}:{self.password}"

    def is_brightdata(self) -> bool:
        return BD_PROXY_HOST_MARKER in self.server


def load_proxy_from_env() -> ProxyConfig | None:
    """PROXY_* vars, with BRIGHTDATA_PROXY_* aliases for convenience."""
    reload_env_if_changed()
    server = os.environ.get("PROXY_SERVER", "")
    if not server:
        host = os.environ.get("BRIGHTDATA_PROXY_HOST", "")
        port = os.environ.get("BRIGHTDATA_PROXY_PORT", "")
        if host:
            server = f"{host}:{port}" if port else host
    if not server:
        return None
    username = (os.environ.get("PROXY_USERNAME")
                or os.environ.get("BRIGHTDATA_PROXY_USER", ""))
    password = (os.environ.get("PROXY_PASSWORD")
                or os.environ.get("BRIGHTDATA_PROXY_PASSWORD", ""))
    if not username and "superproxy.io" in server:
        # allow quick no-auth zones (rare) and IP-allowlisted setups
        pass
    return ProxyConfig(
        server=server,
        username=username,
        password=password,
        session_template=os.environ.get("PROXY_SESSION_TEMPLATE", "-session-{run_id}"),
        targeting_template=os.environ.get("PROXY_TARGETING_TEMPLATE", ""),
        username_template=os.environ.get("PROXY_USERNAME_TEMPLATE", ""),
    )


def validate_proxy_for_google(proxy: ProxyConfig | None) -> tuple[bool, str]:
    """Vendor-policy guard: Bright Data proxy zones must not target Google.

    Docs (verified 2026-10): DC/ISP zones return `403 Search engine host is
    not allowed`; Residential/Mobile requests are silently bypassed to their
    superproxy (`x-brd-ip: superproxy bypass`), so the residential IP is
    never used. Google scraping on Bright Data requires their SERP API
    product, which this in-house scraper intentionally does not use.
    Override with ALLOW_BD_GOOGLE=1 only for policy-evidence testing.
    """
    if proxy and proxy.is_brightdata() and not os.environ.get("ALLOW_BD_GOOGLE"):
        return False, (
            "Bright Data proxy zones cannot target Google — their SERP API "
            "product is required by vendor policy (DC/ISP zones: 403; "
            "Residential/Mobile: request bypassed to their superproxy). "
            "Use an IP-layer provider that permits Google SERP scraping "
            "(see docs/PROVIDERS.md) or set ALLOW_BD_GOOGLE=1 for an "
            "evidence-only test."
        )
    return True, ""


def load_capsolver_proxy() -> str | None:
    """Optional proxy URL for CapSolver API calls.

    Some corporate networks block capsolver.com (verified on this office
    network: TCP 443 timeout). Any HTTP proxy that can reach it works —
    including a Bright Data zone, since api.capsolver.com is NOT a search
    engine (BD only restricts search-engine hosts).
    """
    reload_env_if_changed()
    server = os.environ.get("CAPSOLVER_PROXY_SERVER", "")
    if not server:
        return None
    user = os.environ.get("CAPSOLVER_PROXY_USERNAME", "")
    password = os.environ.get("CAPSOLVER_PROXY_PASSWORD", "")
    auth = f"{user}:{password}@" if user else ""
    return f"http://{auth}{server}"


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------


def build_uule(canonical_name: str) -> str:
    """Encode a canonical Google Ads location name as a `w+` uule value.

    Canonical form is `City,Region,Country` (e.g. "Tokyo,Tokyo,Japan").
    Payload = protobuf {1:2, 2:32, 4:name}, base64url, prefixed with "w+"
    (reverse-engineered format; verified against published test vectors).
    """
    name = canonical_name.strip().encode("utf-8")
    if not name or len(name) > 127:
        return ""
    payload = b"\x08\x02\x10\x20\x22" + bytes([len(name)]) + name
    import base64 as _base64

    return "w+" + _base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def canonical_location_uule(city: str, state: str, country: str) -> str:
    """Build a uule from location fields when they form a canonical name.

    Requires at least city + country; state is included when present.
    """
    city, state, country = city.strip(), state.strip(), country.strip()
    if not city or not country:
        return ""
    return build_uule(",".join(p for p in (city, state, country) if p))


# Free-text location aliases -> (canonical Ads name, ISO country code).
LOCATION_ALIASES = {
    "dhaka": ("Dhaka,Dhaka,Bangladesh", "bd"),
    "chittagong": ("Chattogram,Chattogram,Bangladesh", "bd"),
    "tokyo": ("Tokyo,Tokyo,Japan", "jp"),
    "osaka": ("Osaka,Osaka,Japan", "jp"),
    "new york": ("New York,New York,United States", "us"),
    "new york city": ("New York,New York,United States", "us"),
    "nyc": ("New York,New York,United States", "us"),
    "los angeles": ("Los Angeles,California,United States", "us"),
    "london": ("London,England,United Kingdom", "gb"),
}
COUNTRY_NAME_TO_CODE = {
    "bangladesh": "bd", "japan": "jp", "jp": "jp",
    "us": "us", "usa": "us", "united states": "us",
    "united states of america": "us",
    "uk": "gb", "united kingdom": "gb", "england": "gb",
}
# Countries whose location automatically switches `gl` (kept jp/us/bd for now).
AUTO_GL_COUNTRIES = {"bd", "jp", "us"}


def parse_location(location: str) -> tuple[str, str]:
    """Parse free-text location into (canonical_name, country_code).

    Accepts an alias ("Dhaka"), "City, Country", or a canonical
    "City,Region,Country". Returns ("", "") when unusable.
    """
    raw = (location or "").strip()
    if not raw:
        return "", ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return LOCATION_ALIASES.get(parts[0].lower(), ("", ""))
    if len(parts) == 2:
        city, country = parts
        code = COUNTRY_NAME_TO_CODE.get(country.lower(), "")
        alias = LOCATION_ALIASES.get(city.lower())
        if alias and (not code or alias[1] == code):
            return alias
        return f"{city},{country}", code
    canonical = ",".join(parts)
    code = COUNTRY_NAME_TO_CODE.get(parts[-1].lower(), "")
    if code:
        return canonical, code
    alias = LOCATION_ALIASES.get(parts[0].lower())
    return alias if alias else (canonical, "")


def build_search_url(params: SearchParams, start: int = 0) -> str:
    query = {
        "q": params.q,
        "hl": params.hl,
        "gl": params.gl,
        "start": start,
        "pws": "0",  # no personalization -> reproducible snapshots
        "filter": "0",  # include omitted (near-duplicate) results
    }
    if params.lr:
        query["lr"] = params.lr
    if params.uule:
        query["uule"] = params.uule
    if params.web_mode:
        query["udm"] = "14"  # classic Web results tab
    return f"https://{params.google_domain}/search?{urlencode(query)}"


def build_autocomplete_url(params: SearchParams) -> str:
    # Lightweight JSONP endpoint — plain HTTP, no browser needed.
    query = {"q": params.q, "hl": params.hl, "gl": params.gl, "client": "firefox"}
    return f"https://{params.google_domain}/complete/search?{urlencode(query)}"


def build_maps_search_url(params: SearchParams) -> str:
    return f"https://{params.google_domain}/maps/search/{quote_plus(params.q)}"


def jitter_delay_seconds(
    lo: float = MIN_PAGE_DELAY_S, hi: float = MAX_PAGE_DELAY_S
) -> float:
    return random.uniform(lo, hi)


def preset_accept_languages(params: SearchParams) -> list[str]:
    """Browser `navigator.languages` for the request locale (anti-bot consistency)."""
    for preset in LOCALE_PRESETS.values():
        if preset["hl"] == params.hl and preset["gl"] == params.gl:
            return list(preset["accept_languages"])
    return [f"{params.hl}-{params.gl.upper()}", params.hl, "en-US", "en"]


def build_init_script(languages: list[str]) -> str:
    """Pre-load fingerprint patch (must run before any site script).

    Truth-up patches only — no fake plugins/UA (the browser reports its real
    version now; see `launch_chromium` in serp.py). SwiftShader WebGL strings
    and missing `window.chrome` are the headless giveaways that remain.
    """
    langs = json_dumps(languages)
    return "".join([
        "(function () { try {",
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });",
        f"Object.defineProperty(navigator, 'languages', {{ get: () => {langs} }});",
        "if (!window.chrome) { window.chrome = {}; }",
        "if (!window.chrome.runtime) { window.chrome.runtime = {}; }",
        "Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });",
        "const patchGL = (proto) => { if (!proto) { return; } "
        "const orig = proto.getParameter; "
        "proto.getParameter = function (p) { "
        "const v = orig.call(this, p); "
        "if (p === 37445 && /SwiftShader/i.test(String(v))) { return 'Google Inc. (NVIDIA)'; } "
        "if (p === 37446 && /SwiftShader/i.test(String(v))) { "
        "return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)'; } "
        "return v; }; };",
        "if (typeof WebGLRenderingContext !== 'undefined') { "
        "patchGL(WebGLRenderingContext.prototype); }",
        "if (typeof WebGL2RenderingContext !== 'undefined') { "
        "patchGL(WebGL2RenderingContext.prototype); }",
        "const origQuery = navigator.permissions && navigator.permissions.query;",
        "if (origQuery) { navigator.permissions.query = (p) => "
        "(p && p.name === 'notifications' "
        "? Promise.resolve({ state: Notification.permission }) "
        ": origQuery.call(navigator.permissions, p)); }",
        " } catch (e) {} })();",
    ])


def json_dumps(value: list[str]) -> str:
    import json as _json

    return _json.dumps(value)
