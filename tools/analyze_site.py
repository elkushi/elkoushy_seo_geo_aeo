"""Full SEO + AEO + GEO analysis of one website -> HTML report + Markdown action plan.

Pipeline: crawl_site -> seo_audit, aeo_audit, geo_audit, pagespeed (parallel),
geo_visibility (LLM, optional) -> score -> prioritise -> render.

Usage:
  python tools/analyze_site.py https://example.com [--max-pages 25] [--no-pagespeed] [--no-llm]
Writes .tmp/<domain>/{crawl,findings}.json, report.html, action_plan.md
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aeo_audit  # noqa: E402
import geo_audit  # noqa: E402
import geo_visibility  # noqa: E402
import llm_client  # noqa: E402
import pagespeed  # noqa: E402
import seo_audit  # noqa: E402
from common import AREAS, ROOT, SEVERITY_WEIGHT, FetchError, run_dir, write_json  # noqa: E402
from crawl_site import crawl  # noqa: E402

SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
# Checks fixable in an afternoon (template / config / snippet changes)
QUICK = {"https", "title", "meta_description", "h1", "canonical", "lang", "viewport", "og", "robots", "sitemap",
         "org", "website", "breadcrumb", "faq", "article", "jsonld", "ai_robots", "ai_live", "llms_txt", "sameas", "alt", "redirect_loop",
         "hreflang", "mixed_content", "img_dims", "verification", "org_complete", "software_schema", "date_modified"}
# Checks that are about authority / content, i.e. a longer programme
LONG = {"llm_recall", "llm_share", "ai_sources", "ai_cited", "wikidata", "stats", "sources", "author", "freshness",
        "thin", "questions", "answers", "about", "content_gaps", "review_profiles"}
AREA_INFO = {
    "SEO": ("Search Engine Optimization", "Can Google and Bing crawl, index and rank the site?"),
    "AEO": ("Answer Engine Optimization", "Is content structured so engines can lift direct answers (snippets, PAA, voice, AI Overviews)?"),
    "GEO": ("Generative Engine Optimization", "Can ChatGPT, Perplexity, Claude and Gemini reach, trust and cite the site?"),
}


def score(findings: list[dict], area: str) -> int:
    penalty = sum(SEVERITY_WEIGHT[f["severity"]] for f in findings if f["area"] == area)
    return max(10, 100 - penalty)


def grade(s: int) -> str:
    return "A" if s >= 90 else "B" if s >= 75 else "C" if s >= 60 else "D" if s >= 40 else "E"


def roadmap(findings: list[dict]) -> dict:
    now, month, quarter = [], [], []
    for f in findings:
        if f["check"] in LONG:
            quarter.append(f)
        elif f["check"] in QUICK and f["severity"] != "low" or f["severity"] == "high" and f["check"] not in LONG:
            now.append(f)
        else:
            month.append(f)
    return {"Week 1 - quick wins": now, "Next 30 days": month, "Next 90 days - authority & content": quarter}


def summary(site: str, scores: dict, findings: list[dict], use_llm: bool = True) -> str:
    top = [f for f in findings if f["severity"] == "high"][:6] or findings[:6]
    if use_llm and llm_client.available():
        try:
            return llm_client.ask(
                f"You are a senior SEO/GEO consultant. Write a 4-5 sentence executive summary for the owner of {site}. "
                f"Scores out of 100: SEO {scores['SEO']}, AEO {scores['AEO']}, GEO {scores['GEO']}. "
                "Biggest issues: " + "; ".join(f["title"] for f in top) + ". "
                "Plain language, no bullet points, no markdown, end with the single most valuable next step.",
                temperature=0.4, max_tokens=400)
        except Exception:
            pass
    weakest = min(AREAS, key=lambda a: scores[a])
    issues = ", ".join(f["title"].lower() for f in top[:3])
    return (f"{site} scores {scores['overall']}/100 overall. The weakest area is {weakest} ({scores[weakest]}/100). "
            f"The most important problems are: {issues}. Fixing the Week 1 items below is mostly template and configuration work "
            "and removes the biggest blockers; the 90-day items build the authority that gets a brand recommended by AI engines.")


def optional(fn, *args, **kw) -> dict:
    """Run an optional step (PageSpeed, LLM visibility); a failure becomes a 'skipped' note, not a failed report."""
    try:
        return fn(*args, **kw)
    except Exception as e:
        return {"findings": [], "passed": [], "metrics": {}, "skipped": f"failed: {type(e).__name__}: {str(e)[:160]}"}


def analyze(url: str, max_pages: int = 25, use_pagespeed: bool = True, use_llm: bool = True, progress=None,
            keep_crawl: bool = True, make_pdf: bool = False) -> dict:
    say = progress or (lambda m: print(m, file=sys.stderr))
    data = crawl(url, max_pages, progress=say)
    out_dir = run_dir(data["final_url"])
    write_json(out_dir / "crawl.json", data)

    with ThreadPoolExecutor(max_workers=2) as pool:
        ps_future = pool.submit(optional, pagespeed.audit, data["final_url"]) if use_pagespeed else None
        say("Auditing SEO")
        seo = seo_audit.audit(data)
        say("Auditing AEO (structured data, answers)")
        aeo = aeo_audit.audit(data)
        say("Auditing GEO (AI crawlers, entity, citability)")
        geo = geo_audit.audit(data)
        vis = optional(geo_visibility.audit, data, progress=say) if use_llm else {"findings": [], "passed": [], "metrics": {}, "skipped": "disabled"}
        if ps_future:
            say("Waiting for Google PageSpeed (Core Web Vitals)")
        ps = ps_future.result() if ps_future else {"findings": [], "passed": [], "metrics": {}, "skipped": "disabled"}

    parts = {"seo": seo, "aeo": aeo, "geo": geo, "pagespeed": ps, "visibility": vis}
    findings = sorted([f for p in parts.values() for f in p["findings"]], key=lambda f: (SEV_ORDER[f["severity"]], -f["affected"]))
    checks_ok = [c for p in parts.values() for c in p["passed"]]
    scores = {a: score(findings, a) for a in AREAS}
    scores["overall"] = round(sum(scores[a] for a in AREAS) / len(AREAS))

    say("Writing the report")
    report = {
        "site": data["domain"], "url": data["final_url"], "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "scores": scores, "grades": {k: grade(v) for k, v in scores.items()},
        "summary": summary(data["domain"], scores, findings, use_llm),
        "findings": findings, "passed": checks_ok, "roadmap": roadmap(findings),
        "counts": {s: sum(1 for f in findings if f["severity"] == s) for s in SEV_ORDER},
        "metrics": {k: v.get("metrics", {}) for k, v in parts.items()},
        "skipped": {k: v["skipped"] for k, v in parts.items() if v.get("skipped")},
        "area_info": AREA_INFO, "llm_model": llm_client.model_name() if llm_client.available() and use_llm else None,
    }
    write_json(out_dir / "findings.json", report)
    (out_dir / "report.html").write_text(render(report), encoding="utf-8")
    (out_dir / "action_plan.md").write_text(action_plan_md(report), encoding="utf-8")
    if not keep_crawl:  # page HTML is 2-40 MB per site; the web app only needs the report
        (out_dir / "crawl.json").unlink(missing_ok=True)
    report["paths"] = {"dir": str(out_dir), "html": str(out_dir / "report.html"), "md": str(out_dir / "action_plan.md")}
    if make_pdf:
        say("Creating the PDF")
        try:
            from render_pdf import html_to_pdf
            html_to_pdf(out_dir / "report.html", out_dir / "report.pdf", f"{data['domain']} - SEO · AEO · GEO report")
            report["paths"]["pdf"] = str(out_dir / "report.pdf")
        except Exception as e:  # PDF is a convenience; never lose the report over it
            report["skipped"]["pdf"] = f"failed: {type(e).__name__}: {str(e)[:160]}"
    return report


def render(report: dict) -> str:
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html", "j2"]))
    return env.get_template("report.html.j2").render(r=report)


def action_plan_md(r: dict) -> str:
    L = [f"# SEO / AEO / GEO action plan: {r['site']}", "", f"Generated {r['generated']} - {r['url']}", "",
         f"**Scores:** overall {r['scores']['overall']} · SEO {r['scores']['SEO']} · AEO {r['scores']['AEO']} · GEO {r['scores']['GEO']}", "",
         r["summary"], ""]
    for phase, items in r["roadmap"].items():
        if not items:
            continue
        L += [f"## {phase}", ""]
        for f in items:
            L += [f"### [{f['severity'].upper()}] [{f['area']}] {f['title']}", "", f["detail"], "", f"**Fix:** {f['fix']}", ""]
            if f["urls"]:
                L += ["Affected: " + ", ".join(f["urls"][:5]) + (f" (+{f['affected'] - 5} more)" if f["affected"] > 5 else ""), ""]
            if f["snippet"]:
                L += ["```", f["snippet"], "```", ""]
    L += ["## Already in good shape", ""] + [f"- [{p['area']}] {p['title']}" for p in r["passed"]]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url")
    ap.add_argument("--max-pages", type=int, default=25)
    ap.add_argument("--no-pagespeed", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--pdf", action="store_true", help="also write report.pdf (needs playwright + chromium)")
    a = ap.parse_args()
    try:
        r = analyze(a.url, a.max_pages, not a.no_pagespeed, not a.no_llm, make_pdf=a.pdf)
    except (FetchError, ValueError) as e:
        print(f"Could not analyze {a.url}: {e}", file=sys.stderr)
        return 2
    print(f"\n{r['site']}: overall {r['scores']['overall']} (SEO {r['scores']['SEO']}, AEO {r['scores']['AEO']}, GEO {r['scores']['GEO']})")
    print(f"{len(r['findings'])} findings, {len(r['passed'])} checks passed")
    for k, v in r["skipped"].items():
        print(f"skipped {k}: {v}")
    print(f"Report: {r['paths']['html']}\nPlan:   {r['paths']['md']}" + (f"\nPDF:    {r['paths']['pdf']}" if "pdf" in r["paths"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
