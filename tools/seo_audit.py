"""Technical + on-page SEO checks over a crawl.

Checks: HTTPS, broken pages, redirect chains, noindex, titles, meta descriptions,
duplicates, H1s, canonicals, lang, viewport, image alt text, thin content,
response time, robots.txt, XML sitemap, Open Graph, JavaScript-only rendering.

Usage:
  python tools/seo_audit.py .tmp/<domain>/crawl.json
Writes .tmp/<domain>/seo.json
"""

import json
import re
import sys
from collections import defaultdict
from urllib.parse import urlparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import finding, passed, robots_allowed, write_json  # noqa: E402
from page_parse import html_pages, site_kind  # noqa: E402

A = "SEO"


def dupes(pairs):
    groups = defaultdict(list)
    for url, val in pairs:
        if val:
            groups[val.strip().lower()].append(url)
    return [urls for urls in groups.values() if len(urls) > 1]


def bare(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", u.split("#")[0]).rstrip("/").lower()


def audit(crawl: dict) -> dict:
    F, P = [], []
    pages = html_pages(crawl)
    home_raw, home = pages[0]
    # On-page checks only for pages meant to rank: not noindex, not canonicalised to another URL
    ranked = [(r, p) for i, (r, p) in enumerate(pages)
              if i == 0 or not p["noindex"] and not (p["canonical"] and bare(p["canonical"]) != bare(p["url"]))]
    all_recs = crawl["pages"]

    # --- crawl / indexability -------------------------------------------------
    loop = crawl.get("redirect_loop")
    if loop:
        F.append(finding(A, "high", "Homepage is stuck in an endless redirect loop",
                         f"{loop['url']} answers {loop.get('status', '30x')} with Location: {loop.get('location') or '?'} (itself). "
                         "Browsers show ERR_TOO_MANY_REDIRECTS and Googlebot/AI crawlers cannot load the homepage, so it will drop out of search. "
                         "The page loads fine with a query string, which points to a cached redirect"
                         + (f" (cache header: {loop['cache']})." if loop.get("cache") else "."),
                         "Purge the CDN/server cache for the homepage, then check the redirect rules (www/non-www, trailing slash, HTTPS, "
                         "language redirects) for one that matches its own target. Verify with: curl -sI " + loop["url"],
                         [loop["url"]], check="redirect_loop"))
    if not crawl["final_url"].startswith("https://"):
        F.append(finding(A, "high", "Site is not served over HTTPS",
                         "The homepage resolves to plain HTTP. Browsers flag it and Google uses HTTPS as a ranking signal.",
                         "Install a TLS certificate (free via Let's Encrypt) and 301-redirect all HTTP URLs to HTTPS.", [crawl["final_url"]], check="https"))
    else:
        P.append(passed(A, "Served over HTTPS", "https"))

    refused = {401, 402, 403, 429}  # login walls / bot protection / rate limits: not proof the page is broken
    broken = [r["url"] for r in all_recs if (r.get("status") or 0) >= 400 and r["status"] not in refused]
    unreachable = [r["url"] for r in all_recs if r.get("error") or r.get("status") in refused]
    if broken:
        F.append(finding(A, "high", f"{len(broken)} linked page(s) are broken",
                         "Internal links point to pages that return 404/410 or 5xx errors. This wastes crawl budget and frustrates users.",
                         "Fix or remove these links, or 301-redirect the URLs to the closest live page.", broken, check="broken"))
    elif not unreachable:
        P.append(passed(A, "No broken pages in the crawled sample", "broken"))
    if unreachable:
        F.append(finding(A, "low", f"{len(unreachable)} page(s) timed out or refused the audit crawler",
                         "These URLs timed out, failed to connect or answered 401/402/403/429. It can be a temporary hiccup or bot protection, "
                         "but if search crawlers hit the same wall those pages drop out of the index.",
                         "Open the URLs in a browser; if they load, check server logs / WAF rules for timeouts or blocks on crawlers.",
                         unreachable, check="unreachable"))

    chains = [r["url"] for r in all_recs if len(r.get("redirects", [])) > 1]
    if chains:
        F.append(finding(A, "medium", "Redirect chains found",
                         "These URLs go through 2+ redirects before reaching the final page, slowing crawling and diluting link equity.",
                         "Point internal links and redirects straight at the final URL (one hop at most).", chains, check="redirects"))

    noindex = [p["url"] for _, p in pages if p["noindex"]]
    if home["noindex"]:
        F.append(finding(A, "high", "Homepage is set to noindex",
                         "The homepage carries a noindex directive (meta robots or X-Robots-Tag), so search engines will drop it.",
                         "Remove `noindex` from the homepage unless this is intentionally a staging site.", [home["url"]], check="noindex"))
    elif noindex:
        F.append(finding(A, "medium", f"{len(noindex)} page(s) are set to noindex",
                         "These pages ask search engines not to index them. Confirm this is intentional.",
                         "Remove noindex from any page that should appear in search results.", noindex, check="noindex"))
    else:
        P.append(passed(A, "No unintended noindex directives", "noindex"))

    # --- titles / descriptions ------------------------------------------------
    no_title = [p["url"] for _, p in ranked if not p["title"]]
    long_title = [p["url"] for _, p in ranked if p["title"] and len(p["title"]) > 60]
    short_title = [p["url"] for _, p in ranked if p["title"] and len(p["title"]) < 20]
    if no_title:
        F.append(finding(A, "high", "Pages without a <title>", "The title is the main clickable headline in search results.",
                         "Add a unique, descriptive title (50-60 characters) with the page's main keyword near the front.", no_title, check="title"))
    if long_title:
        F.append(finding(A, "low", "Titles longer than 60 characters", "Long titles get truncated in search results.",
                         "Shorten to ~50-60 characters; keep the key phrase and brand.", long_title, check="title"))
    if short_title:
        F.append(finding(A, "medium", "Very short titles (< 20 characters)", "Short titles waste the most important on-page signal.",
                         "Expand to describe the page: `<Primary keyword> - <benefit> | <Brand>`.", short_title, check="title"))
    for urls in dupes([(p["url"], p["title"]) for _, p in ranked]):
        F.append(finding(A, "medium", "Duplicate titles", f'{len(urls)} pages share the same title, so they compete with each other.',
                         "Give each page a unique title reflecting its specific content.", urls, check="title"))
    if not (no_title or long_title or short_title):
        P.append(passed(A, "Titles present and well-sized", "title"))

    no_desc = [p["url"] for _, p in ranked if not p["meta_description"]]
    bad_desc = [p["url"] for _, p in ranked if p["meta_description"] and not 70 <= len(p["meta_description"]) <= 160]
    if no_desc:
        F.append(finding(A, "medium", "Missing meta descriptions",
                         "Without one, Google and AI engines pick a random snippet. A good description lifts click-through rate.",
                         "Write a unique 140-160 character summary per page that answers what the page offers and why to click.", no_desc, check="meta_description"))
    if bad_desc:
        F.append(finding(A, "low", "Meta descriptions outside 70-160 characters", "Too short says little; too long gets cut.",
                         "Rewrite to 140-160 characters.", bad_desc, check="meta_description"))
    for urls in dupes([(p["url"], p["meta_description"]) for _, p in ranked]):
        F.append(finding(A, "low", "Duplicate meta descriptions", f"{len(urls)} pages share one description.",
                         "Write a page-specific description for each.", urls, check="meta_description"))
    if not (no_desc or bad_desc):
        P.append(passed(A, "Meta descriptions present and well-sized", "meta_description"))

    # --- headings / content ---------------------------------------------------
    no_h1 = [p["url"] for _, p in ranked if not p["h1"]]
    multi_h1 = [p["url"] for _, p in ranked if len(p["h1"]) > 1]
    if no_h1:
        F.append(finding(A, "medium", "Pages without an H1 heading", "The H1 tells search engines and AI what the page is about.",
                         "Add exactly one H1 that states the page topic in plain words.", no_h1, check="h1"))
    if multi_h1:
        F.append(finding(A, "low", "Pages with multiple H1s", "Several H1s blur the main topic.",
                         "Keep one H1; demote the rest to H2.", multi_h1, check="h1"))
    if not (no_h1 or multi_h1):
        P.append(passed(A, "One H1 per page", "h1"))

    thin = [p["url"] for _, p in ranked if p["word_count"] < 250]
    if thin:
        F.append(finding(A, "medium", f"{len(thin)} thin page(s) (< 250 words)",
                         "Pages with little text rarely rank and give AI engines nothing to quote.",
                         "Expand with useful, specific content (what, who it's for, how it works, FAQs), or merge/noindex low-value pages.", thin, check="thin"))

    no_alt = [p["url"] for _, p in pages if p["images_no_alt"]]
    total_no_alt = sum(p["images_no_alt"] for _, p in pages)
    if no_alt:
        F.append(finding(A, "low", f"{total_no_alt} image(s) missing alt text",
                         "Alt text helps accessibility, image search and multimodal AI understand images.",
                         "Add short descriptive alt text to meaningful images; use alt=\"\" for decorative ones.", no_alt, check="alt"))
    else:
        P.append(passed(A, "All images have alt text", "alt"))

    # --- technical tags -------------------------------------------------------
    no_canon = [p["url"] for _, p in ranked if not p["canonical"]]
    cross_canon = [p["url"] for _, p in pages if p["canonical"] and crawl["domain"] not in p["canonical"]]
    if no_canon:
        F.append(finding(A, "low", "Pages without a canonical tag", "Canonicals prevent duplicate-URL issues (tracking params, http/https, trailing slashes).",
                         "Add a self-referencing `<link rel=\"canonical\" href=\"...\">` to each page.", no_canon, check="canonical"))
    if cross_canon:
        F.append(finding(A, "high", "Canonical points to another domain", "These pages tell Google that another site holds the original.",
                         "Point canonicals at your own URL unless the content is genuinely syndicated.", cross_canon, check="canonical"))
    if not (no_canon or cross_canon):
        P.append(passed(A, "Canonical tags in place", "canonical"))

    if not home["lang"]:
        F.append(finding(A, "low", "No `lang` attribute on <html>", "The language attribute helps search and AI engines serve the right audience.",
                         "Set it, e.g. `<html lang=\"en\">`.", [home["url"]], check="lang"))
    if not home["viewport"]:
        F.append(finding(A, "high", "No mobile viewport meta tag", "Google indexes mobile-first; without a viewport the page renders as desktop on phones.",
                         "Add `<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">`.", [home["url"]], check="viewport"))
    if not (home["og_title"] and home["og_image"]):
        F.append(finding(A, "low", "Open Graph tags incomplete", "OG title/image control how links look when shared on social, chat apps and some AI interfaces.",
                         "Add og:title, og:description, og:image (1200x630) and twitter:card.", [home["url"]], check="og",
                         snippet='<meta property="og:title" content="...">\n<meta property="og:description" content="...">\n'
                                 '<meta property="og:image" content="https://.../share-1200x630.jpg">\n<meta name="twitter:card" content="summary_large_image">'))

    slow = [r["url"] for r in all_recs if r.get("elapsed_ms", 0) > 2000]
    if slow and (home_raw.get("elapsed_ms", 0) > 2000 or len(slow) >= max(3, len(all_recs) / 4)):
        F.append(finding(A, "medium", "Slow server response (> 2 s)", "Slow time-to-first-byte drags Core Web Vitals and crawl rate.",
                         "Add page caching / a CDN, and check hosting and database performance.", slow, check="speed"))

    if home["word_count"] < 120 and home["scripts"] > 10:
        F.append(finding(A, "high", "Content appears to be rendered by JavaScript only",
                         f"The raw HTML of the homepage contains only {home['word_count']} words. Google can render JS (with delay); most AI crawlers cannot.",
                         "Use server-side rendering or static generation so the main content is in the initial HTML.", [home["url"]], check="js"))

    # --- robots / sitemap -----------------------------------------------------
    robots, robots_status = crawl["robots"]["text"], crawl["robots"]["status"]
    if not robots and robots_status and robots_status >= 500:
        F.append(finding(A, "high", f"robots.txt returns a server error (HTTP {robots_status})",
                         "Google treats a robots.txt that keeps failing with 5xx as 'disallow everything' and stops crawling the site.",
                         "Make /robots.txt return 200 (or 404 if you want no rules).", [crawl["base"] + "/robots.txt"], check="robots"))
    elif not robots and robots_status in (401, 403):
        F.append(finding(A, "low", f"robots.txt refused to the audit crawler (HTTP {robots_status})",
                         "The server (often a bot-protection layer) blocks robots.txt for non-browser clients, so its rules could not be checked. "
                         "Crawlers that get 4xx here assume there are no rules at all.",
                         "Serve /robots.txt to every client, including bots, with a 200.", [crawl["base"] + "/robots.txt"], check="robots"))
    elif not robots and robots_status is None:
        pass  # fetch failed; do not guess
    elif not robots:
        F.append(finding(A, "low", "No robots.txt", "Not fatal, but robots.txt is where you point crawlers to your sitemap and set AI-bot rules.",
                         "Add /robots.txt with at least a Sitemap line.", [crawl["base"] + "/robots.txt"], check="robots",
                         snippet=f"User-agent: *\nAllow: /\n\nSitemap: {crawl['base']}/sitemap.xml"))
    elif not robots_allowed(robots, "Googlebot", crawl["final_url"]):
        F.append(finding(A, "high", "robots.txt blocks search crawlers", "robots.txt disallows the homepage for Googlebot (e.g. `User-agent: *` + `Disallow: /`), keeping the site out of search.",
                         "Remove the blanket Disallow unless the site is intentionally private.", [crawl["base"] + "/robots.txt"], check="robots"))
    else:
        P.append(passed(A, "robots.txt present and not blocking", "robots"))

    sm = crawl["sitemaps"]
    dead = [f["url"] for f in sm.get("failed", []) if f["url"] in sm["declared_in_robots"] and f["status"] in (404, 410)]
    if dead:
        F.append(finding(A, "medium", "robots.txt points to a sitemap that does not exist",
                         "The Sitemap line(s) in robots.txt return 404/410, so crawlers following them find nothing.",
                         "Fix the Sitemap URL in robots.txt or publish the sitemap at that address.", dead, check="sitemap"))
    if not sm["maps_found"]:
        F.append(finding(A, "medium", "No XML sitemap found", "Sitemaps help Google, Bing (which feeds ChatGPT search and Copilot) discover every page.",
                         "Generate /sitemap.xml (most CMSs have a plugin), list it in robots.txt and submit it in Search Console and Bing Webmaster Tools.",
                         [crawl["base"] + "/sitemap.xml"], check="sitemap"))
    else:
        P.append(passed(A, f"XML sitemap found ({sm['url_count']} URLs)" if sm["url_count"] or not sm.get("failed")
                        else "XML sitemap found (its child sitemaps could not be read in time)", "sitemap"))
        if not sm["declared_in_robots"]:
            F.append(finding(A, "low", "Sitemap not referenced in robots.txt", "Crawlers look for the Sitemap line in robots.txt.",
                             "Add the Sitemap line to robots.txt.", [crawl["base"] + "/robots.txt"], check="sitemap",
                             snippet=f"Sitemap: {sm['maps_found'][0]}"))

    extra = checklist_checks(crawl, pages, F, P)

    metrics = {
        **extra,
        "pages_crawled": len(all_recs), "html_pages": len(pages),
        "avg_words": round(sum(p["word_count"] for _, p in pages) / max(len(pages), 1)),
        "avg_response_ms": round(sum(r.get("elapsed_ms", 0) for r in all_recs) / max(len(all_recs), 1)),
        "sitemap_urls": sm["url_count"],
        "home_title": home["title"], "home_description": home["meta_description"], "home_h1": home["h1"][:1],
    }
    return {"findings": F, "passed": P, "metrics": metrics}


LANG_CODE = re.compile(r"^[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?$", re.I)
REGION_SEG = re.compile(r"^/([a-z]{2,3}(?:[-_][a-z]{2,3})?)(?:/|$)", re.I)
ISO_LANG = set("aa ab af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy da de dv dz ee el en eo es "
               "et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj "
               "kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr "
               "nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te tg "
               "th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu".split())
ISO_COUNTRY = set("ad ae af ag al am ao ar at au az ba bd be bf bg bh bi bj bn bo br bs bt bw by bz ca cd cf cg ch ci cl cm cn co cr "
                  "cu cv cy cz de dj dk dm do dz ec ee eg er es et fi fj fm fr ga gb gd ge gh gm gn gq gr gt gw gy hk hn hr ht hu id ie "
                  "il in iq ir is it jm jo jp ke kg kh ki km kn kp kr kw kz la lb lc li lk lr ls lt lu lv ly ma mc md me mg mh mk ml mm "
                  "mn mr mt mu mv mw mx my mz na ne ng ni nl no np nr nz om pa pe pg ph pk pl pt pw py qa ro rs ru rw sa sb sc sd se sg "
                  "si sk sl sm sn so sr ss st sv sy sz td tg th tj tl tm tn to tr tt tv tw tz ua ug uk us uy uz va vc ve vn vu ws ye za "
                  "zm zw uae ksa usa".split())


def locale_segment(seg: str) -> bool:
    """'de', 'en-us', 'eg-en', 'uae_en' are locale folders; 'ai', 'fm', 'now', 'faq' are not."""
    parts = re.split(r"[-_]", seg.lower())
    if len(parts) == 1:
        return parts[0] in ISO_LANG and len(parts[0]) == 2
    a, b = parts
    return (a in ISO_LANG and b in ISO_COUNTRY) or (a in ISO_COUNTRY and b in ISO_LANG)
CONVERSION = re.compile(r"/(pricing|prices|plans|tariffs?|demo|book-a-demo|request-a?-?demo|contact-sales|sales|"
                        r"get-started|sign-?up|register|free-trial|trial|contacts?|contact-us)(/|$)", re.I)


def norm(u: str) -> str:
    return re.sub(r"[?#].*$", "", u).rstrip("/").lower()


def shingles(text: str, k: int = 5) -> set:
    w = text.lower().split()
    return {" ".join(w[i:i + k]) for i in range(max(0, len(w) - k + 1))}


def checklist_checks(crawl: dict, pages, F: list, P: list) -> dict:
    """Checks from the SEO/AEO/GEO checklist: hreflang, regional duplicates, mixed content, images,
    anchors, links to conversion pages, webmaster/analytics tags."""
    parsed = [p for _, p in pages]
    by_url = {norm(p["url"]): p for p in parsed}
    status = {norm(r.get("final_url") or r["url"]): r.get("status") for r in crawl["pages"]}
    home = parsed[0]

    # --- hreflang -----------------------------------------------------------------
    with_hl = [p for p in parsed if p["hreflang"]]
    bad_codes, no_self, no_return, bad_target, no_xdefault = set(), [], set(), set(), []
    for p in with_hl:
        codes = {c.lower() for c, _ in p["hreflang"]}
        bad_codes |= {c for c, _ in p["hreflang"] if c.lower() != "x-default" and (not LANG_CODE.match(c) or c.lower().endswith("-uk"))}
        targets = {norm(h) for _, h in p["hreflang"]}
        if norm(p["url"]) not in targets:
            no_self.append(p["url"])
        if len(codes - {"x-default"}) >= 2 and "x-default" not in codes:
            no_xdefault.append(p["url"])
        for _, h in p["hreflang"]:
            other = by_url.get(norm(h))
            if other is not None and norm(h) != norm(p["url"]) and norm(p["url"]) not in {norm(x) for _, x in other["hreflang"]}:
                no_return.add(f"{p['url']} → {h}")
            if (status.get(norm(h)) or 200) >= 400 or (other is not None and other["noindex"]):
                bad_target.add(h)
    seg_count = defaultdict(int)
    for u in [p["url"] for p in parsed] + [e["loc"] for e in crawl["sitemaps"]["entries"][:3000]] + home["internal_links"]:
        if (m := REGION_SEG.match(urlparse(u).path)) and locale_segment(m.group(1)):
            seg_count[m.group(1).lower()] += 1
    segs = {g for g, n in seg_count.items() if n >= 2}
    if with_hl:
        problems = []
        if bad_codes:
            problems.append(f"invalid codes {', '.join(sorted(bad_codes))} (use ISO 639-1 language + optional ISO 3166-1 region, e.g. en-GB not en-UK)")
        if no_return:
            problems.append(f"{len(no_return)} missing return link(s), which makes Google ignore the pair")
        if no_self:
            problems.append(f"{len(no_self)} page(s) without a self-reference")
        if bad_target:
            problems.append(f"{len(bad_target)} hreflang URL(s) that are broken or noindex")
        if no_xdefault:
            problems.append("no x-default for the language selector / fallback")
        if problems:
            alts = sorted({(c, h) for c, h in home["hreflang"]}) or [("en", crawl["base"] + "/")]
            fixed = [(c.replace("_", "-").replace("-UK", "-GB").replace("-uk", "-gb"), h) for c, h in alts if c.lower() != "x-default"]
            snippet = "\n".join(f'<link rel="alternate" hreflang="{c}" href="{h}">' for c, h in fixed)
            snippet += f'\n<link rel="alternate" hreflang="x-default" href="{fixed[0][1] if fixed else crawl["base"] + "/"}">'
            sev = "medium" if bad_codes or no_return or bad_target else "low"
            F.append(finding(A, sev, "hreflang errors", "hreflang tells Google which language/region version to show where. Found: "
                             + "; ".join(problems) + ".",
                             "Every version must list all versions including itself, with the same set on each page, plus x-default. "
                             "Example for the homepage (repeat the full block on every language version):",
                             sorted(no_return)[:5] + sorted(bad_target)[:5] + no_self[:5], snippet, check="hreflang"))
        else:
            P.append(passed(A, f"hreflang valid on {len(with_hl)} page(s)", "hreflang"))
    elif len(segs) >= 2:
        F.append(finding(A, "medium", "Language/region versions without hreflang",
                         f"The site has regional or language URL sections ({', '.join('/' + s + '/' for s in sorted(segs)[:8])}) but no hreflang tags, "
                         "so Google may show the wrong version or treat them as duplicates competing with each other.",
                         "Add hreflang link tags (or hreflang in the XML sitemap) linking each page to all its language/region versions plus x-default.",
                         [], check="hreflang"))

    # --- near-duplicate (regional) pages ------------------------------------------
    indexable = [p for p in parsed if not p["noindex"] and p["word_count"] >= 150
                 and not (p["canonical"] and p["canonical"].rstrip("/") != p["url"].rstrip("/"))]
    sh = {p["url"]: shingles(p["text_full"]) for p in indexable}
    counts = defaultdict(int)
    for s in sh.values():
        for g in s:
            counts[g] += 1
    boiler = {g for g, n in counts.items() if n > max(2, len(sh) / 2)}  # nav/footer text shared by most pages
    sh = {u: s - boiler for u, s in sh.items()}
    pairs = []
    urls = [u for u in sh if len(sh[u]) >= 40]
    for i, a in enumerate(urls):
        for b in urls[i + 1:]:
            sim = len(sh[a] & sh[b]) / len(sh[a] | sh[b])
            hl_a = {norm(h) for _, h in by_url[norm(a)]["hreflang"]}
            if sim >= 0.9 and norm(b) not in hl_a:
                pairs.append((round(sim, 2), a, b))
    if pairs:
        pairs.sort(reverse=True)
        F.append(finding(A, "medium", f"{len(pairs)} pair(s) of near-duplicate pages",
                         "These pages have (almost) the same main text, with no canonical pointing to one version and no hreflang linking them: "
                         + "; ".join(f"{a} ≈ {b} ({int(s * 100)}%)" for s, a, b in pairs[:4])
                         + ". They compete with each other in search and split their ranking signals.",
                         "Pick one version: merge and 301-redirect, or set rel=canonical to the main one. If they are genuine regional versions, "
                         "localise the content and link them with hreflang.", [u for _, a, b in pairs[:5] for u in (a, b)], check="regional_dupes"))

    # --- mixed content / images / anchors -------------------------------------------
    active = {p["url"]: p["mixed_active"] for p in parsed if p["mixed_active"]}
    passive = {p["url"]: p["mixed_passive"] for p in parsed if p["mixed_passive"]}
    if active or passive:
        ex = next(iter((active or passive).values()))[:3]
        F.append(finding(A, "high" if active else "low", "Mixed content on HTTPS pages",
                         ("Scripts, stylesheets or iframes are loaded over plain http:// - browsers block them, which can break the page. " if active else
                          "Images/media are loaded over plain http:// - browsers show a 'not secure' warning or upgrade them. ")
                         + "Examples: " + ", ".join(ex),
                         "Change every resource URL to https:// (or protocol-relative), and add the header "
                         "`Content-Security-Policy: upgrade-insecure-requests` as a safety net.", list(active or passive), check="mixed_content"))
    elif crawl["final_url"].startswith("https://"):
        P.append(passed(A, "No mixed content", "mixed_content"))

    imgs = {i["src"]: i for p in parsed for i in p["img_info"]}.values()
    legacy = [i for i in imgs if not i["modern"]]
    no_dims = [i for i in imgs if not i["dims"]]
    if len(imgs) >= 5 and len(legacy) / len(imgs) > 0.5:
        F.append(finding(A, "low", f"{len(legacy)} of {len(imgs)} images are JPG/PNG/GIF",
                         "WebP/AVIF are typically 25-50% smaller at the same quality, which speeds up LCP on mobile.",
                         "Serve WebP/AVIF (most CMSs and CDNs can convert automatically), with <picture> fallbacks if needed.",
                         [i["src"] for i in legacy], check="img_format"))
    if len(imgs) >= 5 and len(no_dims) / len(imgs) > 0.3:
        F.append(finding(A, "medium" if len(no_dims) / len(imgs) > 0.6 and len(no_dims) >= 10 else "low",
                         f"{len(no_dims)} of {len(imgs)} images have no width/height",
                         "Without dimensions the browser cannot reserve space, so content jumps while images load (layout shift, CLS > 0.1 hurts Core Web Vitals).",
                         "Add width and height attributes matching the image's aspect ratio (CSS can still make it responsive), "
                         "or set aspect-ratio in CSS.", [i["src"] for i in no_dims], '<img src="hero.webp" width="1200" height="630" alt="..." '
                         'style="max-width:100%;height:auto">', check="img_dims"))
    elif len(imgs) >= 5:
        P.append(passed(A, "Images have width/height set", "img_dims"))

    generic = [(t, h) for p in parsed for t, h in p["generic_anchors"]]
    if len(generic) >= 3:
        by_text = defaultdict(int)
        for t, _ in generic:
            by_text[t.lower()] += 1
        F.append(finding(A, "low", f"{len(generic)} internal links with generic anchor text",
                         "Anchors like " + ", ".join(f"\"{t}\" ×{n}" for t, n in sorted(by_text.items(), key=lambda x: -x[1])[:4])
                         + " tell Google and AI nothing about the target page.",
                         "Use descriptive anchors that name the target (\"Viber Business Messaging\", \"SMS API pricing\"), or add an "
                         "aria-label with the target's name if the design needs a short button.",
                         sorted({h for _, h in generic}), check="anchors"))

    # --- links to conversion pages ---------------------------------------------------
    home_links = {norm(u) for u in home["internal_links"]}
    all_internal = [norm(u) for p in parsed for u in p["internal_links"]]
    conv = sorted({u for u in all_internal if CONVERSION.search(urlparse(u).path + "/")})
    if site_kind(crawl) != "b2b":
        pass  # conversion-page advice is for B2B / SaaS sites; shops and publishers convert differently
    elif not any(CONVERSION.search(urlparse(u).path + "/") for u in home_links):
        F.append(finding(A, "medium", "No pricing, demo or contact page linked from the homepage",
                         "The homepage is the strongest page; a clear link to the conversion page (pricing / demo / contact sales) "
                         "passes authority to it and gives visitors from search and AI answers a next step.",
                         "Link the main conversion page from the header and from the hero with a descriptive anchor "
                         "(e.g. \"Get WhatsApp API pricing\", \"Book a demo\").", [home["url"]], check="money_links"))
    elif conv:
        weak = [u for u in conv if sum(1 for p in parsed if u in {norm(x) for x in p["internal_links"]}) < 0.3 * len(parsed)]
        if weak and len(parsed) >= 5 and len(weak) == len(conv):
            F.append(finding(A, "low", "Conversion pages get few internal links",
                             "Pricing / demo / contact pages are linked from fewer than 30% of the sampled pages.",
                             "Add contextual links to the conversion page from product, use-case and blog pages.", weak, check="money_links"))
        else:
            P.append(passed(A, "Conversion pages linked from the homepage and across the site", "money_links"))

    # --- webmaster tools / analytics --------------------------------------------------
    v = home["verification"]
    missing = [k for k, ok in v.items() if not ok]
    labels = {"google_search_console": "Google Search Console", "bing_webmaster": "Bing Webmaster Tools",
              "google_analytics": "Google Analytics 4 / Tag Manager"}
    if missing:
        F.append(finding(A, "low", "Not detected: " + ", ".join(labels[k] for k in missing),
                         "No verification meta tag / tracking snippet for these was found on the homepage. They may still be set up via DNS "
                         "or another method - check. Search Console and Bing Webmaster Tools show the queries, indexing problems and "
                         "Core Web Vitals field data this audit cannot see; Bing's index also feeds Copilot and ChatGPT search.",
                         "Verify the domain in Search Console and Bing Webmaster Tools (DNS verification is best; Bing can import from GSC), "
                         "submit the sitemap in both, and install GA4 to track organic and AI referrals (chatgpt.com, perplexity.ai).",
                         [home["url"]], check="verification"))
    else:
        P.append(passed(A, "Search Console, Bing Webmaster Tools and analytics tags detected", "verification"))
    return {"hreflang_pages": len(with_hl), "region_sections": sorted(segs)[:20], "conversion_pages": conv[:10],
            "verification": {labels[k]: ok for k, ok in v.items()}, "images_checked": len(imgs)}


def main() -> int:
    crawl = json.loads(Path(sys.argv[1]).read_text())
    res = audit(crawl)
    out = write_json(Path(sys.argv[1]).with_name("seo.json"), res)
    print(f"{len(res['findings'])} findings -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
