"""Public web app: sign in with an email magic link, paste a website URL, get an
SEO / AEO / GEO improvement report. Contract: app/API_CONTRACT.md.

Run locally:
  .venv/bin/uvicorn app.server:app --host 127.0.0.1 --port 8000
Production (one worker only - jobs run in threads of this process):
  uvicorn app.server:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips="*"

Env (.env): PUBLIC_URL, SESSION_SECRET, RESEND_API_KEY, MAIL_FROM, FREE_REPORTS_PER_MONTH,
MAX_PAGES, ENABLE_AI, ADMIN_EMAILS, PAGESPEED_API_KEY, MAX_CONCURRENT_JOBS, DATA_DIR, DATABASE_URL.
Data: SQLite at DATA_DIR/app.db, or Postgres when DATABASE_URL is set (app/db.py); reports are stored in the db.
"""

import hashlib
import hmac
import json
import re
import secrets
import sys
import threading
import time
import traceback
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import llm_client  # noqa: E402
from analyze_site import analyze  # noqa: E402
from common import domain_of, env, is_public_url, normalize_url  # noqa: E402

from app.db import Database  # noqa: E402

# --- config -------------------------------------------------------------------
PUBLIC_URL = (env("PUBLIC_URL") or "http://127.0.0.1:8000").rstrip("/")
IS_LOCAL = urlparse(PUBLIC_URL).hostname in ("127.0.0.1", "localhost")
SECRET = (env("SESSION_SECRET") or ("dev-only-secret" if IS_LOCAL else "")).encode()
if not IS_LOCAL and len(SECRET) < 32:
    raise RuntimeError("SESSION_SECRET (32+ characters) must be set in .env for a public deployment")
RESEND_KEY = env("RESEND_API_KEY")
MAIL_FROM = env("MAIL_FROM") or "SEO Audit <onboarding@resend.dev>"
FREE_LIMIT = int(env("FREE_REPORTS_PER_MONTH") or 3)
MAX_PAGES = int(env("MAX_PAGES") or 25)
ADMINS = {e.strip().lower() for e in (env("ADMIN_EMAILS") or "").split(",") if e.strip()}
AI_ON = (env("ENABLE_AI") or "0") == "1" and llm_client.available()
PAGESPEED_ON = bool(env("PAGESPEED_API_KEY"))  # the keyless quota is shared worldwide and usually exhausted

LOGIN_TTL = timedelta(minutes=15)
SESSION_TTL = timedelta(days=30)
RETENTION = timedelta(days=365)
DOMAIN_RUNS_PER_DAY = 10
MAX_QUEUE = 10
EMAIL_RX = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[a-z]{2,}$", re.I)

DATA = Path(env("DATA_DIR") or ROOT / ".tmp")  # DATA_DIR: persistent disk on Render/Hetzner; tests use a throwaway folder
JOBS_DIR = DATA / "jobs"  # reports of older versions were files here; new ones live in the database
SLOTS = threading.Semaphore(int(env("MAX_CONCURRENT_JOBS") or 2))  # 1 on 512 MB hosts (Chromium + crawl)
DB_LOCK = threading.Lock()
LIVE_STEPS: dict[str, list[str]] = {}
IP_HITS: dict[str, deque] = defaultdict(deque)

db = Database(env("DATABASE_URL"), DATA / "app.db")
q = db.q


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def digest(token: str) -> str:
    return hmac.new(SECRET, token.encode(), hashlib.sha256).hexdigest()


def startup_cleanup() -> None:
    """Fail jobs interrupted by a restart; drop expired tokens/sessions and reports past retention."""
    q("UPDATE jobs SET status='error', error='Interrupted by a server restart - run it again.' WHERE status IN ('queued','running')")
    q("DELETE FROM login_tokens WHERE expires < ?", (now(),))
    q("DELETE FROM sessions WHERE expires < ?", (now(),))
    cutoff = (datetime.now() - RETENTION).isoformat(timespec="seconds")
    q("DELETE FROM job_files WHERE job_id IN (SELECT id FROM jobs WHERE created < ?)", (cutoff,))
    q("DELETE FROM jobs WHERE created < ?", (cutoff,))


