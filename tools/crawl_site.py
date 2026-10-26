"""Crawl a site sample: homepage, robots.txt, sitemap(s), llms.txt and up to N pages.

Pages are picked from the homepage's internal links first (what users and bots
reach first), then topped up from the sitemap. Fetching is polite: 4 workers,
robots.txt `User-agent: *` disallows are respected.

Usage:
  python tools/crawl_site.py https://example.com --max-pages 25
Writes .tmp/<domain>/crawl.json (page HTML included, so other tools need no refetch).
"""

import argparse
import gzip
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import FetchError, domain_of, fetch, is_public_url, normalize_url, robots_allowed, robots_groups, run_dir, session, write_json  # noqa: E402

SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|zip|mp4|mp3|css|js|xml|ico|woff2?)$", re.I)
# Login / cart / account pages are thin and noindex by design; auditing them only produces noise
UTILITY = re.compile(r"/(log-?in|log-?out|sign-?in|sign-?up|register|account|my-account|cart|basket|checkout|password|auth|"
                     r"wp-login\.php|wp-admin|search|suche|recherche|buscar)(/|$|\?)|[?&](add-to-cart|redirect_to|return_to)=", re.I)
BLOCK_HINT = {401: " - login required", **dict.fromkeys((402, 403, 405, 406), " - the site's firewall / bot protection refused the audit crawler"),
              429: " - rate-limited by the site", 503: " - bot protection or an outage refused the request"}
ERROR_HINT = {"SSLError": "the TLS connection was dropped (network problem, bot protection or a broken certificate)",
              "ReadTimeout": "the site did not answer in time", "ConnectTimeout": "the site did not accept a connection in time",
              "ConnectionError": "the site could not be reached (DNS or connection refused)",
              "TooManyRedirects": "too many redirects"}


def same_site(url: str, host: str) -> bool:
    return domain_of(url) == host


def internal_links(html: str, base_url: str, host: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        raw = a["href"].strip()
        if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        u = urljoin(base_url, raw).split("#")[0]
        if u.startswith("http") and same_site(u, host) and not SKIP_EXT.search(urlparse(u).path):
            out.append(u)
    return list(dict.fromkeys(out))


def read_sitemaps(sess, base: str, robots_text: str, limit_maps: int = 8) -> dict:
    declared = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots_text or "")
    queue = list(dict.fromkeys(declared + [urljoin(base, "/sitemap.xml"), urljoin(base, "/sitemap_index.xml")]))
    seen, found_maps, entries, failed = [], [], [], []
    while queue and len(seen) < limit_maps:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.append(sm)
        try:
            r = fetch(sess, sm, attempts=1)
        except FetchError as e:
            failed.append({"url": sm, "status": None, "error": str(e)[:120]})
            continue
        if r.status_code != 200:
            failed.append({"url": sm, "status": r.status_code})
            continue
        content = r.content
        if sm.endswith(".gz"):
            try:
                content = gzip.decompress(content)
            except OSError:
                continue
        soup = BeautifulSoup(content, "xml")
        if not soup.find(["urlset", "sitemapindex"]):
            continue
        found_maps.append(sm)
        for child in soup.find_all("sitemap"):
            loc = child.find("loc")
            if loc:
                queue.append(loc.text.strip())
        for u in soup.find_all("url"):
            loc, mod = u.find("loc"), u.find("lastmod")
            if loc:
                entries.append({"loc": loc.text.strip(), "lastmod": mod.text.strip()[:10] if mod else None})
    return {"declared_in_robots": declared, "maps_found": found_maps, "url_count": len(entries), "entries": entries, "failed": failed}


def page_record(sess, url: str) -> dict:
    rec = {"url": url}
    try:
        r = fetch(sess, url)
    except FetchError as e:
        rec["error"] = str(e)
        return rec
    rec.update({
        "final_url": r.url, "status": r.status_code,
        "redirects": [h.url for h in r.history], "elapsed_ms": round(r.elapsed.total_seconds() * 1000),
        "content_type": r.headers.get("content-type", ""), "x_robots_tag": r.headers.get("x-robots-tag"),
        "bytes": len(r.content),
    })
    if r.status_code == 200 and "html" in rec["content_type"]:
        rec["html"] = r.text
    return rec


