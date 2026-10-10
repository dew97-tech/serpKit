# serpKit illustrations — v2 shot list (dev-first)

Style: white background, black hand line, orange = main flow, red = failure/warn, blue = optional/state.
Hard rules: all labels English; node titles = real component names; every arrow labeled or obviously directional; one xiaohei accent per figure, performing the figure's key action (never covering a label).

| Fig | File | Topic | Nodes | Xiaohei |
|---|---|---|---|---|
| 1 | `01-run-pipeline.svg` | Full run pipeline | Query → Autocomplete (HTTP JSONP) → Chromium (pages 1–3) → Parsers → Contract JSON; Maps branch (blue dotted, optional) | On the Contract JSON box, plugs the final cable. Caption: "one contract every run" |
| 2 | `02-locale-resolution.svg` | Locale resolution | preset chips (us / japan / bangladesh) + overrides → Resolve + encode (uule) → Search URL | Flips the preset switch on the resolve box. Caption: "pick locale" |
| 3 | `03-session-lifecycle.svg` | Session lifecycle | Session store → Run queries → Exit-IP check → "same IP?" → reuse ≤25 min / rotate + warmup; persist state | Watchman with magnifier at the decision. Caption: "verify" |
| 4 | `04-captcha-loop.svg` | CAPTCHA solve loop | /sorry/ → CAPTCHA kind (v2 Enterprise) → CapSolver task → Inject token → Submit + continue | Cranks the attempts ratchet. Caption: "attempt ≤ 3" |
| 5 | `05-contract-to-seo-api.svg` | Contract → consumers | Two panels: field names (mono) → consumer names, one arrow "same field names" | Stamps the contract. Caption: "stamped frozen" |
| 6 | `06-failure-handling.svg` | Failure handling | error state (mono codes) → action per row: retry ×1 / backoff / stop (red) / partial (blue) | Pulls the retry lever. Caption: "one retry" |

## Notes

- Mermaid equivalents live in the classwork chapters (`docs/classwork/*.md`); SVGs and mermaid carry the same labels.
- Hand annotations are English only. No Chinese labels anywhere.
- Line style stays lightly hand-drawn (wobbly rounded corners) but the layout is a proper diagram: aligned nodes, single direction, labeled connectors.