startup_cleanup()
app = FastAPI(title="SEO · AEO · GEO Expert", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def json_only_posts(request: Request, call_next):
    """CSRF guard: browsers cannot send cross-site application/json without a CORS preflight, which we never allow."""
    if request.method == "POST" and request.url.path.startswith("/api/") \
            and not request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)
    return await call_next(request)


# --- auth ---------------------------------------------------------------------
def current_user(request: Request):
    token = request.cookies.get("session")
    if not token:
        return None
    return q("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.hash = ? AND s.expires > ?",
             (digest(token), now()), one=True)


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def rate_limited(key: str, limit: int, window_s: int) -> bool:
    hits, t = IP_HITS[key], time.time()
    while hits and hits[0] < t - window_s:
        hits.popleft()
    if len(hits) >= limit:
        return True
    hits.append(t)
    return False


def send_login_email(email: str, link: str) -> None:
    html = (f"<p>Click to sign in to SEO · AEO · GEO Expert:</p><p><a href=\"{link}\">Sign in</a></p>"
            "<p>The link works once and expires in 15 minutes. If you did not request it, ignore this email.</p>")
    r = requests.post("https://api.resend.com/emails", timeout=15, headers={"Authorization": f"Bearer {RESEND_KEY}"},
                      json={"from": MAIL_FROM, "to": [email], "subject": "Your sign-in link",
                            "html": html, "text": f"Sign in: {link}\n\nThe link works once and expires in 15 minutes."})
    if r.status_code >= 300:
        raise HTTPException(502, "Could not send the email right now. Please try again in a minute.")


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=254)


