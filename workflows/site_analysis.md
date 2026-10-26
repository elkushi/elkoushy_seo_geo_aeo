# Workflow: Site analysis (SEO + AEO + GEO audit)

## Objective
Audit one website (client site or own site) for traditional search (SEO), answer engines (AEO) and generative AI search (GEO), and produce a scored report with a prioritized action plan the owner can hand to a developer or content team.

## Required inputs
| Input | Example | Notes |
|-------|---------|-------|
| Target URL / domain | `https://example.com` | Must resolve to a public address (private/loopback IPs are refused by the SSRF guard in `tools/common.py`). Scheme optional. |
| Max pages (optional) | `25` | Crawl sample size. CLI default 25; web app allows 5-60. More pages = slower run. |
| PageSpeed on/off (optional) | `--no-pagespeed` | Core Web Vitals via Google PageSpeed Insights. Adds 15-60 s per strategy. Needs `PAGESPEED_API_KEY` to be reliable. |
| AI visibility check on/off (optional) | `--no-llm` | LLM test via NVIDIA NIM. Needs `NVIDIA_API_KEY`; skipped cleanly without it. |
| Competitors | n/a | Not an input: the LLM infers likely competitors from the homepage during the AI visibility test. |

## Tools (in order)
`tools/analyze_site.py` orchestrates everything; the other tools can also be run on their own for debugging.

1. `python tools/crawl_site.py <url> --max-pages 25` — homepage, robots.txt, sitemap(s), llms.txt and up to N pages (homepage links first, then sitemap). Respects `User-agent: *` disallows. Writes `.tmp/<domain>/crawl.json` (HTML included, so later tools never refetch).
2. `python tools/seo_audit.py .tmp/<domain>/crawl.json` — technical + on-page SEO (HTTPS, status codes, redirects, noindex, titles, meta descriptions, duplicates, H1, canonical, lang, viewport, alt text, thin content, response time, robots, sitemap, Open Graph, JS-only rendering). Writes `seo.json`.
3. `python tools/aeo_audit.py .tmp/<domain>/crawl.json` — structured data (JSON-LD validity, Organization/WebSite, BreadcrumbList, Article/Product/FAQPage) and answer-ready content (question headings, concise answers, lists/tables). Generates copy-paste JSON-LD snippets. Writes `aeo.json`.
4. `python tools/geo_audit.py .tmp/<domain>/crawl.json` — AI crawler rules in robots.txt, live AI-bot user-agent requests (catches CDN/WAF blocks robots.txt does not show), llms.txt (draft generated), entity signals (sameAs, Wikidata, about page), citability (authors, dates, stats, outbound sources), freshness. Writes `geo.json`.
5. `python tools/pagespeed.py <url>` — Lighthouse scores + LCP/CLS/TBT for mobile and desktop. Runs in parallel with steps 2-4 inside the orchestrator.
6. `python tools/geo_visibility.py .tmp/<domain>/crawl.json` — about 8 LLM calls through `tools/llm_client.py` (NVIDIA NIM). Infers category + 3 buyer questions, then tests recall mode (model memory) and search mode (answers grounded on live DuckDuckGo results). Writes `geo_visibility.json`.
7. `python tools/analyze_site.py <url> [--max-pages 25] [--no-pagespeed] [--no-llm]` — runs 1-6, scores each area (100 minus severity penalties, floor 10), buckets findings into the roadmap, writes the executive summary (LLM if available, deterministic fallback otherwise) and renders `templates/report.html.j2`.

Web app (same pipeline, browser UI):
- `.venv/bin/uvicorn app.server:app --host 127.0.0.1 --port 8000`, then open http://127.0.0.1:8000
- Paste the URL, choose options, start. Jobs run in background threads (max 2 at once) with live progress. History lists past reports; each report has HTML and Markdown downloads.

Helpers: `python tools/llm_client.py "What is Ahrefs?"` tests the NVIDIA key/model; `python tools/check_setup.py --live` tests packages, keys and PageSpeed access.

Every source here is free. Any paid API call needs explicit approval first.

## Steps
1. Run `python tools/check_setup.py` and confirm packages import. Check that `PAGESPEED_API_KEY` and `NVIDIA_API_KEY` show as SET. If either is missing, tell the user which part of the report will be skipped.
2. Confirm the target URL with the user, plus whether it is their own site or a client site (affects tone, not the checks).
3. Run the analysis: the web app for an interactive run, or `python tools/analyze_site.py <url>` from the CLI.
4. Read the console summary or the job status. Check the `skipped` entries: each one marks a part of the report that is missing (PageSpeed quota, no LLM key, LLM gave no usable questions). Fix the cause and rerun if that part matters for this audit.
5. Sanity-check the report before handing it over:
   - Does the crawl reflect the real site? A handful of pages, or near-empty pages, usually means JS-only rendering or bot blocking (see Edge cases).
   - Do the AI crawler table and live-bot results agree with what the owner expects (for example, a deliberate Cloudflare "Block AI bots" rule)?
   - Do the AI visibility buyer questions match the business? If the inferred category is wrong, treat that section as low confidence.
6. Deliver the HTML report and the Markdown action plan (download from the web app, or take the paths printed by the CLI). If the user wants it in the cloud, upload the action plan to Google Docs/Sheets.

