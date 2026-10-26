# Web API contract (public multi-user version)

Auth = email magic link. Session = HttpOnly cookie `session` (Secure when PUBLIC_URL is https, SameSite=Lax).
All POST bodies are JSON (`Content-Type: application/json`, required — this is the CSRF guard). Errors: `{"detail": "<human message>"}`.

| Method | Path | Auth | Success | Errors |
|---|---|---|---|---|
| GET | `/api/config` | no | `{"ai_available": false, "pagespeed": bool, "free_limit": 3, "max_pages": 25, "dev_login": bool}` | |
| GET | `/api/me` | yes | `{"email": str, "quota": {"limit": int, "used": int, "remaining": int, "resets": "YYYY-MM-DD"}, "max_pages": int}` | 401 |
| POST | `/api/auth/request` | no | body `{"email": str}` -> `{"ok": true}`; plus `"dev_link": str` only when no mail provider is configured (local dev) | 400 invalid email, 429 too many requests |
| GET | `/auth/verify?token=...` | no | sets cookie, 303 -> `/` | 303 -> `/?login=expired` |
| POST | `/api/auth/logout` | yes | `{"ok": true}`, clears cookie | |
| POST | `/api/analyze` | yes | body `{"url": str, "max_pages": int (5..max_pages), "pagespeed": bool}` -> `{"job_id": str}` | 400 bad URL, 401, 402 monthly quota used up, 429 site analysed too recently / server busy |
| GET | `/api/jobs/{id}` | owner | `{"id","url","status": "queued|running|done|error","steps": [str],"created","site"?,"scores"?: {"SEO","AEO","GEO","overall"},"counts"?: {"high","medium","low"},"summary"?,"skipped"?: {k: reason},"finished"?,"error"?}` | 401, 404 |
| GET | `/api/jobs` | yes | list of the user's done jobs `[{"id","site","url","scores","finished"}]` newest first | 401 |
| GET | `/reports/{id}` | owner | report HTML | 401/404 |
| GET | `/reports/{id}/download?fmt=html|md` | owner | file download | 401/404 |
| GET | `/privacy`, `/terms`, `/imprint`, `/bot` | no | static pages from `app/static/legal/{privacy,terms,imprint,bot}.html` | |

A failed run (crawl error) does NOT count against the quota.