@app.post("/api/auth/request")
def auth_request(body: LoginRequest, request: Request):
    email = body.email.strip().lower()
    if not EMAIL_RX.match(email):
        raise HTTPException(400, "Please enter a valid email address.")
    ip = request.client.host if request.client else "?"
    recent = q("SELECT COUNT(*) n FROM login_tokens WHERE email = ? AND created > ?",
               (email, (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")), one=True)["n"]
    if recent >= 5 or rate_limited(f"login:{ip}", 10, 3600):
        raise HTTPException(429, "Too many sign-in requests. Please wait a while and try again.")
    token = secrets.token_urlsafe(32)
    q("INSERT INTO login_tokens (hash, email, expires, created) VALUES (?, ?, ?, ?)",
      (digest(token), email, (datetime.now() + LOGIN_TTL).isoformat(timespec="seconds"), now()))
    link = f"{PUBLIC_URL}/auth/verify?token={token}"
    if RESEND_KEY:
        send_login_email(email, link)
        return {"ok": True}
    if IS_LOCAL:
        return {"ok": True, "dev_link": link}
    raise HTTPException(503, "Sign-in email is not configured on this server.")


@app.get("/auth/verify")
def auth_verify(token: str = ""):
    row = q("SELECT * FROM login_tokens WHERE hash = ? AND used = 0 AND expires > ?", (digest(token), now()), one=True)
    if not row:
        return RedirectResponse("/?login=expired", status_code=303)
    q("UPDATE login_tokens SET used = 1 WHERE hash = ?", (row["hash"],))
    q("INSERT INTO users (email, created) VALUES (?, ?) ON CONFLICT (email) DO NOTHING", (row["email"], now()))
    user = q("SELECT * FROM users WHERE email = ?", (row["email"],), one=True)
    q("UPDATE users SET last_login = ? WHERE id = ?", (now(), user["id"]))
    session = secrets.token_urlsafe(32)
    q("INSERT INTO sessions (hash, user_id, expires) VALUES (?, ?, ?)",
      (digest(session), user["id"], (datetime.now() + SESSION_TTL).isoformat(timespec="seconds")))
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", session, max_age=int(SESSION_TTL.total_seconds()), httponly=True,
                    secure=PUBLIC_URL.startswith("https://"), samesite="lax", path="/")
    return resp


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    if token := request.cookies.get("session"):
        q("DELETE FROM sessions WHERE hash = ?", (digest(token),))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session", path="/")
    return resp


# --- quota --------------------------------------------------------------------
def is_admin(user) -> bool:
    return user["email"] in ADMINS


def quota(user) -> dict:
    start = date.today().replace(day=1)
    resets = (start + timedelta(days=32)).replace(day=1)
    used = q("SELECT COUNT(*) n FROM jobs WHERE user_id = ? AND status IN ('queued','running','done') AND created >= ?",
             (user["id"], start.isoformat()), one=True)["n"]
    limit = 10_000 if is_admin(user) else FREE_LIMIT
    return {"limit": limit, "used": used, "remaining": max(0, limit - used), "resets": resets.isoformat()}


def page_cap(user) -> int:
    return 60 if is_admin(user) else MAX_PAGES


@app.get("/api/config")
def config():
    return {"ai_available": AI_ON, "pagespeed": PAGESPEED_ON, "free_limit": FREE_LIMIT, "max_pages": MAX_PAGES,
            "dev_login": not RESEND_KEY and IS_LOCAL}


@app.get("/api/me")
def me(user=Depends(require_user)):
    return {"email": user["email"], "quota": quota(user), "max_pages": page_cap(user)}


# --- jobs ---------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    url: str = Field(..., max_length=2000)
    max_pages: int = Field(25, ge=5, le=60)
    pagespeed: bool = True


def job_dict(row) -> dict:
    d = {k: row[k] for k in ("id", "url", "status", "created", "finished", "site", "summary", "error")}
    d["steps"] = LIVE_STEPS.get(row["id"]) or json.loads(row["steps"] or "[]")
    for k in ("scores", "counts", "skipped"):
        d[k] = json.loads(row[k]) if row[k] else None
    return {k: v for k, v in d.items() if v is not None}


def run_job(job_id: str, url: str, max_pages: int, pagespeed: bool) -> None:
    steps = LIVE_STEPS.setdefault(job_id, [])
    with SLOTS:
        q("UPDATE jobs SET status='running' WHERE id = ?", (job_id,))
        try:
            r = analyze(url, max_pages, pagespeed, AI_ON, progress=steps.append, keep_crawl=False, make_pdf=True)
            for key, name in (("html", "report.html"), ("md", "action_plan.md"), ("pdf", "report.pdf")):
                if key in r["paths"]:
                    db.put_file(job_id, name, Path(r["paths"][key]).read_bytes())
            q("UPDATE jobs SET status='done', site=?, scores=?, counts=?, summary=?, skipped=?, finished=?, steps=? WHERE id = ?",
              (r["site"], json.dumps(r["scores"]), json.dumps(r["counts"]), r["summary"], json.dumps(r["skipped"]),
               now(), json.dumps(steps), job_id))
        except Exception as e:
            if not isinstance(e, (ValueError, OSError)) and "FetchError" not in type(e).__name__:
                traceback.print_exc()
            q("UPDATE jobs SET status='error', error=?, finished=?, steps=? WHERE id = ?",
              (str(e)[:400] or type(e).__name__, now(), json.dumps(steps), job_id))
        finally:
            LIVE_STEPS.pop(job_id, None)


@app.post("/api/analyze")
def start(req: AnalyzeRequest, user=Depends(require_user)):
    try:
        url = normalize_url(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e)[:1].upper() + str(e)[1:])
    if not is_public_url(url):
        raise HTTPException(400, "That address does not resolve to a public website.")
    qt = quota(user)
    if qt["remaining"] <= 0:
        raise HTTPException(402, f"You have used all {qt['limit']} free reports this month. More are available from {qt['resets']}.")
    if q("SELECT 1 FROM jobs WHERE user_id = ? AND status IN ('queued','running')", (user["id"],), one=True):
        raise HTTPException(429, "You already have an analysis running. Please wait for it to finish.")
    dom = domain_of(url)
    if q("SELECT 1 FROM jobs WHERE domain = ? AND status IN ('queued','running')", (dom,), one=True):
        raise HTTPException(429, f"{dom} is being analysed right now. Try again in a few minutes.")
    day_ago = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    if not is_admin(user) and q("SELECT COUNT(*) n FROM jobs WHERE domain = ? AND created > ?", (dom, day_ago), one=True)["n"] >= DOMAIN_RUNS_PER_DAY:
        raise HTTPException(429, f"{dom} has been analysed many times today. To be polite to that site, please try again tomorrow.")
    if q("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running')", one=True)["n"] >= MAX_QUEUE:
        raise HTTPException(429, "The server is busy. Please try again in a few minutes.")

    job_id = uuid.uuid4().hex[:16]
    q("INSERT INTO jobs (id, user_id, url, domain, status, steps, created) VALUES (?, ?, ?, ?, 'queued', '[]', ?)",
      (job_id, user["id"], url, dom, now()))
    LIVE_STEPS[job_id] = []
    threading.Thread(target=run_job, args=(job_id, url, min(req.max_pages, page_cap(user)),
                                           req.pagespeed and PAGESPEED_ON), daemon=True).start()
    return {"job_id": job_id}


def own_job(job_id: str, user):
    row = q("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"]), one=True) if job_id.isalnum() else None
    if not row:
        raise HTTPException(404, "Report not found.")
    return row


