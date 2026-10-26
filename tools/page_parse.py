"""Parse one crawled page into the fields the audits need (parsed once, cached on the record)."""

import json
import re
from urllib.parse import urljoin, urlparse

import extruct
from bs4 import BeautifulSoup

# Data points: money, percentages, multipliers, magnitudes, numbers with units, 1,200-style and decimal numbers,
# 5+ digit numbers. Bare years (footer "© 2025") and small list numbers ("1.", "Step 3") are not statistics.
STAT = re.compile(
    r"[$€£¥]\s?\d[\d.,]*"
    r"|\b\d[\d.,]*\s?(?:%|percent|prozent|pour ?cent|por ?ciento|million|billion|mio\b|mrd\b|[kmb]\b\+?|x\b|\+|€|\$)"
    r"|\b\d+(?:[.,]\d+)?\s?(?:ms|seconds?|minutes?|hours?|days?|weeks?|months?|years?|kg|km|mb|gb|tb|users|customers|people|"
    r"companies|countries|stars|reviews|times|sekunden|minuten|stunden|tage|jahre|kunden)\b"
    r"|\b\d{1,3}(?:,\d{3})+\b|\b\d+\.\d+\b|\b(?!(?:19|20)\d{2}\b)\d{4}\d*\b", re.I)
GA_RX = re.compile(r"gtag/js\?id=G-|googletagmanager\.com/gtm\.js|['\"]G-[A-Z0-9]{6,12}['\"]|GTM-[A-Z0-9]{4,}")
GENERIC_ANCHOR = re.compile(r"^(click here|here|read more|learn more|more|this|this page|link|continue reading|details|"
                            r"mehr|hier|weiterlesen|mehr erfahren|en savoir plus|ici|lire la suite|leer más|más|aquí|"
                            r"подробнее|читать далее|здесь)\W*$", re.I)
SUMMARY_RX = re.compile(r"tl;?dr|key takeaways|in short|at a glance|summary|in a nutshell|the short answer|"
                        r"auf einen blick|zusammenfassung|en bref|résumé|en resumen|кратко", re.I)
MODERN_IMG = re.compile(r"\.(webp|avif|svg)(\?|$|\s)|[?&](fm|format|auto)=(webp|avif|auto|format)|/f_auto|/format:webp", re.I)
QUESTION = re.compile(r"\?\s*$")
QUESTION_WORD = re.compile(r"^(what|how|why|when|where|who|which|can|does|do|is|are|should|will)\b", re.I)


def on_host(url: str, host: str) -> bool:
    """True if url is on host or one of its subdomains (docs.example.com counts as example.com; netflix.com is not x.com)."""
    h = urlparse(url).hostname or ""
    return h == host or h.endswith("." + host)


HOW_TO = re.compile(r"^(how|what|where|when|which|who) to\b", re.I)  # "How to create X", "Where to go from here"


def is_question(text: str) -> bool:
    """'How do I export data' counts; 'Who it's for' (a label) and 'How to create X' (a how-to step) do not."""
    if QUESTION.search(text):
        return True
    return bool(QUESTION_WORD.search(text)) and not HOW_TO.search(text) and len(text.split()) >= 4


def is_teaser(h, text: str) -> bool:
    """A heading that is mostly a link to another page is a card / headline teaser, not a question answered here."""
    a = h.find_parent("a", href=True) or h.find("a", href=True)
    if not a or a["href"].startswith("#"):
        return False
    return h.find_parent("a") is not None or len(a.get_text(" ", strip=True)) >= 0.6 * len(text)


def has_key(items, key: str) -> bool:
    """True if any (nested) dict in the structured data has this key, e.g. Article.author."""
    stack = [items]
    while stack:
        it = stack.pop()
        if isinstance(it, dict):
            if it.get(key):
                return True
            stack.extend(it.values())
        elif isinstance(it, list):
            stack.extend(it)
    return False


def schema_types(items) -> list[str]:
    """All @type values in JSON-LD / microdata items, including nested @graph."""
    types, stack = [], list(items)
    while stack:
        it = stack.pop()
        if isinstance(it, list):
            stack.extend(it)
        elif isinstance(it, dict):
            t = it.get("@type") or it.get("type")
            for v in (t if isinstance(t, list) else [t]):
                if isinstance(v, str):
                    types.append(v.rsplit("/", 1)[-1])
            stack.extend(v for v in it.values() if isinstance(v, (dict, list)))
    return types


def jsonld_nodes(items) -> list[dict]:
    """Top-level JSON-LD objects plus the members of their @graph (which may be a list or a single object)."""
    out = []
    for it in items if isinstance(items, list) else [items]:
        if isinstance(it, list):
            out += jsonld_nodes(it)
        elif isinstance(it, dict):
            g = it.get("@graph", [])
            out += [it] + [n for n in (g if isinstance(g, list) else [g]) if isinstance(n, dict)]
    return out