def crawl(url: str, max_pages: int = 25, progress=None) -> dict:
    url = normalize_url(url)
    if not is_public_url(url):
        raise ValueError("this URL does not resolve to a public address")
    sess = session()
    say = progress or (lambda m: print(m, file=sys.stderr))

    # A site that names our crawler and disallows it gets no request beyond robots.txt (promised on /bot).
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    try:
        pre = fetch(sess, origin + "/robots.txt", attempts=2)
        pre_text = pre.text if pre.status_code == 200 and "html" not in pre.headers.get("content-type", "") else ""
    except FetchError:
        pre_text = ""
    if "seo-geo-aeo-audit" in robots_groups(pre_text) and not robots_allowed(pre_text, "SEO-GEO-AEO-Audit", url):
        raise FetchError("this site's robots.txt asks our crawler (SEO-GEO-AEO-Audit) not to visit it, so no audit was run")

    say("Fetching homepage")
    home = page_record(sess, url)
    redirect_loop = None
    if "html" not in home and "TooManyRedirects" in (home.get("error") or ""):
        # Often a CDN that cached a redirect pointing at itself. Record the hop, then audit via a cache-busting URL.
        try:
            hop = sess.get(url, allow_redirects=False, timeout=15)
            redirect_loop = {"url": url, "status": hop.status_code, "location": hop.headers.get("location"),
                             "cache": hop.headers.get("cache") or hop.headers.get("x-cache") or hop.headers.get("cf-cache-status")}
        except requests.RequestException:
            redirect_loop = {"url": url}
        say("Homepage is in a redirect loop - continuing with a cache-busting URL")
        home = page_record(sess, url + ("&" if "?" in url else "?") + "audit_nocache=1")
        if "html" in home:
            home["final_url"] = home["final_url"].split("?")[0]
    if "html" not in home:
        st = home.get("status")
        err = home.get("error") or ""
        err_hint = next((h for k, h in ERROR_HINT.items() if err.startswith(k)), err[:160])
        why = err and err_hint or (f"not an HTML page ({home.get('content_type') or 'no content type'})" if st == 200
                                    else f"HTTP {st}{BLOCK_HINT.get(st, '')}")
        raise FetchError(f"homepage could not be loaded ({why})"
                         + (" - the site redirects to itself in an endless loop" if redirect_loop else ""))
    final = home["final_url"]
    base = f"{urlparse(final).scheme}://{urlparse(final).netloc}"
    host = domain_of(final)

    say("Reading robots.txt, sitemap and llms.txt")
    robots_text, robots_status = "", None
    try:
        rr = fetch(sess, urljoin(base, "/robots.txt"))
        robots_status = rr.status_code
        if rr.status_code == 200 and "html" not in rr.headers.get("content-type", ""):
            robots_text = rr.text
    except FetchError:
        pass
    llms = {}
    for name in ("/llms.txt", "/llms-full.txt"):
        try:
            lr = fetch(sess, urljoin(base, name), attempts=1)
            ok = lr.status_code == 200 and "html" not in lr.headers.get("content-type", "") and len(lr.text.strip()) > 20
            llms[name] = lr.text[:4000] if ok else None
        except FetchError:
            llms[name] = None
    sitemaps = read_sitemaps(sess, base, robots_text)

    candidates = internal_links(home["html"], final, host)
    candidates += [e["loc"] for e in sitemaps["entries"] if same_site(e["loc"], host)]
    picked = []
    candidates = [re.sub(r"[?&]audit_nocache=1$", "", c) for c in candidates]  # pages echo our cache-buster back
    for c in dict.fromkeys(candidates):
        if c.rstrip("/") in (url.rstrip("/"), final.rstrip("/")) or SKIP_EXT.search(urlparse(c).path) or UTILITY.search(c):
            continue
        if not robots_allowed(robots_text, "SEO-GEO-AEO-Audit", c):
            continue
        picked.append(c)
        if len(picked) >= max_pages - 1:
            break

    try:  # the first request paid DNS + TLS setup; re-time the homepage on the warm connection
        home["elapsed_ms"] = min(home["elapsed_ms"], round(fetch(sess, final, attempts=1).elapsed.total_seconds() * 1000))
    except FetchError:
        pass

    say(f"Crawling {len(picked) + 1} page" + ("s" if picked else ""))
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = [home] + list(pool.map(lambda u: page_record(sess, u), picked))

    return {
        "input_url": url, "final_url": final, "base": base, "domain": host, "redirect_loop": redirect_loop,
        "robots": {"status": robots_status, "text": robots_text[:20000]},
        "llms_txt": llms, "sitemaps": sitemaps, "pages": pages,
        "home_links": internal_links(home["html"], final, host)[:300],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url")
    ap.add_argument("--max-pages", type=int, default=25)
    args = ap.parse_args()
    data = crawl(args.url, args.max_pages)
    out = write_json(run_dir(data["final_url"]) / "crawl.json", data)
    print(f"{len(data['pages'])} pages, {data['sitemaps']['url_count']} sitemap URLs -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
