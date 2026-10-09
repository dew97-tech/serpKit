"""Reliability benchmark: N live scrapes on the demo keyword matrix.

Measures what the PM/feasibility note needs: organic success %, CAPTCHA
attempts vs accepted, latency and error mix — end-to-end through whatever
egress is configured in `.env` (proxy) with CapSolver solving enabled.

Usage:
    python scripts/benchmark.py --runs 8 --pages 1 --preset us
    python scripts/benchmark.py --runs 4 --preset japan --include-maps 1

Writes `samples/benchmark.json` and prints a summary. Pace with --delay
(default 20 s) — hammering one egress pool burns its Google reputation.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import DEMO_KEYWORDS  # noqa: E402
from scraper.config import SearchParams, load_proxy_from_env, resolve_preset  # noqa: E402
from scraper.runner import run_full_scrape  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=8)
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--preset", default="us")
    ap.add_argument("--include-maps", type=int, default=0)
    ap.add_argument("--delay", type=float, default=20.0)
    ap.add_argument("--no-session", action="store_true",
                    help="disable session reuse (A/B baseline)")
    ap.add_argument("--out", default="samples/benchmark.json")
    args = ap.parse_args()
    if args.no_session:
        os.environ["SESSION_REUSE"] = "0"

    base = resolve_preset(args.preset)
    kws = [k["q"] for k in DEMO_KEYWORDS.get(args.preset, DEMO_KEYWORDS["us"])]
    proxy = load_proxy_from_env()
    print(f"preset={args.preset} runs={args.runs} pages={args.pages} "
          f"proxy={proxy.server if proxy else 'none'} "
          f"session_reuse={os.environ.get('SESSION_REUSE', '1')}")

    results = []
    for i in range(args.runs):
        q = kws[i % len(kws)]
        params = SearchParams(
            q=q, hl=base["hl"], gl=base["gl"],
            google_domain=base["google_domain"], lr=base.get("lr", ""),
            city=base.get("city", ""), state=base.get("state", ""),
            country=base.get("country", ""),
        )
        r = run_full_scrape(params, pages=args.pages,
                            include_maps=bool(args.include_maps),
                            proxy_cfg=proxy, headless=True, solve=True)
        d = r.to_dict()
        solved = sum(1 for s in d["solves"] if s["outcome"] == "solved")
        entry = {
            "q": q,
            "organic": len(d["organic"]),
            "pages_fetched": d["pages_fetched"],
            "related_searches": len(d["related_searches"]),
            "related_questions": len(d["related_questions"]),
            "ai_overview": bool(d["ai_overview"]),
            "local_pack": bool(d["local_map"]),
            "solves": len(d["solves"]),
            "solves_accepted": solved,
            "errors": [e["code"] for e in d["errors"]],
            "total_ms": d["timing_ms"].get("total_ms", 0),
        }
        results.append(entry)
        print(f"[{i + 1}/{args.runs}] {q[:42]!r} organic={entry['organic']} "
              f"pages={entry['pages_fetched']} solves={entry['solves']}/{solved} "
              f"errors={entry['errors']} {entry['total_ms']}ms")
        if i < args.runs - 1:
            time.sleep(args.delay)

    n = len(results)
    success = sum(1 for r in results if r["organic"] > 0)
    solves = sum(r["solves"] for r in results)
    accepted = sum(r["solves_accepted"] for r in results)
    ms = [r["total_ms"] for r in results if r["total_ms"]]
    summary = {
        "preset": args.preset,
        "runs": n,
        "pages": args.pages,
        "proxy": proxy.server if proxy else None,
        "session_reuse": os.environ.get("SESSION_REUSE", "1"),
        "organic_success": success,
        "organic_success_pct": round(100.0 * success / n, 1) if n else 0.0,
        "captcha_solves_attempted": solves,
        "captcha_solves_accepted": accepted,
        "solves_per_query": round(solves / n, 2) if n else None,
        "captcha_solve_accept_pct": round(100.0 * accepted / solves, 1) if solves else None,
        "avg_total_ms": int(statistics.mean(ms)) if ms else None,
        "results": results,
    }
    Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"},
                     indent=2))
    print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
