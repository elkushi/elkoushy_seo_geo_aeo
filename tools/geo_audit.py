"""Generative Engine Optimization checks (deterministic part, no LLM needed).

Can AI engines (ChatGPT search, Perplexity, Claude, Gemini, Copilot, AI Overviews)
reach, understand, trust and quote this site?

Checks: AI crawler rules in robots.txt, live AI-bot access (CDN/WAF blocks that
robots.txt does not show), llms.txt, entity signals (sameAs, Wikidata, about page),
citability (authors, dates, statistics, outbound sources), freshness.

Usage:
  python tools/geo_audit.py .tmp/<domain>/crawl.json
Writes .tmp/<domain>/geo.json
"""

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import finding, passed, robots_allowed, write_json  # noqa: E402
from aeo_audit import ARTICLE_TYPES, ARTICLE_URL  # noqa: E402
from page_parse import html_pages, jsonld_nodes, on_host, site_identity, site_kind  # noqa: E402

A = "GEO"
CONTENT_TYPES = {  # page types buyers and AI engines lean on, matched on URL path + title
    "comparison / alternative pages": r"(?<!\w)(vs|versus)(?![\w-]*code)(?!\w)|\b(alternatives?|compare|comparison|competitors?)\b",
    "glossary / explainer pages": r"\b(what-is|what is|glossary|explained|dictionary|academy|learn|guides?|wiki)\b",
    "pricing page": r"\b(pricing|prices|plans|tariffs?|preise|tarifs?|tseny)\b",
    "case studies / customer stories": r"\b(case-stud(y|ies)|case stud(y|ies)|customers?|success-stor(y|ies)|testimonials?|clients)\b",
    "industry / use-case pages": r"\b(industr(y|ies)|use-cases?|use cases?|sectors?|verticals?|branchen|solutions for)\b",
}
REVIEW_SITES = {"G2": "g2.com", "Capterra": "capterra.com", "TrustRadius": "trustradius.com", "Trustpilot": "trustpilot.com",
                "Crunchbase": "crunchbase.com", "LinkedIn": "linkedin.com", "Clutch": "clutch.co", "Product Hunt": "producthunt.com",
                "GetApp": "getapp.com"}
# token -> (product, purpose). "search" bots fetch pages to cite them in answers; blocking them removes you from those answers.
AI_BOTS = {
    "OAI-SearchBot": ("ChatGPT search", "search"),
    "ChatGPT-User": ("ChatGPT browsing on user request", "search"),
    "GPTBot": ("OpenAI model training", "training"),
    "Claude-SearchBot": ("Claude search", "search"),
    "Claude-User": ("Claude browsing on user request", "search"),
    "ClaudeBot": ("Anthropic model training", "training"),
    "PerplexityBot": ("Perplexity answers", "search"),
    "Perplexity-User": ("Perplexity browsing on user request", "search"),
    "Googlebot": ("Google Search + AI Overviews / AI Mode", "search"),
    "Bingbot": ("Bing + Copilot (ChatGPT search also draws on Bing)", "search"),
    "Google-Extended": ("Gemini training & grounding", "training"),
    "Applebot-Extended": ("Apple Intelligence training", "training"),
    "CCBot": ("Common Crawl (used to train many LLMs)", "training"),
    "meta-externalagent": ("Meta AI training", "training"),
}
LIVE_TEST_UA = {
    "OAI-SearchBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot",
    "PerplexityBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)",
    "ClaudeBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)",
    "GPTBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.2; +https://openai.com/gptbot",
}


def bot_matrix(robots_text: str, home: str) -> dict:
    return {bot: robots_allowed(robots_text, bot, home) for bot in AI_BOTS}


BLOCK_CODES = {401, 403, 406, 451, 503}


def live_bot_access(url: str) -> tuple[dict, dict]:
    """Request the homepage with real AI-bot user agents. A 401/403/406/451/503 means a
    CDN/WAF rule (e.g. Cloudflare's 'Block AI bots') turns them away. Timeouts are retried
    once and then reported as inconclusive, never as a block."""
    out = {}
    for bot, ua in LIVE_TEST_UA.items():
        for _ in range(2):
            try:
                out[bot] = requests.get(url, headers={"User-Agent": ua}, timeout=20, allow_redirects=True).status_code
                break
            except requests.RequestException as e:
                out[bot] = type(e).__name__
    return {b: s for b, s in out.items() if s in BLOCK_CODES}, out


