# Deploying the SEO / AEO / GEO audit app (Hetzner + Docker)

This guide takes you from zero to a public HTTPS site at `https://your-domain.com` where
anyone can log in with an email magic link and get a few free reports per month.

**What runs on the server**

```
Internet ──443/80──> Caddy (HTTPS certificates, security headers, gzip)
                        │  internal Docker network only
                        ▼
                     app (FastAPI, 1 worker, background crawl jobs,
                          headless Chromium for PDF reports)
                        │
                        ▼
                     Docker volume "app-data" -> /app/.tmp
                     (SQLite db app.db, job reports, crawl intermediates)
```

## Option A: Render, native Python (no Docker, no server to manage)

Render builds and runs the app from the GitHub repo. Settings for **New > Web Service > elkushi/elkoushy_seo_geo_aeo**:

| Field | Value |
|---|---|
| Language | **Python 3** |
| Branch | `main` |
| Region | Frankfurt (EU) |
| Build Command | `grep -v "# optional" requirements.txt > /tmp/req.txt && pip install -r /tmp/req.txt && python -m playwright install --only-shell chromium` |
| Start Command | `uvicorn app.server:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips='*'` |
| Instance Type | Starter ($7). Free has no disk and sleeps. |
| Health Check Path | `/api/config` |
| Disk | mount path `/var/data`, 1 GB (users, reports, sessions) |
| Auto-Deploy | After CI Checks Pass |

Environment variables: `PYTHON_VERSION=3.12.8`, `PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/src/.playwright`
(Chromium must be inside the project folder to survive from build to runtime), `DATA_DIR=/var/data`,
`MAX_CONCURRENT_JOBS=1`, plus `PUBLIC_URL`, `SESSION_SECRET` (Render: Generate), `RESEND_API_KEY`, `MAIL_FROM`,
`ADMIN_EMAILS`, `FREE_REPORTS_PER_MONTH`, `MAX_PAGES`, `ENABLE_AI=0`, `BOT_INFO_URL` as in step 10.

**Render Free + Neon (no paid disk):** Free instances have no persistent disk, so the SQLite file would be wiped on
every restart. Create a free Postgres at <https://neon.tech> (region: AWS Frankfurt), copy the connection string and set
`DATABASE_URL=<it>` instead of the Disk/`DATA_DIR`. Users, sessions, jobs and the report files are then stored in Neon.
Free instances sleep after 15 minutes without traffic (first visit takes ~1 minute) and have 0.1 CPU (audits take a few minutes).

PDF: the native runtime cannot install Chromium's system libraries. If PDFs fail, the report still works
(the PDF step is optional and is noted in the report). Switch Language to Docker in that case; the Dockerfile ships the libraries.

The rest of this guide (Option B) is for your own Hetzner server with Docker.

Everywhere below, replace:

| Placeholder | Meaning |
|---|---|
| `your-domain.com` | the domain you buy |
| `SERVER_IP` | the IPv4 address Hetzner gives your server |
| `YOUR_GITHUB_USER` | your GitHub username |
| `deploy` | the non-root Linux user we create on the server |

Expected time: about 1-2 hours the first time, most of it waiting for DNS.

---

## 1. Buy a domain

Any registrar works (Cloudflare Registrar, Porkbun, INWX, Namecheap, Hetzner's own
domain service). You need access to the **DNS settings** (to add A, AAAA, TXT and MX records).

If you use Cloudflare DNS: set the records below to **"DNS only" (grey cloud)**, not proxied,
otherwise Caddy cannot obtain its certificate the simple way.

## 2. Create the server (Hetzner Cloud)

