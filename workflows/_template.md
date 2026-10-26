# Workflow: <name>

> Copy this file to `workflows/<snake_case_name>.md` and fill it in. Keep it current as you learn.

## Objective
What this workflow achieves and for whom (client site or own site). One or two sentences.

## Required inputs
| Input | Example | Notes |
|-------|---------|-------|
| Target URL / domain | `https://example.com` | |
| Competitors (optional) | `a.com, b.com` | |

## Tools (in order)
1. `python tools/<script>.py --arg ...` — what it does, what it writes to `.tmp/`
2. ...

Prefer free sources. Any paid API call needs explicit approval first.

## Steps
1. ...
2. ...

## Outputs
- Intermediates: `.tmp/<name>/...` (disposable)
- Deliverable: Google Sheet / Slides link (cloud), plus what it contains

## Edge cases & failure handling
- Rate limits / quotas: ...
- Site blocks crawler / robots.txt disallows: ...
- Missing data: ...

## Learnings
Dated notes on quirks, limits and better approaches discovered while running this workflow.
- YYYY-MM-DD: ...
