"""Frozen JSON contract (and the future PHP port).

Field names intentionally mirror the SerpAPI subset `seo_api` consumes today,
so Laravel mapping code (`RetrieveKeywordAdditionalDataCommand`,
article prompt vars, MEO flow) does not change regardless of whether the
collector is this Python service or a later PHP rewrite.

Backend rule preserved: `ai_overview` is only kept when `text_blocks` is
non-empty; otherwise it is `None` (absent block, not an error).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class OrganicResult:
    position: int
    title: str
    link: str
    snippet: str = ""


@dataclass
class RelatedQuestion:
    question: str
    snippet: str = ""
    link: str = ""


@dataclass
class CitedLink:
    title: str
    link: str


@dataclass
class AIOverview:
    text_blocks: list = field(default_factory=list)
    links: list = field(default_factory=list)  # list[CitedLink] as dicts

    def is_present(self) -> bool:
        return bool(self.text_blocks)


@dataclass
class LocalResult:
    position: int
    title: str
    address: str = ""
    phone: str = ""
    rating: float | None = None
    reviews: int | None = None
    website: str = ""
    place_id: str = ""


@dataclass
class MapsResult:
    position: int
    title: str
    address: str = ""
    rating: float | None = None
    reviews: int | None = None
    website: str = ""
    hours: str = ""
    phone: str = ""


@dataclass
class ScrapeResult:
    query: str
    locale: dict
    organic: list = field(default_factory=list)
    related_searches: list = field(default_factory=list)
    related_questions: list = field(default_factory=list)
    ai_overview: dict | None = None
    local_map: dict | None = None
    local_results: list = field(default_factory=list)
    autocomplete: list = field(default_factory=list)
    maps_local_results: list = field(default_factory=list)
    has_next: bool = False
    pages_fetched: int = 0
    solves: list = field(default_factory=list)  # CapSolver telemetry
    timing_ms: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    mock: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