1. Sign up at <https://console.hetzner.cloud> (new accounts may need ID verification).
2. Create a project, then **Add Server**:
   - **Location:** an EU location (Falkenstein, Nuremberg or Helsinki).
   - **Image:** Ubuntu 24.04.
   - **Type:** Shared vCPU, x86, **CX22** (2 vCPU, 4 GB RAM, 40 GB disk) or whatever the
     smallest current "CX" plan is called. The app is capped at 1.5 GB RAM (headless
     Chromium for PDF reports plus two concurrent crawls), so 4 GB is plenty. Avoid 2 GB plans.
   - **Networking:** keep public IPv4 and IPv6.
   - **SSH key:** add your public key. On your laptop:
     ```bash
     ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -C "you@your-domain.com"
     cat ~/.ssh/id_ed25519.pub      # paste this into Hetzner
     ```
   - **Backups:** enable them (+20% of the server price, daily snapshots, 7 kept).
   - **Name:** e.g. `seo-audit`.
3. Note the server's **IPv4** and **IPv6** address.

## 3. Firewall (Hetzner Cloud Firewall)

Use the Hetzner Cloud Firewall, not only `ufw` on the server: Docker writes its own
iptables rules for published ports and **bypasses ufw**. The Hetzner firewall sits in front
of the server, so it cannot be bypassed.

Console > **Firewalls** > **Create Firewall**. Inbound rules (delete everything else):

| Protocol | Port | Source | Why |
|---|---|---|---|
| TCP | 22 | Any IPv4 + IPv6 (or only your home IP) | SSH |
| TCP | 80 | Any IPv4 + IPv6 | HTTP -> HTTPS redirect + certificate challenge |
| TCP | 443 | Any IPv4 + IPv6 | HTTPS |
| UDP | 443 | Any IPv4 + IPv6 | optional, HTTP/3 |

Leave outbound open (the app must crawl websites and call the Resend API).
**Apply to resources** > select your server.

## 4. DNS records

At your registrar's DNS settings:

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `@` (the bare domain) | `SERVER_IP` | 300 |
| AAAA | `@` | the server's IPv6 (Hetzner shows a `/64`; use the address ending in `::1`) | 300 |
| A | `www` | `SERVER_IP` | 300 |
| AAAA | `www` | same IPv6 as above | 300 |

