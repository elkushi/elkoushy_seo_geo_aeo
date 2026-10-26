"""AI visibility check: does an LLM mention / cite this brand for buyer questions?

Needs NVIDIA_API_KEY (free). Skipped cleanly without it. About 8 LLM calls per run.

1. From the homepage text the LLM infers the category and 3 realistic buyer
   questions that do NOT name the brand ("best X for Y", "how do I ...").
2. Recall mode: brand question + the 3 buyer questions answered from model memory
   (what models "know" without search).
3. Search mode: the same buyer questions answered from live DuckDuckGo results
   (approximates Perplexity / ChatGPT search / AI Overviews: is the site among the
   sources, and does the answer cite it?).

Usage:
  python tools/geo_visibility.py .tmp/<domain>/crawl.json
Writes .tmp/<domain>/geo_visibility.json
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_client  # noqa: E402
from common import finding, passed, write_json  # noqa: E402
from page_parse import parse, site_identity  # noqa: E402

A = "GEO"
UNKNOWN = re.compile(r"(not (familiar|aware)|no (specific |reliable )?information|don'?t have (any )?(specific )?information|"
                     r"couldn'?t find|unable to find|not sure what|no knowledge)", re.I)


def extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def mentions(text: str, brand: str, domain: str) -> bool:
    t = text.lower()
    return brand.lower() in t or domain.lower() in t or domain.split(".")[0].lower() in re.findall(r"[a-z0-9-]+", t)


def search(q: str, n: int = 8) -> list[dict]:
    try:
        from ddgs import DDGS
        return [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")} for r in DDGS().text(q, max_results=n)]
    except Exception:
        return []


def audit(crawl: dict, progress=None) -> dict:
    say = progress or (lambda m: print(m, file=sys.stderr))
    if not llm_client.available():
        return {"findings": [], "passed": [], "metrics": {}, "skipped": "NVIDIA_API_KEY not set - AI visibility check skipped"}
    ident = site_identity(crawl)
    brand, domain = ident["name"], crawl["domain"]
    home = parse(crawl["pages"][0])

    say("AI: understanding the business")
    profile = extract_json(llm_client.ask(
        f"Website: {crawl['final_url']}\nTitle: {home['title']}\nDescription: {home['meta_description']}\n"
        f"Homepage text: {home['text_sample'][:2500]}\n\n"
        "Return JSON only: {\"category\": \"<what they sell, 3-6 words>\", \"audience\": \"<who buys>\", "
        "\"location\": \"<country/city if local, else empty>\", "
        "\"questions\": [3 questions a potential customer would type into ChatGPT when looking for this kind of product/service, "
        "WITHOUT naming the brand], \"competitors\": [up to 5 well-known competitor brand names]}",
        temperature=0.2, max_tokens=600)) or {}
    questions = [q for q in profile.get("questions", []) if isinstance(q, str)][:3]
    if not questions:
        return {"findings": [], "passed": [], "metrics": {"profile": profile}, "skipped": "LLM did not return usable questions"}

    say("AI: asking what the model knows about the brand")
    recall_brand = llm_client.ask(f"What is {brand} ({domain})? Answer in 3-4 sentences. If you do not know it, say so.", temperature=0.2, max_tokens=300)
    known = not UNKNOWN.search(recall_brand) and mentions(recall_brand, brand, domain)

    runs = []
    for i, q in enumerate(questions, 1):
        say(f"AI: buyer question {i}/{len(questions)}")
        recall = llm_client.ask(q + "\nName specific companies, products or websites.", temperature=0.5, max_tokens=700)
        results = search(q)
        in_results = [r["url"] for r in results if domain in r["url"]]
        sources = "\n".join(f"[{n}] {r['title']} - {r['url']}\n{r['snippet']}" for n, r in enumerate(results, 1))
        grounded = llm_client.ask(
            f"Question: {q}\n\nSearch results:\n{sources}\n\nAnswer the question using these sources, naming specific companies, "
            "and cite sources inline as [n].", temperature=0.3, max_tokens=700) if results else ""
        cited = [results[int(n) - 1]["url"] for n in set(re.findall(r"\[(\d+)\]", grounded))
                 if n.isdigit() and 0 < int(n) <= len(results)]
        runs.append({
            "question": q,
            "recall_answer": recall[:1500], "recall_mentions": mentions(recall, brand, domain),
            "search_results": [r["url"] for r in results], "site_in_results": in_results,
            "search_answer": grounded[:1500], "search_mentions": mentions(grounded, brand, domain),
            "site_cited": [u for u in cited if domain in u],
            "competitors_mentioned": [c for c in profile.get("competitors", []) if isinstance(c, str) and c.lower() in (recall + grounded).lower()],
        })

    F, P = [], []
    if not known:
        F.append(finding(A, "medium", f"The AI model does not recognize \"{brand}\"",
                         f"Asked \"What is {brand}?\", {llm_client.model_name()} answered: “{recall_brand[:220]}”. "
                         "Models learn brands from mentions across the web (press, directories, reviews, Wikipedia, forums).",
                         "Build off-site mentions: digital PR, industry directories, review sites (G2, Trustpilot, Google Business Profile), "
                         "podcasts/guest articles, Reddit/Quora answers, and a Wikidata entry if notable.", [], check="llm_recall"))
    else:
        P.append(passed(A, f"AI model recognizes the brand ({llm_client.model_name()})", "llm_recall"))

    recall_hits = sum(r["recall_mentions"] for r in runs)
    search_hits = sum(r["search_mentions"] or bool(r["site_cited"]) for r in runs)
    serp_hits = sum(bool(r["site_in_results"]) for r in runs)
    comp = sorted({c for r in runs for c in r["competitors_mentioned"]})
    if recall_hits == 0:
        F.append(finding(A, "high", "Not recommended by AI for buyer questions (memory)",
                         f"For 0 of {len(runs)} buyer questions did the model mention {brand} from its own knowledge"
                         + (f"; it named competitors instead: {', '.join(comp)}" if comp else "") + ".",
                         "Earn third-party coverage on the sites models learn from (comparison articles, 'best X' lists, reviews, communities), "
                         "and publish clear category pages that state what you are and who you are for.", [], check="llm_share"))
    else:
        P.append(passed(A, f"Mentioned by AI from memory in {recall_hits}/{len(runs)} buyer questions", "llm_share"))
    if serp_hits == 0:
        F.append(finding(A, "high", "Site is not among the web sources AI search would use",
                         f"For {len(runs)} buyer questions, {domain} was not in the top search results that answer engines ground on, so it cannot be cited.",
                         "Create pages that directly target these questions (comparison, 'best X for Y', pricing, how-to guides) with answer-first structure; "
                         "get listed in the pages that do rank (roundups, directories).", [], check="ai_sources"))
    elif search_hits == 0:
        F.append(finding(A, "medium", "Site appears in sources but is not cited in the AI answer",
                         f"{domain} showed up in search results for {serp_hits} question(s), but the generated answer did not use it.",
                         "Make the ranking page more quotable: answer in the first paragraph, add specific numbers, comparison tables and FAQs.", [], check="ai_cited"))
    else:
        P.append(passed(A, f"Cited / mentioned in AI search answers ({search_hits}/{len(runs)})", "ai_cited"))

    metrics = {"model": llm_client.model_name(), "brand": brand, "profile": profile, "brand_answer": recall_brand,
               "brand_known": known, "runs": runs, "recall_share": f"{recall_hits}/{len(runs)}",
               "search_share": f"{search_hits}/{len(runs)}", "competitors_seen": comp}
    return {"findings": F, "passed": P, "metrics": metrics}


def main() -> int:
    crawl = json.loads(Path(sys.argv[1]).read_text())
    res = audit(crawl)
    out = write_json(Path(sys.argv[1]).with_name("geo_visibility.json"), res)
    print(res.get("skipped") or f"{len(res['findings'])} findings -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