def parse(rec: dict) -> dict:
    if "_parsed" in rec:
        return rec["_parsed"]
    url = rec.get("final_url") or rec["url"]
    html = rec.get("html") or ""
    soup = BeautifulSoup(html, "lxml")
    host = (urlparse(url).hostname or "").removeprefix("www.")

    def meta(name=None, prop=None):
        tag = soup.find("meta", attrs={"name": re.compile(f"^{name}$", re.I)} if name else {"property": prop})
        return (tag.get("content") or "").strip() if tag else None

    canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
    title = next((t.get_text(strip=True) for t in soup.find_all("title") if not t.find_parent("svg")), None)
    robots_meta = (meta("robots") or "").lower()

    # Structured data (extruct handles JSON-LD, microdata, RDFa, OpenGraph)
    jsonld_errors = 0
    for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            jsonld_errors += 1
    try:
        sd = extruct.extract(html, base_url=url, syntaxes=["json-ld", "microdata", "opengraph"], errors="ignore")
    except Exception:
        sd = {"json-ld": [], "microdata": [], "opengraph": []}
    types = schema_types(sd.get("json-ld", [])) + schema_types(sd.get("microdata", []))

    headings = [(h.name, h.get_text(" ", strip=True)) for h in soup.find_all(["h1", "h2", "h3", "h4"])]
    questions, answered = [], 0
    for h in soup.find_all(["h2", "h3", "h4"]):
        text = h.get_text(" ", strip=True)
        if text and len(text) < 160 and is_question(text) and not is_teaser(h, text):
            questions.append(text)
            nxt = h.find_next(["p", "ul", "ol", "table", "div"])
            words = len(nxt.get_text(" ", strip=True).split()) if nxt else 0
            if 15 <= words <= 90:
                answered += 1

    links_ext, links_int = set(), set()
    for a in soup.find_all("a", href=True):
        h = urljoin(url, a["href"].strip()).split("#")[0]
        if h.startswith("http"):
            (links_int if on_host(h, host) else links_ext).add(h)

    sd_all = [sd.get("json-ld", []), sd.get("microdata", [])]
    author = (meta("author") or soup.find(attrs={"rel": "author"}) or soup.find(class_=re.compile("author|byline", re.I))
              or "Person" in types or has_key(sd_all, "author"))
    has_date = bool(soup.find("time") or meta(prop="article:published_time") or '"datePublished"' in html)

    hreflang = [(l["hreflang"].strip(), urljoin(url, l["href"].strip())) for l in soup.find_all("link", hreflang=True, href=True)
                if "alternate" in (l.get("rel") or [])]
    verification = {"google_search_console": bool(soup.find("meta", attrs={"name": "google-site-verification"})),
                    "bing_webmaster": bool(soup.find("meta", attrs={"name": re.compile("^msvalidate.01$", re.I)})),
                    "google_analytics": bool(GA_RX.search(html))}
    mixed_active, mixed_passive = [], []
    if url.startswith("https://"):
        for tag, attr in (("script", "src"), ("iframe", "src"), ("link", "href")):
            for t in soup.find_all(tag, **{attr: re.compile(r"^http://", re.I)}):
                if tag != "link" or "stylesheet" in (t.get("rel") or []):
                    mixed_active.append(t[attr])
        for tag in soup.find_all(["img", "source", "video", "audio"]):
            src = tag.get("src") or (tag.get("srcset") or "").split(" ")[0]
            if src.lower().startswith("http://"):
                mixed_passive.append(src)
    generic, internal = [], []
    for a in soup.find_all("a", href=True):
        h = urljoin(url, a["href"].strip()).split("#")[0]
        if h.startswith("http") and on_host(h, host):
            internal.append(h)
            if GENERIC_ANCHOR.match(a.get_text(" ", strip=True)) and not a.get("aria-label"):
                generic.append((a.get_text(" ", strip=True), h))
    first_heads = [h.get_text(" ", strip=True) for h in soup.find_all(["h2", "h3", "h4", "strong", "b"], limit=8)]
    summary_box = any(SUMMARY_RX.search(t) and len(t) < 60 for t in first_heads) or bool(soup.find(
        attrs={"class": re.compile(r"tl-?dr|summary|takeaway|key-?points", re.I)}))

    scripts = len(soup.find_all("script"))
    for t in soup(["script", "style", "noscript", "template", "svg"]):
        t.decompose()
    imgs = soup.find_all("img")  # after decompose: <noscript> lazy-load duplicates are not double counted
    body_text = soup.get_text(" ", strip=True)
    words = body_text.split()
    img_info = []
    for i in imgs:
        src = i.get("src") or i.get("data-src") or i.get("data-lazy-src") or ""
        if not src or src.startswith("data:") or (i.get("width") in ("0", "1", "2") or i.get("height") in ("0", "1", "2")):
            continue
        pic = i.find_parent("picture")
        modern = bool(MODERN_IMG.search(src + " " + (i.get("srcset") or ""))) or bool(
            pic and pic.find("source", type=re.compile("webp|avif", re.I)))
        dims = bool(i.get("width") and i.get("height")) or "aspect-ratio" in (i.get("style") or "")
        img_info.append({"src": urljoin(url, src)[:300], "modern": modern, "dims": dims})

    out = {
        "url": url,
        "title": title or None,
        "meta_description": meta("description"),
        "h1": [t for n, t in headings if n == "h1"],
        "headings": headings[:60],
        "canonical": urljoin(url, canonical["href"]) if canonical and canonical.get("href") else None,
        "noindex": "noindex" in robots_meta or "noindex" in (rec.get("x_robots_tag") or "").lower(),
        "lang": soup.html.get("lang") if soup.html else None,
        "viewport": bool(soup.find("meta", attrs={"name": "viewport"})),
        "og_title": meta(prop="og:title"), "og_image": meta(prop="og:image"), "og_site_name": meta(prop="og:site_name"),
        "twitter_card": meta("twitter:card"),
        "word_count": len(words),
        "text_sample": " ".join(words[:400]),
        "numbers_in_text": len(STAT.findall(body_text)),
        "images": len(imgs), "images_no_alt": sum(1 for i in imgs if not (i.get("alt") or "").strip()),
        "links_internal": len(links_int), "links_external": len(links_ext),
        "external_links": sorted(links_ext)[:400],
        "lists": len(soup.find_all(["ul", "ol"])), "tables": len(soup.find_all("table")),
        "question_headings": questions, "answered_questions": answered,
        "schema_types": sorted(set(types)), "jsonld": sd.get("json-ld", []), "jsonld_errors": jsonld_errors,
        "has_author": bool(author), "has_date": has_date, "scripts": scripts,
        "date_published": has_key(sd_all, "datePublished"), "date_modified": has_key(sd_all, "dateModified"),
        "hreflang": hreflang, "verification": verification,
        "mixed_active": mixed_active[:20], "mixed_passive": mixed_passive[:20],
        "generic_anchors": generic[:20], "internal_links": sorted(set(internal))[:400],
        "img_info": img_info, "summary_box": summary_box,
        "text_full": " ".join(words[:2500]),
    }
    rec["_parsed"] = out
    return out