The `www` records are optional; if you skip them, delete the `www.{$DOMAIN}` block at the
bottom of `Caddyfile`. Only add an AAAA record if the IPv6 really works (a wrong AAAA record
is the #1 reason certificates fail). Check from your laptop:

```bash
dig +short your-domain.com A
dig +short your-domain.com AAAA
```

Both should print your server's addresses (can take a few minutes, sometimes up to an hour).

## 5. First login and server hardening

From your laptop:

```bash
ssh root@SERVER_IP
```

On the server (as root):

```bash
apt update && apt upgrade -y
adduser deploy                        # choose a password (needed for sudo)
usermod -aG sudo deploy
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy   # copy your SSH key to the new user
```

**Open a second terminal** and check the new user works before going further:

```bash
ssh deploy@SERVER_IP
sudo whoami        # prints: root
```

Then turn off root login and password login (as `deploy`):

```bash
printf "PermitRootLogin no\nPasswordAuthentication no\n" | sudo tee /etc/ssh/sshd_config.d/99-hardening.conf
sudo systemctl reload ssh
sudo apt install -y unattended-upgrades && sudo dpkg-reconfigure -plow unattended-upgrades   # answer "Yes"
```

Optional but recommended on a small server: a 2 GB swap file (helps during `docker build`).

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

From now on always log in as `deploy`: `ssh deploy@SERVER_IP`.

## 6. Install Docker

Official convenience script (installs Docker Engine + the Compose plugin from Docker's apt repo):

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker deploy
exit                                  # log out and back in so the group applies
```

```bash
ssh deploy@SERVER_IP
docker --version
docker compose version
docker run --rm hello-world
```

(Alternative: the manual apt-repository steps at <https://docs.docker.com/engine/install/ubuntu/>.)

## 7. Put the code in a private GitHub repo

Do this **on your laptop**. This project folder sits inside a bigger study repo, so give it
its own repository:

```bash
cd ~/Desktop/study/claue_ai/5-seo_geo_aeo_expert
git init -b main

# Safety check: each of these MUST print a matching .gitignore rule.
git check-ignore -v .env .venv .tmp/app.db credentials.json token.json

git add .
git status                            # .env, .venv/, .tmp/ contents must NOT be listed
git commit -m "Initial commit"
```

Create the private repo and push, either with the GitHub CLI:

```bash
gh auth login                         # once
gh repo create seo-audit --private --source=. --remote=origin --push
```

or in the browser: github.com > **New repository** > name `seo-audit`, **Private**, no README, then:

```bash
git remote add origin git@github.com:YOUR_GITHUB_USER/seo-audit.git
git push -u origin main
```

Optional: add `5-seo_geo_aeo_expert/` to the parent folder's `.gitignore` (`../.gitignore`) so
the outer study repo does not treat it as an embedded repository.

> **`.env` must never be committed.** It holds your session secret and API keys. If it ever
> lands in git, rotate every key in it (new `SESSION_SECRET`, new Resend key), because git
> history keeps old versions.

## 8. Clone the repo on the server (read-only deploy key)

On the server:

```bash
ssh-keygen -t ed25519 -C "seo-audit-server" -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```

GitHub > your repo > **Settings > Deploy keys > Add deploy key**: paste it, leave
"Allow write access" **unchecked**.

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config

git clone git@github.com:YOUR_GITHUB_USER/seo-audit.git ~/seo-audit
cd ~/seo-audit
```

The folder name `seo-audit` becomes the Docker Compose project name, so the data volume is
called `seo-audit_app-data` (used in the backup commands below).

## 9. Set up login emails (Resend)

1. Sign up at <https://resend.com> (free tier: 3,000 emails/month, 100/day).
2. **Domains > Add Domain**: enter `your-domain.com`, pick the **EU** region (Ireland).
3. Resend shows DNS records. Add **all of them** at your registrar, typically:
   - **MX** on `send` (e.g. `feedback-smtp.eu-west-1.amazonses.com`, priority 10) - bounce handling
   - **TXT** on `send`: `v=spf1 include:amazonses.com ~all` - **SPF**
   - **TXT** on `resend._domainkey`: a long `p=...` key - **DKIM**
   - recommended: **TXT** on `_dmarc`: `v=DMARC1; p=none;` - **DMARC**

   Copy the exact values from Resend; the examples above are only for orientation.
4. Click **Verify DNS Records** and wait until the domain shows **Verified**.
5. **API Keys > Create API Key**: permission "Sending access", restricted to your domain.
   Copy the key (starts with `re_`); it is shown only once.

`MAIL_FROM` must use the verified domain, e.g. `SEO Audit <login@your-domain.com>`.
The address does not need a real mailbox.

(Hetzner blocks outgoing port 25 on new servers; that does not matter here, Resend is called over HTTPS.)

## 10. Create `.env`

On the server, in `~/seo-audit`:

```bash
cp .env.example .env
chmod 600 .env
python3 -c "import secrets;print(secrets.token_urlsafe(48))"   # copy the output
nano .env
```

Set at least:

```ini
PUBLIC_URL=https://your-domain.com
DOMAIN=your-domain.com
SESSION_SECRET=<the long random string you just generated>
RESEND_API_KEY=re_...
MAIL_FROM="SEO Audit <login@your-domain.com>"
FREE_REPORTS_PER_MONTH=3
MAX_PAGES=25
ENABLE_AI=0
BOT_INFO_URL=https://your-domain.com/bot
ADMIN_EMAILS=you@your-domain.com
PAGESPEED_API_KEY=<optional, recommended: keyless PageSpeed calls are often rate limited>
```

Leave the Google OAuth / Bing / Gemini / Groq / Ollama / NVIDIA keys empty; the web app
does not need them while `ENABLE_AI=0`. (If you later switch AI on, check that your LLM
provider's terms allow production use; free developer tiers often do not.)

Save in nano with `Ctrl+O`, `Enter`, exit with `Ctrl+X`.

## 11. Start it

```bash
cd ~/seo-audit
docker compose up -d --build         # first build takes a few minutes (see below)
docker compose ps                     # app should become "healthy" within ~30 s
docker compose logs -f caddy          # wait for "certificate obtained successfully", then Ctrl+C
docker compose logs -f app            # app logs, Ctrl+C to stop following
```

About the image size: reports can also be downloaded as **PDF**. The PDF is rendered by
headless Chromium (Playwright) inside the app container, with network requests blocked.
Chromium and its system libraries add roughly **400 MB** to the image, so expect a total
image of about 0.8-1 GB and a first build of 3-6 minutes. Later builds reuse cached layers
unless `requirements.txt` changes. `docker image prune -f` after updates keeps disk use in check.

Open `https://your-domain.com`. Quick checks from your laptop:

```bash
curl -s https://your-domain.com/api/config          # JSON; "dev_login" must be false
curl -sI https://your-domain.com | grep -iE "strict-transport|content-security|x-content-type"
curl -sI http://your-domain.com | head -3           # 308 redirect to https
```

## 12. First login test

1. Open `https://your-domain.com`, enter your email, request the link.
2. The email should arrive within a minute (check spam; Resend dashboard > **Emails**
   shows delivery status).
3. Click the link: you land on the app, logged in.
4. Run one small analysis (e.g. your own domain), open the report, try every download
   (HTML, Markdown, PDF).

## 13. Make yourself admin (unlimited reports)

Admins are the emails listed in `ADMIN_EMAILS` (comma-separated, no spaces needed):

```bash
nano .env                             # ADMIN_EMAILS=you@your-domain.com,partner@example.com
docker compose up -d                  # recreates the app with the new env
```

Note: after **any** `.env` change use `docker compose up -d`. A plain `docker compose restart`
keeps the old environment.

## 14. Updating the app

On your laptop: commit and `git push`. On the server:

```bash
cd ~/seo-audit
git pull
docker compose up -d --build
docker image prune -f                 # remove old image layers (disk space)
```

The app restarts in a few seconds. A report that is running at that moment is interrupted,
so update when nobody is mid-analysis (`docker compose logs --since 10m app`).

### 14b. Automatic deploys with GitHub Actions (recommended)

`.github/workflows/deploy.yml` does the update for you on every push to `main`:

1. **test**: offline smoke tests (`tests/`), on every push and pull request.
2. **build**: builds the image on GitHub's machines (the small server never builds Chromium itself)
   and pushes it to the private registry `ghcr.io/YOUR_GITHUB_USER/seo-audit` (tags `:<commit sha>` and `:latest`).
3. **deploy**: copies `docker-compose.yml` + `Caddyfile` to the server over SSH, pulls exactly that image,
   restarts, and checks that `https://your-domain.com/api/config` answers with `"dev_login":false`.

Deploy stays **off** until you switch it on, so pushes before the server exists only test and build.

**One-time setup**

On the server (as `deploy`), create a key that GitHub Actions uses to log in:

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/actions_deploy -N ""
cat ~/.ssh/actions_deploy.pub >> ~/.ssh/authorized_keys
cat ~/.ssh/actions_deploy          # private key -> GitHub secret SSH_PRIVATE_KEY (then you may delete this file)
```

On your laptop, get the server's host key (so Actions only ever talks to *your* server):

```bash
ssh-keyscan -t ed25519 SERVER_IP   # whole output line -> GitHub secret SSH_KNOWN_HOSTS
```

GitHub > repo > **Settings > Secrets and variables > Actions**:

| Kind | Name | Value |
|---|---|---|
| Secret | `SSH_HOST` | `SERVER_IP` |
| Secret | `SSH_USER` | `deploy` |
| Secret | `SSH_PRIVATE_KEY` | the private key printed above (all lines) |
| Secret | `SSH_KNOWN_HOSTS` | the `ssh-keyscan` line |
| Variable | `DOMAIN` | `your-domain.com` (used by the health check) |
| Variable | `DEPLOY_ENABLED` | `true` - set this last, once steps 8-12 work |
| Variable (optional) | `DEPLOY_PATH` | default `seo-audit` (relative to the deploy user's home) |

In `~/seo-audit/.env` on the server, add `APP_IMAGE=ghcr.io/YOUR_GITHUB_USER/seo-audit:latest`, so a manual
`docker compose up -d` uses the registry image too. The `.env` file itself stays only on the server; Actions never sees it.

**Day-to-day:** commit, `git push`, watch **Actions** on GitHub. A failed test or build never reaches the server.
**Rollback:** GitHub > Actions > an older successful run > **Re-run all jobs** (it redeploys that commit's image),
or on the server: `APP_IMAGE=ghcr.io/YOUR_GITHUB_USER/seo-audit:<old sha> docker compose up -d --no-build`.

## 15. Backups

**Layer 1: Hetzner Backups** (enabled in step 2) snapshot the whole server daily, including
the Docker volumes. Restore from the Hetzner console.

**Layer 2: nightly SQLite copies** (users, quotas, job list). The image has no `sqlite3` CLI,
so we use Python's built-in SQLite backup API, which is safe while the app is running:

```bash
mkdir -p ~/backups
cd ~/seo-audit
docker compose exec -T app python -c "import sqlite3; s=sqlite3.connect('/app/.tmp/app.db'); d=sqlite3.connect('/app/.tmp/app-backup.db'); s.backup(d); d.close(); s.close()"
docker compose cp app:/app/.tmp/app-backup.db ~/backups/app-$(date +%F).db
ls -lh ~/backups
```

Automate it with cron (`crontab -e`, then add one line; `%` must be escaped as `\%` in crontab):

```cron
15 3 * * * cd /home/deploy/seo-audit && ( docker compose exec -T app python -c "import sqlite3; s=sqlite3.connect('/app/.tmp/app.db'); d=sqlite3.connect('/app/.tmp/app-backup.db'); s.backup(d); d.close(); s.close()" && docker compose cp app:/app/.tmp/app-backup.db /home/deploy/backups/app-$(date +\%F).db && find /home/deploy/backups -name 'app-*.db' -mtime +14 -delete ) >> /home/deploy/backups/cron.log 2>&1
```

**Full volume copy** (database + all HTML reports), e.g. weekly or before risky changes:

```bash
docker run --rm -v seo-audit_app-data:/data:ro -v ~/backups:/b alpine \
  tar czf /b/app-data-$(date +%F).tar.gz -C /data .
```

**Off-site copy**: pull backups to your laptop now and then (or to a Hetzner Storage Box):

```bash
rsync -av deploy@SERVER_IP:backups/ ~/seo-audit-backups/
```

**Restore the database** from a nightly copy:

```bash
cd ~/seo-audit
docker compose stop app
docker run --rm -v seo-audit_app-data:/data -v ~/backups:/b alpine sh -c \
  'rm -f /data/app.db-wal /data/app.db-shm && cp /b/app-YYYY-MM-DD.db /data/app.db && chown 10001:10001 /data/app.db'
docker compose start app
```

## 16. Monitoring

- **UptimeRobot** (free, <https://uptimerobot.com>): New monitor > type **HTTP(s)** (or
  **Keyword** with keyword `free_limit`) > URL `https://your-domain.com/api/config` >
  interval 5 minutes > alert to your email. You get notified when the site is down.
- On the server, occasionally:
  ```bash
  docker compose ps                   # both containers "Up", app "healthy"
  docker stats --no-stream            # memory use (app is capped at 1.5 GB)
  df -h /                             # disk space (reports accumulate in the volume)
  ```
- Resend dashboard: bounce / complaint rates for the login emails.

## 17. Crawling etiquette (avoid Hetzner abuse reports)

The server fetches websites that strangers type in. Site owners who see unexpected traffic
can complain to Hetzner's abuse desk, and Hetzner forwards those complaints to you with a
deadline to respond (unanswered complaints can get the server blocked). Keep it boring:

- Keep the public **bot page** (`/bot`, `BOT_INFO_URL`) online and accurate: what the crawler
  does, how many pages it fetches, how to contact you, how to opt out.
- The crawler's User-Agent must point to that page/contact, so admins can identify you.
- Keep `MAX_PAGES` small (25 is fine) and the per-site cooldown on; the app respects robots.txt.
- Never raise worker count or concurrency to "go faster".
- Answer every abuse email quickly and politely (follow the link in Hetzner's email):
  explain the service, and block the complaining domain if asked.
- Watch for misuse (someone auditing the same site in a loop); the per-user quota and
  per-site cooldown help.
- The AI-crawler check sends a few requests per audit with AI-bot User-Agents (GPTBot etc.)
  to see how the site responds to them. If a site owner complains, explain that it is a
  one-off access test a user triggered, not real bot traffic.

## 18. Pre-launch checklist

- [ ] Legal pages filled in with your real details: `/imprint` (Impressum: name, address,
      contact; required in Germany/Austria), `/privacy` (what is stored: email, reports,
      IP in logs; processors Hetzner and Resend; retention; contact), `/terms`, `/bot`
- [ ] `SESSION_SECRET` set to a long random value (not empty, not the example)
- [ ] `RESEND_API_KEY` set, domain **Verified** in Resend, `/api/config` shows `"dev_login": false`
- [ ] `PUBLIC_URL` is the `https://` URL; `DOMAIN` matches it
- [ ] `ENABLE_AI=0`
- [ ] Your email in `ADMIN_EMAILS`
- [ ] Magic link tested end to end (request, email arrives, click, logged in, logout)
- [ ] Quota tested with a second, non-admin email: after `FREE_REPORTS_PER_MONTH` reports
      the next one is refused with a clear message
- [ ] `.env` is not in git (`git ls-files | grep -x .env` prints nothing)
- [ ] Hetzner firewall active (only 22/80/443), root + password SSH login disabled
- [ ] Nightly backup cron added and one backup restored on a test basis
- [ ] UptimeRobot monitor green

## 19. Monthly cost estimate

| Item | Cost |
|---|---|
| Domain (`.com`, ~€10-15 per year) | ~€1 / month |
| Hetzner CX22 incl. IPv4 + VAT | ~€4-5 / month |
| Hetzner Backups (+20% of server price) | ~€1 / month |
| Resend free tier (3,000 emails/month) | €0 |
| Caddy / Let's Encrypt certificates | €0 |
| UptimeRobot free | €0 |
| **Total** | **~€6-7 / month** |

Prices change; check the Hetzner and registrar price lists.

## 20. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Caddy log: certificate errors, "timeout during connect" | DNS not pointing to the server yet, wrong AAAA record, or port 80/443 closed in the Hetzner firewall. Fix, then `docker compose restart caddy`. |
| `docker compose up` says "dependency failed to start" | The app is not healthy. `docker compose logs app` shows the Python error (often a missing or invalid `.env` value). |
| 502 Bad Gateway | App container down or restarting: `docker compose ps`, `docker compose logs app`. |
| `set DOMAIN in .env` error | `DOMAIN=` is missing or empty in `.env`. |
| Login email never arrives | Resend dashboard > Emails (bounced?); domain verified?; `MAIL_FROM` on the verified domain?; check spam. |
| App killed / restarts during big crawls | Out of memory (`docker compose logs app`, `sudo dmesg \| grep -i oom`): lower `MAX_PAGES`. |
| PDF download fails, HTML works | Chromium problem: `docker compose logs app`. Rebuild with `docker compose build --no-cache app` if the browser files are missing. |
| Disk filling up | `docker system df`; `docker image prune -f`; old reports live in the `seo-audit_app-data` volume. |

**Local dry run** (optional, on a machine with Docker): set `DOMAIN=localhost` and
`PUBLIC_URL=https://localhost` in `.env`, run `docker compose up -d --build`, open
`https://localhost` (Caddy uses its own local certificate, so the browser warns once).
Run `docker compose down` afterwards.
