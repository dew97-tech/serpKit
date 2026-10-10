# serpKit Classwork

In-house Google SERP collector, JSON API, and viewer. Replaces paid per-call SERP APIs for Laravel `seo_api`.

This classwork covers Flow -> Mechanism -> Process -> Finalization. Each chapter pairs text with one xiaohei illustration from `assets/serpkit-illustrations/`.

## Map

- `01-flow.md` — system flow: query in, JSON out. Illus `Fig 1`
- `02-mechanism.md` — locale, egress, fingerprint, solver, parsers. Illus `Fig 2`, `Fig 3`, `Fig 4`
- `03-process.md` — run lifecycle, retries, debugging. Illus `Fig 6`
- `04-finalization.md` — contract, Laravel handoff, limits. Illus `Fig 5`
- `05-labs.md` — hands-on exercises with mock + tests.
- Illustrations: `assets/serpkit-illustrations/01-run-pipeline.svg` … `06-failure-handling.svg`; spec in `assets/serpkit-illustrations/shot-list.md`, QA in `assets/serpkit-illustrations/QA.md`.

## How to read

1. Read chapters in order. Each has a mermaid flowchart that renders on GitHub.
2. Open the matching SVG figure next to the chapter for the illustrated version (English labels, same content).
3. Run labs with `mock=1` first, no network needed.

## Sources of truth

- `README.md:28-47`, `api.py:239-267`, `api.py:274-351`
- `scraper/runner.py:342-471`, `scraper/runner.py:233-339`
- `scraper/config.py:68-99`, `scraper/schemas.py:17-88`
- `docs/MAPPING.md`, `docs/FEASIBILITY.md`
