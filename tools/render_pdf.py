"""Render a report.html to an A4 PDF with headless Chromium (Playwright).

Uses the report's print stylesheet (light palette, all findings expanded, copy
buttons hidden). The page is loaded from file:// and every network request is
blocked, so scraped content inside a report can never make Chromium fetch anything.

Setup: pip install playwright && python -m playwright install chromium
       (Docker: python -m playwright install --with-deps chromium)
Usage:
  python tools/render_pdf.py .tmp/<domain>/report.html [out.pdf]
"""

import sys
from pathlib import Path


def html_to_pdf(html_path: Path, pdf_path: Path, title: str = "") -> Path:
    from playwright.sync_api import sync_playwright

    html_path, pdf_path = Path(html_path).resolve(), Path(pdf_path)
    footer = ('<div style="font:8px system-ui,sans-serif;color:#777;width:100%;padding:0 12mm;display:flex;justify-content:space-between">'
              f'<span>{title}</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file://") else route.abort())
            page.goto(html_path.as_uri(), wait_until="load", timeout=30_000)
            page.emulate_media(media="print", color_scheme="light")
            page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
            page.pdf(path=str(pdf_path), format="A4", print_background=True, display_header_footer=True,
                     header_template="<span></span>", footer_template=footer,
                     margin={"top": "12mm", "bottom": "16mm", "left": "10mm", "right": "10mm"})
        finally:
            browser.close()
    return pdf_path


if __name__ == "__main__":
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".pdf")
    print(html_to_pdf(src, out, title=src.parent.name))
