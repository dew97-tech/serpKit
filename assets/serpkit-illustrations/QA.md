# QA — serpKit v2 illustrations (dev-clarity)

Clarity failures are easy to introduce; these tests exist so they get caught.

## Mandatory tests

1. **10-second test** — hand one figure to a developer who has not read the classwork. They must name the stages in order within 10 seconds. If they need the text, the figure fails.
2. **Arrow-label test** — every arrow is labeled ("fetch", "parse", "finalize", "uule", "reuse ≤25 min" …) or point-to-point with a self-evident single result box (Fig 6 rows).
3. **English-only** — scan for CJK characters: zero allowed.
4. **Mono test** — code identifiers (`THREADS_EXHAUSTED`, `.state.json`, `organic[]`) render in monospace, ≥13px.
5. **No-overlap test** — at 1280px wide, no text touches a line, box edge, or the xiaohei figure; min ~8px padding.
6. **Palette test** — orange = main flow only; red = warning/failure/result only; blue = optional/state/secondary only; black = structure.
7. **Xiaohei test** — exactly one per figure, at the key junction, action matches caption; removing him must not break comprehension (accent, not carrier).

## Render checks

- Open each SVG at 100% in a browser; all labels legible without zoom.
- Mermaid blocks in `docs/classwork/*.md` render on GitHub (LR/TD direction, no syntax errors).
- Node titles match real code names (`scraper/runner.py`, `collect_search`, `CapSolver`).

## Deliver judgment

A good figure answers "what happens, in what order, with what transport" at a glance. If it first reads as art, it fails.
