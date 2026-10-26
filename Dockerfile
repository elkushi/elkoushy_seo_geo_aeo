# SEO / AEO / GEO audit web app (FastAPI + background crawl jobs).
# Build + run via docker-compose.yml (Caddy in front). See DEPLOY.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependencies first (cached layer). Packages tagged "# optional" in
# requirements.txt are not used by the web app and are skipped to save space/RAM.
COPY requirements.txt .
RUN grep -v "# optional" requirements.txt > /tmp/requirements-app.txt \
 && pip install -r /tmp/requirements-app.txt \
 && rm /tmp/requirements-app.txt

# Headless Chromium for PDF reports (tools/render_pdf.py), installed as root into
# /ms-playwright with its system libraries. --only-shell installs just the
# chromium-headless-shell that a default headless launch() uses (about 100 MB less than full
# Chromium). Drop the flag if render_pdf ever launches with channel="chromium".
RUN python -m playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /root/.cache \
 && chmod -R a+rX /ms-playwright

# Non-root runtime user. Code stays root-owned (read-only for the app);
# only /app/.tmp (SQLite db, job reports, crawl intermediates) is writable.
RUN useradd --system --uid 10001 --user-group --create-home --home-dir /home/app app

COPY . .
RUN mkdir -p /app/.tmp && chown app:app /app/.tmp

# The container starts as root only long enough for app/entrypoint.py to make the data folder
# (Docker volume / Render persistent disk, often mounted root-owned) writable; it then drops to
# the unprivileged user `app` before the server starts.
EXPOSE 8000
VOLUME ["/app/.tmp"]

# slim has no curl: use the stdlib. /api/config is public and cheap. PORT is set by Render.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/config' % os.environ.get('PORT', '8000'), timeout=4)" || exit 1

# Exactly ONE worker: jobs run in background threads guarded by an in-process semaphore
# (MAX_CONCURRENT_JOBS), so more workers would multiply the concurrent crawls.
# Proxy headers are trusted from any IP because the port is only reachable through the
# platform's proxy (Caddy on Hetzner, Render's router) - it is never published directly.
# PORT: Render injects it (default 10000); docker-compose leaves it unset -> 8000.
ENTRYPOINT ["python", "/app/app/entrypoint.py"]
CMD ["sh", "-c", "exec uvicorn app.server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