@app.get("/api/jobs/{job_id}")
def status(job_id: str, user=Depends(require_user)):
    return job_dict(own_job(job_id, user))


@app.get("/api/jobs")
def history(user=Depends(require_user), limit: int = 30):
    rows = q("SELECT * FROM jobs WHERE user_id = ? AND status = 'done' ORDER BY finished DESC LIMIT ?", (user["id"], min(limit, 100)))
    return [{k: v for k, v in job_dict(r).items() if k in ("id", "site", "url", "scores", "finished")} for r in rows]


def job_file(job_id: str, name: str) -> bytes | None:
    data = db.get_file(job_id, name)
    if data is None and (JOBS_DIR / job_id / name).exists():  # report made before reports moved into the db
        data = (JOBS_DIR / job_id / name).read_bytes()
    return data


@app.get("/reports/{job_id}", response_class=HTMLResponse)
def report(job_id: str, user=Depends(require_user)):
    data = job_file(own_job(job_id, user)["id"], "report.html")
    if data is None:
        raise HTTPException(404, "Report not ready.")
    return Response(data, media_type="text/html")


@app.get("/reports/{job_id}/download")
def download(job_id: str, fmt: str = "html", user=Depends(require_user)):
    job = own_job(job_id, user)
    name, label = {"html": ("report.html", "seo-report.html"), "md": ("action_plan.md", "action-plan.md"),
                   "pdf": ("report.pdf", "seo-report.pdf")}.get(fmt, (None, None))
    data = job_file(job["id"], name) if name else None
    if data is None:
        raise HTTPException(404, "File not found.")
    media = {"html": "text/html", "md": "text/markdown; charset=utf-8", "pdf": "application/pdf"}[fmt]
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{job["site"] or "site"}-{label}"'})


# --- pages --------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(ROOT / "app" / "static" / "index.html", media_type="text/html")


@app.get("/{page}", response_class=HTMLResponse)
def legal(page: str):
    f = ROOT / "app" / "static" / "legal" / f"{page}.html"
    if page not in ("privacy", "terms", "imprint", "bot") or not f.exists():
        raise HTTPException(404, "Page not found.")
    return FileResponse(f, media_type="text/html")