def wikidata(name: str, domain: str) -> list[dict] | None:
    """Wikidata items whose label/alias equals the brand name, each flagged with whether its official website (P856)
    is this domain. None if the lookup itself failed (then the check is skipped)."""
    api, ua = "https://www.wikidata.org/w/api.php", {"User-Agent": "SEO-GEO-AEO-Audit/1.0"}
    short = re.sub(r"\.(com|net|org|io|co|app|ai|[a-z]{2})$", "", name.strip(), flags=re.I)  # "Le Monde.fr" -> "Le Monde"
    names = {name.lower(), short.lower()}
    for _ in range(2):
        try:
            r = requests.get(api, timeout=15, headers=ua, params={"action": "wbsearchentities", "search": short, "language": "en",
                                                                   "format": "json", "limit": 5})
            if r.status_code != 200:
                continue
            hits = [{"id": e["id"], "label": e.get("label"), "description": e.get("description", "")} for e in r.json().get("search", [])
                    if {(e.get("label") or "").lower(), (e.get("match") or {}).get("text", "").lower()} & names]
            if not hits:
                return []
            ents = requests.get(api, timeout=15, headers=ua, params={"action": "wbgetentities", "ids": "|".join(h["id"] for h in hits),
                                                                     "props": "claims", "format": "json"}).json().get("entities", {})
            for h in hits:
                sites = [c["mainsnak"].get("datavalue", {}).get("value", "") for c in ents.get(h["id"], {}).get("claims", {}).get("P856", [])]
                h["website"] = any(on_host(u, domain) for u in sites if isinstance(u, str))
            return hits
        except (requests.RequestException, ValueError, KeyError):
            pass
    return None


def llms_txt_draft(crawl: dict, ident: dict, pages) -> str:
    lines = [f"# {ident['name']}", "", f"> {ident['description'] or 'One-sentence summary of what you do and for whom.'}", "",
             "## Key pages", ""]
    for _, p in pages[:12]:
        title = (p["title"] or p["url"]).split(" | ")[0][:80]
        desc = (p["meta_description"] or "")[:120]
        lines.append(f"- [{title}]({p['url']})" + (f": {desc}" if desc else ""))
    lines += ["", "## Optional", "", f"- [Sitemap]({crawl['sitemaps']['maps_found'][0] if crawl['sitemaps']['maps_found'] else crawl['base'] + '/sitemap.xml'})"]
    return "\n".join(lines)