SOCIAL_HOSTS = ("linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com",
                "tiktok.com", "github.com", "wikipedia.org", "wikidata.org", "crunchbase.com")


def site_identity(crawl: dict) -> dict:
    """Best guess of brand name, description and official profiles from the homepage."""
    home = parse(crawl["pages"][0])
    def kind(n):  # 0 organization-like, 1 Person (personal site), 2 WebSite, 3 other
        ts = schema_types([{"@type": n.get("@type")}])
        return (0 if any(t.endswith(("Organization", "Business", "Store")) or t == "Corporation" for t in ts)
                else 1 if "Person" in ts else 2 if "WebSite" in ts else 3)
    org = min((n for n in jsonld_nodes(home["jsonld"]) if kind(n) < 3), key=kind, default={})
    title = home["title"] or ""
    name = (org.get("name") if isinstance(org.get("name"), str) else None) or home["og_site_name"]
    if not name:
        parts = [p.strip() for p in re.split(r"\s[|\-–—:·]\s", title) if p.strip()]
        name = min(parts, key=len) if parts else crawl["domain"].split(".")[0].title()
    same_as = sorted({u.split("?")[0] for u in home["external_links"]
                      if any(on_host(u, h) for h in SOCIAL_HOSTS)})
    return {"name": name, "url": crawl["base"] + "/", "title": title,
            "description": home["meta_description"] or "", "same_as": same_as[:10], "logo": home["og_image"]}


def site_kind(crawl: dict) -> str:
    """'shop', 'media', 'b2b' (SaaS / services / platform) or 'other' - checks like conversion pages, review profiles
    and content gaps only make sense for some kinds."""
    parsed = [parse(r) for r in crawl["pages"] if r.get("html")]
    paths = " ".join(urlparse(u).path for p in parsed for u in p["internal_links"])
    types = {t for p in parsed for t in p["schema_types"]}
    news = sum(1 for p in parsed if set(p["schema_types"]) & {"NewsArticle", "LiveBlogPosting"})
    if "NewsMediaOrganization" in types or news >= max(2, 0.3 * len(parsed)):
        return "media"
    cart = re.search(r"/(cart|basket|warenkorb|panier|checkout|korzina)(/|\b)", paths)
    shopify = re.search(r"/collections/", paths) and re.search(r"/products/[\w-]+", paths)
    if types & {"Store", "OnlineStore"} or shopify or cart and (types & {"Product", "ProductGroup", "Offer"} or re.search(r"/products?/", paths)):
        return "shop"
    if re.search(r"/(solutions|features|platform|developers|api|integrations|enterprise|use-cases|industries|channels|pricing|demo)(/|\b)", paths):
        return "b2b"
    return "other"


def html_pages(crawl: dict) -> list[tuple[dict, dict]]:
    """(raw record, parsed) for every page that returned HTML."""
    return [(r, parse(r)) for r in crawl["pages"] if r.get("html")]
