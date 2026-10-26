"""Offline smoke tests (no network): parsing, robots rules, SSRF guard, auth / quota / privacy flow.

Run: .venv/bin/python -m pytest -q
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="seo-test-")
os.environ["PUBLIC_URL"] = "http://127.0.0.1:8000"
os.environ["FREE_REPORTS_PER_MONTH"] = "1"
os.environ.pop("RESEND_API_KEY", None)
# DATABASE_URL (CI sets it to a Postgres service) switches the same tests to Postgres

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from common import is_public_url, normalize_url, robots_allowed  # noqa: E402
from page_parse import parse  # noqa: E402

HTML = """<!doctype html><html lang="en"><head><title>Acme SMS API - Fast delivery | Acme</title>
<meta name="description" content="Send SMS worldwide with one API.">
<link rel="canonical" href="https://acme.test/">
<link rel="alternate" hreflang="en" href="https://acme.test/"><link rel="alternate" hreflang="de" href="https://acme.test/de/">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Acme","url":"https://acme.test/",
"sameAs":["https://www.linkedin.com/company/acme"]}</script></head>
<body><h1>SMS API</h1><h2>How much does the SMS API cost?</h2><p>Acme charges per message sent, with volume discounts that start
at 10,000 messages per month and no setup fee or monthly minimum for any plan.</p>
<a href="/pricing">Learn more</a><img src="a.jpg"><img src="b.webp" width="10" height="10" alt="b"></body></html>"""


def test_parse_fields():
    p = parse({"url": "https://acme.test/", "html": HTML})
    assert p["title"].startswith("Acme SMS API") and p["h1"] == ["SMS API"]
    assert "Organization" in p["schema_types"]
    assert p["question_headings"] == ["How much does the SMS API cost?"] and p["answered_questions"] == 1
    assert ("de", "https://acme.test/de/") in p["hreflang"]
    assert p["generic_anchors"] and p["images_no_alt"] == 1
    assert [i["modern"] for i in p["img_info"]] == [False, True]


def test_robots_rules():
    txt = "User-agent: *\nDisallow: /private/\n\nUser-agent: GPTBot\nDisallow: /\n\nUser-agent: ClaudeBot\nAllow: /\n"
    assert robots_allowed(txt, "SEO-GEO-AEO-Audit", "https://x.test/") is True
    assert robots_allowed(txt, "SEO-GEO-AEO-Audit", "https://x.test/private/a") is False
    assert robots_allowed(txt, "GPTBot", "https://x.test/") is False
    assert robots_allowed(txt, "Claude-SearchBot", "https://x.test/private/") is False  # falls back to *, not ClaudeBot


def test_ssrf_guard():
    for bad in ("http://127.0.0.1", "http://localhost", "http://10.0.0.5", "http://169.254.169.254", "http://[::1]"):
        assert not is_public_url(bad), bad
    with pytest.raises(ValueError):
        normalize_url("localhost")


@pytest.fixture(scope="module")
def client():
    import app.server as server
    return TestClient(server.app), server


def login(c, email):
    r = c.post("/api/auth/request", json={"email": email})
    assert r.status_code == 200 and "dev_link" in r.json()
    token = r.json()["dev_link"].split("token=")[1]
    v = c.get(f"/auth/verify?token={token}", follow_redirects=False)
    assert v.status_code == 303 and "session" in v.cookies
    again = c.get(f"/auth/verify?token={token}", follow_redirects=False)
    assert "expired" in again.headers["location"]  # single use


def test_auth_quota_privacy(client):
    c, server = client
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/auth/request", content='{"email":"a@b.co"}', headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.post("/api/auth/request", json={"email": "nope"}).status_code == 400

    login(c, "alice@example.org")
    me = c.get("/api/me").json()
    assert me["email"] == "alice@example.org" and me["quota"]["remaining"] == 1
    assert c.post("/api/analyze", json={"url": "http://127.0.0.1", "max_pages": 5}).status_code == 400

    # a finished job owned by alice, inserted directly (no crawling in tests)
    alice = server.q("SELECT id FROM users WHERE email = ?", ("alice@example.org",), one=True)["id"]
    server.db.put_file("abc123", "report.html", b"<html>ok</html>")
    server.db.put_file("abc123", "report.pdf", b"%PDF-1.4 test")
    server.q("INSERT INTO jobs (id, user_id, url, domain, status, steps, created, finished, site, scores) "
             "VALUES ('abc123', ?, 'https://acme.test', 'acme.test', 'done', '[]', ?, ?, 'acme.test', '{\"overall\": 80}')",
             (alice, server.now(), server.now()))
    assert c.get("/reports/abc123").text == "<html>ok</html>"
    pdf = c.get("/reports/abc123/download?fmt=pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert c.get("/reports/abc123/download?fmt=md").status_code == 404
    assert c.get("/api/me").json()["quota"]["remaining"] == 0
    assert c.post("/api/analyze", json={"url": "example.com", "max_pages": 5}).status_code == 402

    c.post("/api/auth/logout", json={})
    c.cookies.clear()
    login(c, "bob@example.org")
    assert c.get("/reports/abc123").status_code == 404          # other users cannot see it
    assert c.get("/api/jobs/abc123").status_code == 404
    assert c.get("/api/jobs").json() == []


def test_storage_backend(client):
    _, server = client
    assert server.db.kind == ("postgres" if os.environ.get("DATABASE_URL") else "sqlite")
    server.db.put_file("x1", "a.txt", b"one")
    server.db.put_file("x1", "a.txt", b"two")          # upsert
    assert server.db.get_file("x1", "a.txt") == b"two" and server.db.get_file("x1", "nope") is None


def test_pages_served(client):
    c, _ = client
    for path in ("/", "/privacy", "/terms", "/imprint", "/bot", "/api/config"):
        assert c.get(path).status_code == 200, path
    assert c.get("/nope").status_code == 404
