"""Answer Engine Optimization checks: structured data + answer-ready content.

Answer engines (featured snippets, People Also Ask, voice assistants, AI Overviews)
lift short, clearly structured answers and lean on schema.org to understand pages.

Checks: JSON-LD validity, Organization/WebSite on the homepage, BreadcrumbList,
Article/Product/FAQPage where the page type calls for it, question-style headings,
concise answers under those headings, lists/tables.

Usage:
  python tools/aeo_audit.py .tmp/<domain>/crawl.json
Writes .tmp/<domain>/aeo.json
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import finding, passed, write_json  # noqa: E402
from page_parse import html_pages, jsonld_nodes, schema_types, site_identity, site_kind  # noqa: E402

A = "AEO"
ORG_TYPES = {"Organization", "Corporation", "LocalBusiness", "OnlineStore", "NGO", "ProfessionalService", "Restaurant",
             "Person", "Airline", "Hotel", "Dentist", "Bakery", "Physician", "Attorney", "LegalService", "FinancialService",
             "RealEstateAgent", "AutoDealer", "FoodEstablishment", "CafeOrCoffeeShop", "PerformingGroup", "MusicGroup",
             "Library", "Museum", "CollegeOrUniversity", "School", "Hospital", "MedicalClinic", "TravelAgency", "InsuranceAgency"}
ARTICLE_TYPES = {"Article", "BlogPosting", "LiveBlogPosting", "NewsArticle", "TechArticle", "Report", "ScholarlyArticle",
                 "AnalysisNewsArticle", "OpinionNewsArticle", "ReportageNewsArticle", "ReviewNewsArticle", "BackgroundNewsArticle"}
PRODUCT_TYPES = {"Product", "ProductGroup", "IndividualProduct", "ProductModel", "SomeProducts", "Vehicle", "Car"}


def is_org(types) -> bool:
    """Organization, any subtype (NewsMediaOrganization, ClothingStore, HomeAndConstructionBusiness ...) or Person (personal sites)."""
    return any(t in ORG_TYPES or t.endswith(("Organization", "Business", "Store")) for t in types)
# An article lives *under* a content section; the section hubs themselves (/resources/blog/, /blog/category/x/) are listings.
ARTICLE_URL = re.compile(r"/(blog|news|article|articles|insights?|posts?|guides?|resources|stories)/"
                         r"(?!(?:blog|news|articles?|insights?|posts?|guides?|resources|stories|case-studies|webinars|ebooks)/?$)"
                         r"(?!(?:category|categories|tag|tags|author|page)/).+", re.I)
PRODUCT_URL = re.compile(r"/(product|products|shop|item|p)/.+", re.I)
SAAS_URL = re.compile(r"/(products?|features|platform|channels|api|integrations)/[^/]+", re.I)  # not /solutions/ hubs
SOFTWARE_TYPES = {"SoftwareApplication", "WebApplication", "MobileApplication", "Service", "FinancialProduct"}
FAQ_URL = re.compile(r"faq|frequently-asked|help|support", re.I)


def org_snippet(ident: dict) -> str:
    data = {"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "@id": ident["url"] + "#org", "name": ident["name"], "url": ident["url"],
         "logo": ident["logo"] or ident["url"] + "logo.png", "description": ident["description"] or "...",
         "sameAs": ident["same_as"] or ["https://www.linkedin.com/company/...", "https://x.com/..."]},
        {"@type": "WebSite", "@id": ident["url"] + "#website", "url": ident["url"], "name": ident["name"],
         "publisher": {"@id": ident["url"] + "#org"}},
    ]}
    return '<script type="application/ld+json">\n' + json.dumps(data, indent=2, ensure_ascii=False) + "\n</script>"


def breadcrumb_snippet(url: str, base: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    items = [{"@type": "ListItem", "position": 1, "name": "Home", "item": base + "/"}]
    path = ""
    for i, p in enumerate(parts, start=2):
        path += "/" + p
        items.append({"@type": "ListItem", "position": i, "name": p.replace("-", " ").title(), "item": base + path})
    data = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": items}
    return '<script type="application/ld+json">\n' + json.dumps(data, indent=2) + "\n</script>"


def faq_snippet(questions: list[str]) -> str:
    data = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": "<40-60 word direct answer>"}}
        for q in questions[:4]]}
    return '<script type="application/ld+json">\n' + json.dumps(data, indent=2, ensure_ascii=False) + "\n</script>"


ARTICLE_SNIPPET = '''<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": "<page H1>",
  "datePublished": "2026-01-15",
  "dateModified": "2026-03-02",
  "author": {"@type": "Person", "name": "<Author>", "url": "<author bio page>"},
  "publisher": {"@id": "<homepage>#org"},
  "image": "<1200px wide image>"
}
</script>'''


def audit(crawl: dict) -> dict:
    F, P = [], []
    pages = html_pages(crawl)
    _, home = pages[0]
    ident = site_identity(crawl)
    base = crawl["base"]
    all_types = sorted({t for _, p in pages for t in p["schema_types"]})

    broken = [p["url"] for _, p in pages if p["jsonld_errors"]]
    if broken:
        F.append(finding(A, "high", "Invalid JSON-LD (structured data does not parse)",
                         "Search and answer engines silently ignore structured data that has syntax errors.",
                         "Fix the JSON (trailing commas, unescaped quotes) and validate at validator.schema.org / Google's Rich Results Test.", broken, check="jsonld"))

    if not is_org(home["schema_types"]):
        F.append(finding(A, "high", "No Organization schema on the homepage",
                         "Organization markup tells Google's Knowledge Graph and LLMs who you are, your logo and official profiles. It is the base of entity recognition.",
                         "Add this JSON-LD to the homepage <head> (fill in the gaps).", [home["url"]], org_snippet(ident), check="org"))
    else:
        P.append(passed(A, "Organization (or Person) schema on the homepage", "org"))
        org_node = next((n for n in jsonld_nodes(home["jsonld"]) if is_org(schema_types([{"@type": n.get("@type")}]))), {})
        same = org_node.get("sameAs") or []
        missing = [k for k, ok in (("name", org_node.get("name")), ("url", org_node.get("url")), ("logo", org_node.get("logo")),
                                   ("sameAs (2+ profiles)", len(same if isinstance(same, list) else [same]) >= 2),
                                   ("contactPoint or address", org_node.get("contactPoint") or org_node.get("address")),
                                   ("foundingDate", org_node.get("foundingDate"))) if not ok]
        if org_node and "Person" not in schema_types([{"@type": org_node.get("@type")}]) and missing:
            merged = {k: v for k, v in org_node.items() if not k.startswith("@") or k in ("@type", "@id")}
            merged = {"@context": "https://schema.org", **merged}
            merged.setdefault("name", ident["name"])
            merged.setdefault("url", ident["url"])
            merged.setdefault("logo", ident["logo"] or ident["url"] + "logo.png")
            if len(same if isinstance(same, list) else [same]) < 2:
                merged["sameAs"] = sorted(set((same if isinstance(same, list) else [same]) + ident["same_as"]))[:10] or [
                    "https://www.linkedin.com/company/...", "https://www.crunchbase.com/organization/..."]
            merged.setdefault("contactPoint", {"@type": "ContactPoint", "contactType": "sales", "email": "sales@...", "url": base + "/contact"})
            merged.setdefault("address", {"@type": "PostalAddress", "streetAddress": "...", "addressLocality": "...", "addressCountry": "..."})
            merged.setdefault("foundingDate", "YYYY")
            F.append(finding(A, "medium" if len([m for m in missing if m != "foundingDate"]) >= 2 else "low", "Organization schema is incomplete",
                             "Missing: " + ", ".join(missing) + ". These properties are what Google's Knowledge Panel and LLMs use to "
                             "identify the company, link it to its profiles and trust it.",
                             "Extend the existing Organization JSON-LD (your current values are kept below; replace the ... placeholders).",
                             [home["url"]], '<script type="application/ld+json">\n' + json.dumps(merged, indent=2, ensure_ascii=False)[:3500] + "\n</script>",
                             check="org_complete"))
        elif org_node and not missing:
            P.append(passed(A, "Organization schema complete (logo, sameAs, contact, founding date)", "org_complete"))
    if "WebSite" not in home["schema_types"] and is_org(home["schema_types"]):
        F.append(finding(A, "low", "No WebSite schema", "WebSite markup confirms the site name shown in Google results.",
                         "Add a WebSite node (included in the Organization snippet above).", [home["url"]], check="website"))

    inner = [(r, p) for r, p in pages[1:] if urlparse(p["url"]).path.strip("/")]
    no_crumb = [p["url"] for _, p in inner if "BreadcrumbList" not in p["schema_types"]]
    if inner and len(no_crumb) / len(inner) > 0.5:
        F.append(finding(A, "medium", "Inner pages lack BreadcrumbList schema",
                         "Breadcrumbs show your site hierarchy in results and help engines understand how pages relate.",
                         "Add BreadcrumbList JSON-LD to every inner page (your CMS/SEO plugin can generate it).",
                         no_crumb, breadcrumb_snippet(no_crumb[0], base), check="breadcrumb"))
    elif inner:
        P.append(passed(A, "Breadcrumb schema on inner pages", "breadcrumb"))

    articles = [p for _, p in pages if ARTICLE_URL.search(urlparse(p["url"]).path)]
    no_article = [p["url"] for p in articles if not set(p["schema_types"]) & ARTICLE_TYPES]
    if no_article:
        F.append(finding(A, "medium", "Articles without Article/BlogPosting schema",
                         "Article markup exposes headline, author and dates, which engines use to judge freshness and expertise.",
                         "Add BlogPosting JSON-LD to each article.", no_article, ARTICLE_SNIPPET, check="article"))
    elif articles:
        P.append(passed(A, "Article schema on blog/article pages", "article"))

    kind = site_kind(crawl)
    valid = PRODUCT_TYPES | SOFTWARE_TYPES
    if kind == "b2b":  # platform / SaaS: product & channel pages are software or services, not shop products
        saas = [p for _, p in pages if SAAS_URL.search(urlparse(p["url"]).path) and not ARTICLE_URL.search(urlparse(p["url"]).path)]
        no_sw = [p for p in saas if not set(p["schema_types"]) & valid]
        if no_sw:
            first = no_sw[0]
            name = (first["h1"] or [first["title"] or "Product"])[0][:80]
            snip = {"@context": "https://schema.org", "@type": "SoftwareApplication", "name": name,
                    "applicationCategory": "BusinessApplication", "operatingSystem": "Web",
                    "description": first["meta_description"] or "...", "url": first["url"],
                    "provider": {"@id": base + "/#org"},
                    "offers": {"@type": "Offer", "url": base + "/pricing", "priceCurrency": "EUR", "price": "0", "description": "Pricing on request"}}
            F.append(finding(A, "medium", f"{len(no_sw)} product/feature page(s) without SoftwareApplication or Service schema",
                             "These pages describe what you sell, but nothing tells search and AI engines it is a product: name, category, provider, "
                             "and offer. That structured description is what gets quoted when buyers ask AI 'which tool does X'.",
                             "Add SoftwareApplication (software/API products) or Service (managed services) JSON-LD to each one, linked to your "
                             "Organization via provider @id. Remove the offers block if you have no public price.",
                             [p["url"] for p in no_sw], '<script type="application/ld+json">\n' + json.dumps(snip, indent=2, ensure_ascii=False) + "\n</script>",
                             check="software_schema"))
        elif saas:
            P.append(passed(A, f"Product/feature pages have product schema ({len(saas)})", "software_schema"))
    else:
        products = [p for _, p in pages if PRODUCT_URL.search(urlparse(p["url"]).path)]
        no_product = [p["url"] for p in products if not set(p["schema_types"]) & valid]
        if no_product:
            F.append(finding(A, "high", "Product pages without Product schema",
                             "Product markup (price, availability, reviews) powers rich results, Google Shopping and AI shopping answers.",
                             "Add Product + Offer JSON-LD (name, image, price, priceCurrency, availability, aggregateRating if real).", no_product, check="product"))

    # --- from the checklist: dates, FAQ hub, summary boxes ---------------------------
    art_pages = [p for _, p in pages if set(p["schema_types"]) & ARTICLE_TYPES]
    no_mod = [p["url"] for p in art_pages if not p["date_modified"]]
    if no_mod:
        F.append(finding(A, "low", "Article schema without dateModified",
                         "Search and AI engines prefer fresh sources and read the date from the markup. Without dateModified, an updated "
                         "article looks as old as its first publication.",
                         "Add datePublished and dateModified to the Article/BlogPosting JSON-LD, and update dateModified when you refresh content.",
                         no_mod, '"datePublished": "2026-01-15T09:00:00+00:00",\n"dateModified": "2026-10-20T09:00:00+00:00"', check="date_modified"))
    elif art_pages:
        P.append(passed(A, "Articles carry dateModified", "date_modified"))

    known = [p["url"] for _, p in pages] + crawl.get("home_links", []) + home["internal_links"] + [
        e["loc"] for e in crawl["sitemaps"]["entries"][:5000]]
    if kind in ("b2b", "shop", "other") and not any(
            re.search(r"/(faqs?|frequently-asked[\w-]*|help-?center|help|support|hilfe|aide|ayuda|voprosy)(/|$)", urlparse(u).path, re.I)
            or re.match(r"(help|support|faq|docs|hilfe)\.", urlparse(u).hostname or "") for u in known):
        F.append(finding(A, "low", "No FAQ / help hub found",
                         "A dedicated FAQ page that answers the real pre-sales questions (price, setup, integrations, security, comparisons) "
                         "is one of the most quoted page types in answer boxes and AI answers.",
                         "Create /faq with 15-30 questions grouped by topic, each answered in 40-60 words first, and FAQPage JSON-LD. "
                         "Collect the questions from sales calls, support tickets and Google's 'People also ask'.", [], check="faq_page"))

    long_pages = [p for _, p in pages if p["word_count"] >= 1200 and (ARTICLE_URL.search(urlparse(p["url"]).path)
                                                                     or set(p["schema_types"]) & ARTICLE_TYPES)]
    no_tldr = [p["url"] for p in long_pages if not p["summary_box"]]
    if no_tldr:
        F.append(finding(A, "low", f"{len(no_tldr)} long article(s) without a summary box",
                         "Long articles (1,200+ words) with no TL;DR / key takeaways near the top force engines to guess the main answer; "
                         "a short summary is often what gets lifted into snippets and AI answers.",
                         "Add a 3-5 bullet 'Key takeaways' box right under the H1, with the core answer and numbers.", no_tldr,
                         '<aside class="key-takeaways">\n  <h2>Key takeaways</h2>\n  <ul>\n    <li>...</li>\n  </ul>\n</aside>', check="tldr"))

    faq_like = [p for _, p in pages if FAQ_URL.search(urlparse(p["url"]).path)
                or (len(p["question_headings"]) >= 3 and not ARTICLE_URL.search(urlparse(p["url"]).path))]
    no_faq = [p for p in faq_like if "FAQPage" not in p["schema_types"] and p["question_headings"]]
    if no_faq:
        F.append(finding(A, "medium", "Q&A content without FAQPage schema",
                         "These pages already contain question headings. Google now limits FAQ rich results to authoritative sites, "
                         "but FAQ markup still gives AI engines clean question→answer pairs to quote.",
                         "Wrap the existing Q&A in FAQPage JSON-LD (the answer text must match what is visible on the page).",
                         [p["url"] for p in no_faq], faq_snippet(no_faq[0]["question_headings"]), check="faq"))

    q_pages = [p for _, p in pages if p["question_headings"]]
    total_q = sum(len(p["question_headings"]) for p in q_pages)
    answered = sum(p["answered_questions"] for p in q_pages)
    if not q_pages:
        F.append(finding(A, "high", "No question-style headings anywhere",
                         "Answer engines match user questions to headings like \"How does X work?\". None of the crawled pages use them, so there is nothing to lift into a featured snippet or AI answer.",
                         "On key pages, add H2s phrased as the real questions customers ask (see 'People also ask' for your keywords), each followed by a 40-60 word direct answer, then detail.",
                         [p["url"] for _, p in pages[:5]], check="questions",
                         snippet="<h2>How much does <product> cost?</h2>\n<p><Product> costs <price> per <unit>. The price includes <x, y, z>. "
                                 "Volume discounts start at <n>. [40-60 words, answer first, no fluff]</p>"))
    elif total_q and answered / total_q < 0.5:
        F.append(finding(A, "medium", "Question headings are not followed by concise answers",
                         f"Only {answered} of {total_q} question headings are followed by a short (15-90 word) answer block. Engines prefer answer-first paragraphs.",
                         "Start the text under each question with a 1-3 sentence direct answer, then expand.", [p["url"] for p in q_pages], check="answers"))
    else:
        P.append(passed(A, f"Question headings with direct answers ({answered}/{total_q})", "answers"))

    structured = [p for _, p in pages if p["lists"] or p["tables"]]
    if len(structured) < len(pages) / 3:
        F.append(finding(A, "low", "Little list/table formatting",
                         "Steps, comparisons and specs in <ol>/<ul>/<table> are lifted directly into list and table snippets.",
                         "Turn processes into numbered lists and comparisons/specs into HTML tables.", [p["url"] for _, p in pages if not (p["lists"] or p["tables"])], check="format"))

    metrics = {"schema_types_found": all_types, "site_kind": kind,
               "pages_with_schema": sum(1 for _, p in pages if p["schema_types"]),
               "question_headings": total_q, "answered_questions": answered,
               "sample_questions": [q for p in q_pages for q in p["question_headings"]][:8]}
    return {"findings": F, "passed": P, "metrics": metrics}


def main() -> int:
    crawl = json.loads(Path(sys.argv[1]).read_text())
    res = audit(crawl)
    out = write_json(Path(sys.argv[1]).with_name("aeo.json"), res)
    print(f"{len(res['findings'])} findings -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
