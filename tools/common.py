"""Shared helpers for the SEO / GEO / AEO tools.

- fetch(): GET with a hard wall-clock deadline, size cap and one retry
- normalize_url() / is_public_url(): input cleaning + SSRF guard (the web app
  fetches user-supplied URLs, so private / loopback addresses are refused)
- finding(): the one shape every audit tool returns, so scoring and the report
  stay deterministic
- run_dir(), write_json(): .tmp/<domain>/ intermediates
"""

import ipaddress
import json
import os
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# BOT_INFO_URL (public deployments) tells site owners who is crawling and how to opt out.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 "
      "SEO-GEO-AEO-Audit/1.0" + (f" (+{os.getenv('BOT_INFO_URL')})" if os.getenv("BOT_INFO_URL") else ""))

SEVERITY_WEIGHT = {"high": 15, "medium": 7, "low": 3}
AREAS = ("SEO", "AEO", "GEO")


class FetchError(Exception):
    pass


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en;q=0.9,*;q=0.5"})
    s.max_redirects = 10
    return s


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("empty URL")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc or "." not in p.hostname:
        raise ValueError(f"not a valid website URL: {url}")
    return url


def _non_public(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified


def _pinned_create_connection(address, *args, **kwargs):
    """Every urllib3 (requests) socket connects only to an IP that was checked to be public, closing the
    DNS-rebinding gap between is_public_url() and the real connection. TLS still verifies the hostname."""
    host, port = address
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    public = [i[4][0] for i in infos if not _non_public(i[4][0])]
    if not public:
        raise OSError(f"refusing to connect to non-public address for {host}")
    return _orig_create_connection((public[0], port), *args, **kwargs)


import urllib3.util.connection as _u3conn  # noqa: E402

_orig_create_connection = _u3conn.create_connection
_u3conn.create_connection = _pinned_create_connection


def is_public_url(url: str) -> bool:
    """False for loopback, private, link-local or reserved addresses."""
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def fetch(sess: requests.Session, url: str, deadline_s: float = 25, max_bytes: int = 8_000_000,
          attempts: int = 3) -> requests.Response:
    """GET with a total deadline (requests' timeout is per read, so a slow-trickling
    server could stall forever). Redirect targets are re-checked against the SSRF guard."""
    last = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 * attempt)  # transient TLS/connection drops usually clear within seconds
        start = time.monotonic()
        try:
            r = sess.get(url, timeout=(8, 10), allow_redirects=True, stream=True)
            if r.url != url and not is_public_url(r.url):
                r.close()
                raise FetchError(f"redirected to a non-public address: {r.url}")
            chunks, size = [], 0
            for chunk in r.iter_content(65536):
                chunks.append(chunk)
                size += len(chunk)
                if time.monotonic() - start > deadline_s:
                    r.close()
                    raise FetchError(f"no complete response within {deadline_s:.0f}s")
                if size > max_bytes:
                    break
            r._content = b"".join(chunks)
            return r
        except requests.TooManyRedirects as e:
            last = e
            break  # a loop does not heal on retry; don't hammer the server
        except (requests.RequestException, FetchError) as e:
            last = e
    raise FetchError(f"{type(last).__name__}: {str(last)[:200]}")


def robots_groups(text: str) -> dict[str, list[tuple[bool, str]]]:
    """robots.txt -> {agent token: [(allow, path pattern)]}, groups for the same agent merged (RFC 9309).
    urllib.robotparser is not used: it matches agents by substring, keeps only the first `*` group,
    applies the first matching rule instead of the longest and ignores `*` / `$` wildcards."""
    groups, agents, in_rules = {}, [], False
    for line in (text or "").lstrip("﻿").splitlines():
        key, _, val = line.split("#", 1)[0].partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            token = re.match(r"[\w*-]*", val).group(0).lower()  # "GPTBot/1.1" -> "gptbot"
            if token:
                agents.append(token)
                groups.setdefault(token, [])
        elif key in ("allow", "disallow", "crawl-delay") and agents:
            in_rules = True
            if key != "crawl-delay" and val:
                for a in agents:
                    groups[a].append((key == "allow", val))
    return groups


def robots_allowed(text: str, agent: str, url: str) -> bool:
    """Google/RFC 9309 semantics: exact agent token (else `*`), longest matching rule wins, Allow wins ties."""
    groups = robots_groups(text)
    rules = groups.get(agent.lower(), groups.get("*", []))
    p = urlparse(url)
    path = (p.path or "/") + ("?" + p.query if p.query else "")
    best_len, allow = -1, True
    for is_allow, pat in rules:
        rx = re.escape(pat).replace(r"\*", ".*")
        rx = rx[:-2] + "$" if rx.endswith(r"\$") else rx
        if re.match(rx, path) and (len(pat) > best_len or len(pat) == best_len and is_allow):
            best_len, allow = len(pat), is_allow
    return allow


def finding(area: str, severity: str, title: str, detail: str, fix: str,
            urls: list | None = None, snippet: str | None = None, check: str = "") -> dict:
    assert area in AREAS and severity in SEVERITY_WEIGHT
    return {"area": area, "severity": severity, "title": title, "detail": detail, "fix": fix,
            "urls": (urls or [])[:10], "affected": len(urls or []), "snippet": snippet, "check": check}


def passed(area: str, title: str, check: str = "") -> dict:
    """A check that is fine - shown in the report as proof of what was verified."""
    return {"area": area, "title": title, "check": check}


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "site").removeprefix("www.")


def run_dir(url: str) -> Path:
    d = ROOT / ".tmp" / domain_of(url)
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def env(key: str) -> str | None:
    return os.getenv(key) or None