## Outputs
- Intermediates (disposable): `.tmp/<domain>/crawl.json`, `findings.json`, plus per-tool JSON when tools run standalone.
- Web app job store: `.tmp/jobs/<id>/report.html`, `action_plan.md`, `meta.json` (survives server restarts).
- Deliverable: the local HTML report and Markdown action plan (accepted deliverables for this project), containing:
  - SEO, AEO, GEO and overall scores with A-E grades, plus an executive summary
  - Prioritized roadmap: Week 1 quick wins / Next 30 days / Next 90 days (authority & content)
  - Each finding with severity, affected URLs, the fix, and copy-paste snippets (JSON-LD, robots.txt rules, llms.txt draft)
  - AI crawler access table (robots.txt rules + live user-agent test per bot)
  - AI visibility test (brand recall, buyer questions, recall vs search answers, competitors mentioned)
  - Core Web Vitals (when PageSpeed ran), checks already passing, and method notes

## Edge cases & failure handling
- **PageSpeed quota (HTTP 429)**: keyless calls share a global daily quota that is often exhausted. `pagespeed.py` backs off (15 s, then 30 s) and retries, then gives up and marks PageSpeed as skipped; the rest of the report still renders. Fix: add `PAGESPEED_API_KEY` to `.env` (Google Cloud console > enable "PageSpeed Insights API" > Credentials > API key). Use `--no-pagespeed` for fast iteration.
- **Transient SSL/connection errors** (`SSLError: EOF occurred in violation of protocol`, connection resets): seen on this network on otherwise healthy sites. `common.fetch()` retries up to 3 times with growing sleeps (2 s, 4 s) and a hard 25 s wall-clock deadline per URL. robots.txt and llms.txt use one attempt only, since a miss there just means "not found". If the homepage still fails, the run stops with `homepage could not be loaded (...)`: rerun once before concluding the site is down.
- **JS-only sites (SPA / client-side rendering)**: the crawler reads raw HTML and does not execute JavaScript. Few words plus many scripts on the homepage raises a high "rendered by JavaScript only" finding. That finding is real for GEO (most AI crawlers do not render JS), but on-page counts (headings, word counts, links) will under-report what Google sees after rendering. Say so in the hand-over.
- **robots.txt / WAF blocking**: the crawler skips URLs disallowed for `User-agent: *`, so a heavily restricted site yields a small sample. A CDN/WAF (Cloudflare, Akamai) may return 403/503 to the audit UA: if the homepage itself fails the run stops, otherwise blocked pages show up as broken-page findings. Check whether these are real errors or bot protection before reporting them. The live AI-bot test reports 401/403/406/451/503 as a block; timeouts are inconclusive, never a block.
- **NVIDIA free tier rate limits**: the developer tier is rate limited per minute (limits vary by model). One analysis uses about 9 calls (8 visibility + 1 summary). The OpenAI client retries twice; if the LLM still fails, the summary falls back to the deterministic text, but an exception inside the visibility test fails the whole job. Avoid running two AI-checked jobs back to back; rerun with `--no-llm` (or untick the AI check) if limits persist. Switch model with `NVIDIA_MODEL` if the default is overloaded.
- **LLM visibility limitations**: recall mode only measures what one model memorized at training time, not what ChatGPT/Perplexity/Gemini show today. Search mode approximates AI search with DuckDuckGo top results, which differ from the Bing/Google indexes those engines use. Brand matching is string-based, so very generic brand names can produce false positives. Present both as directional signals, not measurements.
- **Missing data**: no sitemap, no robots.txt or no llms.txt each become findings, not failures. Missing keys produce a `skipped` entry instead of an error.

## Learnings
Dated notes on quirks, limits and better approaches discovered while running this workflow.
- 2026-10-26: Keyless PageSpeed calls regularly return HTTP 429 (shared global quota). Added backoff/retry in `pagespeed.py` and a clean "skipped" path so the report still renders; a free `PAGESPEED_API_KEY` is effectively required for Core Web Vitals.
- 2026-10-26: This network intermittently drops TLS handshakes (`SSLError ... EOF`) on healthy sites. `common.fetch()` now retries 3 times with backoff inside a 25 s total deadline; a single attempt would misreport healthy pages as broken.
- 2026-10-26: requests' `timeout` is per read, so a slow-trickling server could stall a run indefinitely. `fetch()` streams with a wall-clock deadline and an 8 MB size cap.
- 2026-10-26: Live AI-bot UA checks sometimes time out; a timeout is now reported as inconclusive, never as a WAF block, to avoid false GEO findings.
- 2026-10-26: NVIDIA NIM free tier is rate limited; thinking mode is disabled in `llm_client.py` (`enable_thinking: False`) to save tokens and latency, and the visibility test was kept to about 8 calls per run.
- 2026-10-26: Recall-mode results vary between runs and models (temperature 0.5 on buyer questions). Do not compare scores across runs that used different `NVIDIA_MODEL` values.
- 2026-10-26: Added the checks from the owner's 8-week plan: hreflang validation, near-duplicate (regional) pages, mixed content, image format/width-height, generic anchors, conversion-page links, GSC/Bing/GA4 tag detection ("not detected", DNS verification is invisible), Organization completeness, SoftwareApplication/Service schema on SaaS product pages, dateModified, FAQ hub, TL;DR on long articles, content gaps and review profiles. `page_parse.site_kind()` (b2b / shop / media / other) gates the checks that only make sense for some site types; without it, shops and news sites got B2B advice. Language folders are matched against ISO 639-1/3166 lists (otherwise /ai/, /fm/ looked like locales).
