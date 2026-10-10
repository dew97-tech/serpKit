# Block to consumer mapping (contract to seo_api)

Frozen JSON field names mirror the SerpAPI subset so Laravel code swaps
collector without rewrites.

| Scraped block | Contract field | Consumer in seo_api |
|---|---|---|
| organic top-30 (pages 1–3) | `organic[{position,title,link,snippet}]` | keyword acquisition competitor URLs; article prompt `competitor_urls`; backlink prospecting (pages 1–3) |
| related searches (page-1 bottom) and People also search for | `related_searches[]` (merged, deduped) | article prompt var `related_searches` |
| PAA / related_questions | `related_questions[{question,snippet,link}]` | `RetrieveKeywordAdditionalDataCommand.related_questions_data`; article prompt `related_questions` |
| AI Overview / AI Mode | `ai_overview{text_blocks,links[,source]}` or `None` (kept only when `text_blocks` non-empty). Classic overviews carry cited `links[]`; when Google serves the AI Mode panel instead, its reply is captured with `source: "ai_mode"` (additive key; links are Google-internal and stay empty) | `RetrieveKeywordAdditionalDataCommand.ai_overview_data` |
| local pack | `local_map{"present","count"}` + `local_results[{title,address,rating,reviews}]` parsed from `div.VkpGBb` cards; `phone/website/place_id` not present in card markup and stay empty | `RetrieveKeywordAdditionalDataCommand.meo_data`; MEO flow |
| google_autocomplete | `autocomplete[]` | article prompt var `suggestions` |
| google_maps top-10 | `maps_local_results[{position,title,address,rating,reviews,hours}]` from the Maps list feed (`website` needs a place-page pass) | maps/MEO flow |
| pagination | `has_next`, `pages_fetched` | crawl continuation decision |
| location | `locale{...,uule}` built from the free-text `location` param (aliases + canonical `City,Region,Country`) or `city/state/country` | localized SERP / MEO |

Laravel call shape (sidecar): `GET /api/scrape?q=&hl=ja&gl=jp&domain=www.google.co.jp
&city=Tokyo&state=Tokyo&country=Japan&uule=&pages=3&include_maps=1`
→ same params as `ArticleGenerationService::fetchSerpResults` location variant.
