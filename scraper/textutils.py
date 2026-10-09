"""Small shared text/number helpers for the parsers."""
from __future__ import annotations

import re


def parse_count(value: str | None) -> int | None:
    """Parse Google review counts: '1.7K' -> 1700, '2,314' -> 2314."""
    if not value:
        return None
    text = value.strip().replace(",", "").replace("+", "")
    match = re.match(r"^([\d.]+)\s*([KkMm]?)$", text)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"k": 1_000, "m": 1_000_000}.get(match.group(2).lower(), 1)
    return int(number * multiplier)