def audit(crawl: dict) -> dict:
    F, P = [], []
    pages = html_pages(crawl)
    home_raw, home = pages[0]
    ident = site_identity(crawl)
    robots = crawl["robots"]["text"]

    # --- access ----------------------------------------------------------------
    matrix = bot_matrix(robots, crawl["final_url"])
    blocked_search = [b for b, ok in matrix.items() if not ok and AI_BOTS[b][1] == "search"]
    blocked_train = [b for b, ok in matrix.items() if not ok and AI_BOTS[b][1] == "training"]
    sample = [p["url"] for _, p in pages[1:]]
    # Share of crawled inner pages each search bot may fetch: catches section-level rules the homepage test misses
    reach = {b: sum(robots_allowed(robots, b, u) for u in sample) for b in AI_BOTS if AI_BOTS[b][1] == "search"}
    partly = {b: n for b, n in reach.items() if n and b in blocked_search}
    mostly_blocked = [b for b, n in reach.items() if matrix[b] and sample and n < len(sample) / 2]
    if blocked_search:
        F.append(finding(A, "high", "robots.txt blocks AI search crawlers",
                         "Blocked on the homepage: " + ", ".join(f"{b} ({AI_BOTS[b][0]})" for b in blocked_search)
                         + (". Some sections stay open: " + ", ".join(f"{b} may fetch {n} of {len(sample)} sampled pages" for b, n in partly.items())
                            if partly else "")
                         + ". These bots fetch pages so the AI can cite them. Blocking them removes the site from those answers.",
                         "Allow the search/user bots even if you opt out of training.", [crawl["base"] + "/robots.txt"], check="ai_robots",
                         snippet="# Allow AI search & answer engines (citations, referral traffic)\n"
                                 "User-agent: OAI-SearchBot\nUser-agent: ChatGPT-User\nUser-agent: Claude-SearchBot\n"
                                 "User-agent: Claude-User\nUser-agent: PerplexityBot\nUser-agent: Perplexity-User\nAllow: /\n\n"
                                 "# Optional: opt out of model training only\nUser-agent: GPTBot\nUser-agent: ClaudeBot\n"
                                 "User-agent: Google-Extended\nUser-agent: CCBot\nDisallow: /"))
    elif mostly_blocked:
        F.append(finding(A, "medium", "robots.txt keeps AI search crawlers out of most content",
                         "The homepage is open, but " + ", ".join(f"{b} may fetch only {reach[b]} of {len(sample)} sampled pages" for b in mostly_blocked)
                         + ". Answers can only cite pages the bot is allowed to read.",
                         "Review the Disallow rules in the groups for these bots (or `User-agent: *`).", [crawl["base"] + "/robots.txt"], check="ai_robots"))
    elif robots or crawl["robots"]["status"] in (200, 404, 410):
        P.append(passed(A, "AI search crawlers allowed in robots.txt", "ai_robots"))
    if blocked_train:
        F.append(finding(A, "low", "AI training crawlers are blocked (business decision)",
                         "Blocked: " + ", ".join(blocked_train) + ". This keeps content out of future model training, so models may know less about the brand "
                         "when answering without search. It is a valid choice; just make it on purpose.",
                         "Keep it if protecting content matters more than brand recall in LLMs; otherwise allow them.", [crawl["base"] + "/robots.txt"], check="ai_training"))

    live_blocked, live_all = live_bot_access(crawl["final_url"])
    live_blocked = {b: s for b, s in live_blocked.items() if matrix[b]}  # robots.txt already opts these out: consistent, not a surprise
    live_search = any(AI_BOTS[b][1] == "search" for b in live_blocked)
    if live_blocked:
        F.append(finding(A, "high" if live_search else "low",
                         "Firewall/CDN rejects AI crawlers" if live_search else "Firewall/CDN rejects AI training crawlers",
                         "Requests with real AI-bot user agents got: " + ", ".join(f"{b} → {s}" for b, s in live_blocked.items())
                         + f", while a normal browser got {home_raw.get('status', 200)}. robots.txt says one thing, the server does another (common with Cloudflare's 'Block AI bots' setting).",
                         "In your CDN/WAF, allow verified AI search bots (OAI-SearchBot, PerplexityBot, Claude-SearchBot), at least on public marketing pages.",
                         [crawl["final_url"]], check="ai_live"))
    elif any(isinstance(v, int) for v in live_all.values()):  # all timeouts / redirect loops = inconclusive
        P.append(passed(A, "Server answers AI-bot user agents normally", "ai_live"))

    if home["word_count"] < 120 and home["scripts"] > 10:
        F.append(finding(A, "high", "AI crawlers see an almost empty page",
                         "GPTBot, ClaudeBot and PerplexityBot generally do not run JavaScript. The raw HTML holds only "
                         f"{home['word_count']} words, so they have nothing to quote.",
                         "Server-render or pre-render the main content (Next.js SSR/SSG, Nuxt, Astro, prerender.io).", [home["url"]], check="ai_js"))

    # --- llms.txt ---------------------------------------------------------------
    if not crawl["llms_txt"].get("/llms.txt"):
        F.append(finding(A, "low", "No /llms.txt",
                         "llms.txt is a proposed standard: a short Markdown map of your key pages for LLM tools and agents. Adoption by the big engines is "
                         "unconfirmed, so treat it as cheap, low-impact insurance.",
                         "Publish this draft at /llms.txt and edit the descriptions.", [crawl["base"] + "/llms.txt"],
                         llms_txt_draft(crawl, ident, pages), check="llms_txt"))
    else:
        P.append(passed(A, "llms.txt published", "llms_txt"))

    # --- entity -------------------------------------------------------------------
    org_same_as = []
    for n in jsonld_nodes(home["jsonld"]):
        if n.get("sameAs"):
            org_same_as += [u for u in (n["sameAs"] if isinstance(n["sameAs"], list) else [n["sameAs"]]) if isinstance(u, str)]
    if not org_same_as:
        F.append(finding(A, "medium", "No sameAs links tying the brand to its profiles",
                         "LLMs and Google's Knowledge Graph link an entity across sources. `sameAs` in Organization schema names the official "
                         "LinkedIn, Wikipedia/Wikidata, Crunchbase, social and review profiles." + (f" Profiles found on the page: {', '.join(ident['same_as'][:5])}." if ident["same_as"] else ""),
                         "Add `sameAs` to your Organization JSON-LD (see AEO Organization snippet).", [home["url"]], check="sameas"))
    else:
        P.append(passed(A, f"Organization sameAs links ({len(org_same_as)})", "sameas"))

    wd = wikidata(ident["name"], crawl["domain"])
    wd_site = [e for e in wd or [] if e.get("website")]
    if wd is None:
        pass  # lookup failed; do not guess
    elif wd_site:
        P.append(passed(A, f"Wikidata entity exists ({wd_site[0]['id']})", "wikidata"))
    else:
        same_name = ", ".join(f"{e['id']} ({e['description'] or 'no description'})" for e in wd[:3])
        F.append(finding(A, "low", f"No Wikidata entity found for \"{ident['name']}\"",
                         "Wikidata/Wikipedia are among the most-cited sources in LLM training and grounding. An entity there makes the brand "
                         "unambiguous to models." + (f" Items with the same name exist ({same_name}), but none lists {crawl['domain']} as its "
                                                     "official website, so models cannot tie them to this site." if wd else ""),
                         "If the organization is notable (press coverage, references), create a Wikidata item (or add 'official website' to the "
                         "existing one) with logo and social IDs, then link it via sameAs. Do not create promotional Wikipedia articles.", [], check="wikidata"))

    crawled_paths = [urlparse(u).path.lower() for u in [p["url"] for _, p in pages] + crawl.get("home_links", [])]
    if not any(re.search(r"about|who-we-are|our-story|company|team|mission|ueber-uns|uber-uns|über-uns|unternehmen|impressum|qui-sommes|a-propos|"
                                r"chi-siamo|quienes-somos|sobre-nos|over-ons|om-oss|o-nas|o-kompanii|o-firmie", p) for p in crawled_paths):
        F.append(finding(A, "medium", "No About page found in the crawl",
                         "AI engines and Google's quality raters look for who is behind a site (E-E-A-T). An About page with founders, history, "
                         "credentials and contact details is a core trust page.",
                         "Create /about with team, credentials, address and links to press and profiles, and link it in the main nav.", [], check="about"))

    # --- citability -----------------------------------------------------------------
    content = [p for _, p in pages if p["word_count"] >= 300]
    articles = [p for p in content if ARTICLE_URL.search(urlparse(p["url"]).path) or set(p["schema_types"]) & ARTICLE_TYPES]
    no_author = [p["url"] for p in articles if not p["has_author"]]
    no_date = [p["url"] for p in articles if not p["has_date"]]
    no_stats = [p["url"] for p in content if p["numbers_in_text"] == 0]
    no_sources = [p["url"] for p in content if p["links_external"] < 2]
    if articles and len(no_author) / len(articles) > 0.5:
        F.append(finding(A, "medium", "Content has no visible author",
                         "Named experts with bios are a strong trust signal for AI engines picking which source to cite.",
                         "Add author bylines linking to bio pages (credentials, LinkedIn) and Person schema.", no_author, check="author"))
    if articles and len(no_date) / len(articles) > 0.5:
        F.append(finding(A, "medium", "Content has no published/updated date",
                         "Generative engines favor fresh content and need a date to judge it.",
                         "Show 'Published' / 'Updated' dates on content and add datePublished/dateModified in schema.", no_date, check="date"))
    if content and len(no_stats) / len(content) > 0.5:
        F.append(finding(A, "medium", "Few concrete statistics or data points",
                         "GEO research (Princeton, 2023) found that adding statistics, quotations and cited sources raised visibility in "
                         "generative answers by up to ~40%. Pages without numbers are easy to skip.",
                         "Add specific, sourced numbers (prices, results, benchmarks, survey data) and original data where you have it.", no_stats, check="stats"))
    if content and len(no_sources) / len(content) > 0.5:
        F.append(finding(A, "low", "Content rarely cites outside sources",
                         "Linking to authoritative sources (studies, standards, official docs) makes claims verifiable, and engines trust verifiable pages more.",
                         "Cite 2+ credible sources per substantial article.", no_sources, check="sources"))

    dated = [e["lastmod"] for e in crawl["sitemaps"]["entries"] if e.get("lastmod")]
    if dated:
        latest = max(dated)
        if latest < (date.today() - timedelta(days=180)).isoformat():
            F.append(finding(A, "medium", "No content updated in the last 6 months",
                             f"Latest sitemap lastmod is {latest}. AI answers lean toward recent sources.",
                             "Refresh key pages (update facts, dates, examples) and publish on a regular cadence.", [], check="freshness"))
        else:
            P.append(passed(A, f"Recently updated content (latest {latest})", "freshness"))

    # --- from the checklist: content gaps and the off-site review footprint -----------
    kind = site_kind(crawl)
    found_types, profiles = {}, {}
    if kind in ("b2b", "other"):
        cands = [(p["url"], p["title"] or "") for _, p in pages] + [(u, "") for u in crawl.get("home_links", []) + home["internal_links"]]
        cands += [(e["loc"], "") for e in crawl["sitemaps"]["entries"][:5000]]
        for label, rx in CONTENT_TYPES.items():
            hit = next((u for u, t in cands if re.search(rx, urlparse(u).path.replace("/", " ").replace("_", "-").lower() + " " + t.lower())), None)
            if hit:
                found_types[label] = hit
        missing_types = [k for k in CONTENT_TYPES if k not in found_types]
        if missing_types and kind == "b2b":
            name = ident["name"]
            ideas = {"comparison / alternative pages": f'"<Big competitor> alternative", "{name} vs <competitor>", "Best <category> tools (2026)" with an honest comparison table',
                     "glossary / explainer pages": '"What is <core term>?", "<term A> vs <term B>" - question H1, 50-word answer, then detail',
                     "pricing page": "/pricing with plans or a price range, what is included and FAQs (AI answers price questions from it)",
                     "case studies / customer stories": "3+ stories with the customer's name, problem, solution and measurable result",
                     "industry / use-case pages": '"<Product> for <industry>" pages with industry-specific proof points and a CTA'}
            F.append(finding(A, "medium" if len(missing_types) >= 2 else "low", "Missing page types AI engines and buyers rely on",
                             "Not found in the crawl, homepage links or sitemap: " + ", ".join(missing_types) + ". Comparison, explainer and "
                             "pricing pages are among the formats generative engines quote most when buyers ask 'best X', 'X vs Y' or 'how much'.",
                             "Create them, answer-first: " + "; ".join(f"{k}: {ideas[k]}" for k in missing_types) + ".",
                             [], check="content_gaps"))
        elif kind == "b2b":
            P.append(passed(A, "Comparison, explainer, pricing, case-study and industry pages exist", "content_gaps"))

        links = set(home["external_links"]) | {u for _, p in pages for u in p["external_links"]} | set(org_same_as if isinstance(org_same_as, list) else [])
        for label, host in REVIEW_SITES.items():
            u = next((x for x in links if isinstance(x, str) and on_host(x, host)
                      and (label != "LinkedIn" or "/company/" in x or "/school/" in x)), None)
            if u:
                profiles[label] = u
        key = ["G2", "Capterra", "TrustRadius", "Crunchbase", "LinkedIn"] if kind == "b2b" else ["Trustpilot", "LinkedIn", "Crunchbase"]
        missing_p = [k for k in key if k not in profiles]
        if missing_p:
            F.append(finding(A, "medium" if len(missing_p) >= len(key) - 1 and len(key) > 3 else "low", "Review & company profiles not linked from the site",
                             "Not linked from the crawled pages or Organization sameAs: " + ", ".join(missing_p) + ". AI engines cite third-party "
                             "sources (review sites, directories, communities) far more often than a brand's own site, and use these profiles to "
                             "confirm the company exists and what it does." + (f" Linked already: {', '.join(profiles)}." if profiles else ""),
                             "Claim/complete these profiles with an identical name, one-line description, founding date and logo; ask happy customers "
                             "for reviews; then link them in the footer and in Organization sameAs.", [], check="review_profiles"))
        else:
            P.append(passed(A, "Review and company profiles linked (" + ", ".join(profiles) + ")", "review_profiles"))

    metrics = {"site_kind": kind, "content_types_found": found_types, "review_profiles_found": profiles,
               "brand": ident["name"], "ai_bots": {b: {"allowed": ok, "product": AI_BOTS[b][0], "purpose": AI_BOTS[b][1]} for b, ok in matrix.items()},
               "live_bot_status": live_all, "wikidata": (wd or [])[:3], "same_as_found": org_same_as or ident["same_as"],
               "llms_txt": bool(crawl["llms_txt"].get("/llms.txt")), "content_pages": len(content)}
    return {"findings": F, "passed": P, "metrics": metrics}


def main() -> int:
    crawl = json.loads(Path(sys.argv[1]).read_text())
    res = audit(crawl)
    out = write_json(Path(sys.argv[1]).with_name("geo.json"), res)
    print(f"{len(res['findings'])} findings -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
