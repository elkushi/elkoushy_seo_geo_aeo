"""Google PageSpeed Insights / Core Web Vitals for a URL (mobile + desktop).

Free API. Keyless calls share a global quota that is often exhausted; set
PAGESPEED_API_KEY in .env. Each call takes 15-60 s. Backs off and retries on 429.
Returns findings for the report: slow LCP, layout shift (CLS), blocking JS (TBT),
low Lighthouse performance score.

Usage:
  python tools/pagespeed.py https://example.com
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import env, finding, passed  # noqa: E402

API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CATEGORIES = ["performance", "seo", "accessibility", "best-practices"]
METRICS = {"lcp_s": ("largest-contentful-paint", 1000), "fcp_s": ("first-contentful-paint", 1000),
           "tbt_ms": ("total-blocking-time", 1), "cls": ("cumulative-layout-shift", 1)}


def run(url: str, strategy: str, retries: int = 3) -> dict:
    params = [("url", url), ("strategy", strategy)] + [("category", c) for c in CATEGORIES]
    if env("PAGESPEED_API_KEY"):
        params.append(("key", env("PAGESPEED_API_KEY")))
    delay = 15
    for attempt in range(retries):
        try:
            r = requests.get(API, params=params, timeout=150)
        except requests.RequestException as e:
            if attempt == retries - 1:
                return {"error": type(e).__name__}
            time.sleep(delay)
            continue
        if r.status_code == 429 and attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code != 200:
            msg = r.json().get("error", {}).get("message", "") if "json" in r.headers.get("content-type", "") else ""
            return {"error": f"HTTP {r.status_code}: {msg[:160]}"}
        lh = r.json().get("lighthouseResult", {})
        cats, audits = lh.get("categories", {}), lh.get("audits", {})
        out = {c: round(cats[c]["score"] * 100) if cats.get(c, {}).get("score") is not None else None for c in CATEGORIES}
        for name, (audit, div) in METRICS.items():
            v = audits.get(audit, {}).get("numericValue")
            out[name] = round(v / div, 2) if v is not None else None
        out["opportunities"] = [a["title"] for a in audits.values()
                                if a.get("details", {}).get("type") == "opportunity" and (a.get("score") or 1) < 0.9][:6]
        return out
    return {"error": "quota exhausted - add PAGESPEED_API_KEY to .env"}


def audit(url: str) -> dict:
    with ThreadPoolExecutor(max_workers=2) as pool:
        mobile, desktop = pool.map(lambda s: run(url, s), ["mobile", "desktop"])
    F, P = [], []
    m = mobile
    if "error" in m:
        return {"findings": [], "passed": [], "metrics": {"mobile": mobile, "desktop": desktop}, "skipped": m["error"]}
    if m.get("performance") is not None and m["performance"] < 50:
        F.append(finding("SEO", "high", f"Poor mobile performance score ({m['performance']}/100)",
                         "Google ranks on mobile Core Web Vitals; slow pages also lose visitors before they load.",
                         "Top fixes from Lighthouse: " + ("; ".join(m["opportunities"]) or "compress images, defer JS, cache assets") + ".", [url], check="cwv"))
    elif m.get("performance") is not None and m["performance"] < 90:
        F.append(finding("SEO", "medium", f"Mobile performance could be better ({m['performance']}/100)",
                         "Core Web Vitals are a tie-breaker ranking signal and affect conversions.",
                         "Lighthouse suggests: " + ("; ".join(m["opportunities"]) or "optimize images and JavaScript") + ".", [url], check="cwv"))
    else:
        P.append(passed("SEO", f"Fast on mobile ({m.get('performance')}/100)", "cwv"))
    if m.get("lcp_s") and m["lcp_s"] > 2.5:
        F.append(finding("SEO", "medium", f"Slow Largest Contentful Paint ({m['lcp_s']} s on mobile)",
                         "Google's 'good' threshold is 2.5 s. LCP is usually the hero image or headline.",
                         "Preload and compress the hero image (WebP/AVIF, correct size), cut render-blocking CSS/JS, use a CDN.", [url], check="lcp"))
    if m.get("cls") and m["cls"] > 0.1:
        F.append(finding("SEO", "medium", f"Layout shift (CLS {m['cls']})",
                         "Content jumping while loading (threshold 0.1) annoys users and hurts Core Web Vitals.",
                         "Set width/height on images and embeds, and reserve space for banners and ads.", [url], check="cls"))
    if m.get("tbt_ms") and m["tbt_ms"] > 600:
        F.append(finding("SEO", "medium", f"Heavy JavaScript blocks the page ({round(m['tbt_ms'])} ms TBT)",
                         "Long main-thread work delays interaction (INP).",
                         "Remove unused JS, defer third-party scripts (chat widgets, trackers), split bundles.", [url], check="tbt"))
    return {"findings": F, "passed": P, "metrics": {"mobile": mobile, "desktop": desktop}}


if __name__ == "__main__":
    print(json.dumps(audit(sys.argv[1]), indent=2))
