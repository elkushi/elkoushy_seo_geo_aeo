"""Smoke test for the project environment.

Checks that required packages import, reports which keys from .env.example are
set in .env, and whether Google OAuth credentials exist. With --live, makes one
free PageSpeed Insights request to confirm network/API access.

Usage:
    python tools/check_setup.py [--live]

Exit code 0 if all packages import (and the live check passes, if requested), else 1.
"""

import argparse
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# import name -> pip package name
PACKAGES = {
    "dotenv": "python-dotenv",
    "requests": "requests",
    "bs4": "beautifulsoup4",
    "lxml": "lxml",
    "extruct": "extruct",
    "advertools": "advertools",
    "googleapiclient": "google-api-python-client",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "google_auth_httplib2": "google-auth-httplib2",
    "ddgs": "ddgs",
    "pandas": "pandas",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "jinja2": "jinja2",
    "openai": "openai",
}

PAGESPEED_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


def check_packages():
    ok = True
    print("Packages:")
    for module, pip_name in PACKAGES.items():
        try:
            importlib.import_module(module)
            print(f"  OK       {pip_name}")
        except Exception as exc:
            ok = False
            print(f"  MISSING  {pip_name} ({exc.__class__.__name__}: {exc})")
    return ok


def env_keys():
    """Uncommented variable names declared in .env.example."""
    keys = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.split("=", 1)[0].strip())
    return keys


def mask(value):
    return value[:4] + "..." if len(value) > 8 else "set"


def check_env():
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    print("\nEnvironment (.env):")
    for key in env_keys():
        value = os.getenv(key, "")
        print(f"  {'SET' if value else 'missing':<8} {key}" + (f" = {mask(value)}" if value else ""))

    secrets = ROOT / os.getenv("GOOGLE_CLIENT_SECRETS", "credentials.json")
    print("\nGoogle OAuth:")
    print(f"  {'OK' if secrets.exists() else 'missing':<8} {secrets.name}")
    print(f"  {'OK' if (ROOT / 'token.json').exists() else 'missing':<8} token.json (created on first login)")


def check_live():
    import requests

    params = {"url": "https://example.com", "strategy": "mobile", "category": "performance"}
    key = os.getenv("PAGESPEED_API_KEY")
    if key:
        params["key"] = key
    print(f"\nLive PageSpeed check ({'with key' if key else 'keyless'}):")
    try:
        resp = requests.get(PAGESPEED_URL, params=params, timeout=90)
    except requests.RequestException as exc:
        print(f"  FAIL     network error: {exc}")
        return False
    if resp.status_code == 429 or (resp.status_code == 403 and not key):
        print("  QUOTA    keyless quota exhausted - add PAGESPEED_API_KEY to .env")
        return False
    if not resp.ok:
        print(f"  FAIL     HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    score = resp.json()["lighthouseResult"]["categories"]["performance"]["score"]
    print(f"  OK       example.com mobile performance score: {round(score * 100)}")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="also make one free PageSpeed API call")
    args = parser.parse_args()

    ok = check_packages()
    if ok:
        check_env()
        if args.live:
            ok = check_live()
    print("\nResult:", "OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
