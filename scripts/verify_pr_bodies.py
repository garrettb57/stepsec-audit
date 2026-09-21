"""One-off check for P0.2: does the issues-search API return `body`, and what
does the secure-repo PR body actually say about the requester?

Run inside Actions (the Claude sandbox proxy blocks these API paths):

    python scripts/verify_pr_bodies.py
"""
from __future__ import annotations

import json
import os
import re
import sys

import requests

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
S = requests.Session()
S.headers.update({"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})

DENY = {"step-security", "step-security-experiments", "step-integration-tests", "harden-runner-canary", "actions-security-demo",
        "varunsh-coder", "ashishkurmi", "sailikhith-stepsecurity", "raj-stepsecurity", "raj-organization-1234",
        "anurags-org-returns", "vamshi-stepsecurity"}
REQ_RE = re.compile(r"at the request of\s+@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)", re.IGNORECASE)


def get(path, **params):
    r = S.get(API + path, params=params, timeout=60)
    rl = {k: r.headers.get(k) for k in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Resource")}
    print(f"\n### GET {path} {params} -> {r.status_code} rate={rl}")
    if r.status_code >= 400:
        print(r.text[:400])
        return None
    return r.json()


def show_body(label, body):
    body = body or ""
    m = REQ_RE.search(body)
    print(f"--- {label} | body_len={len(body)} | requester_match={m.group(1) if m else None}")
    print(body[:1200].replace("\r", ""))
    print("--- end")


def main() -> int:
    if not TOKEN:
        print("no token"); return 1

    # 1. search API: does it return body?
    for q in ("is:pr author:step-security-bot", "is:pr author:app/stepsecurity-app", "is:pr author:stepsecurity-app[bot]"):
        data = get("/search/issues", q=q, per_page=3, sort="created", order="desc")
        if not data:
            continue
        items = data.get("items", [])
        print(f"total_count={data.get('total_count')} items={len(items)}")
        for it in items:
            print(f"keys_has_body={'body' in it} author={it['user']['login']} type={it['user'].get('type')} repo={it['repository_url'].rsplit('/',2)[-2:]} #{it['number']} title={it['title']!r}")
            show_body(f"search {it['html_url']}", it.get("body"))

    # 2. real PRs from state/prs.json across title variants, owners, and bot identities
    with open("state/prs.json") as f:
        prs = json.load(f)
    flat = [dict(p, repo=r) for r, ps in prs.items() for p in ps]

    def ok(p):
        o = p["repo"].split("/")[0].lower()
        return o not in DENY and "stepsecurity" not in o and "step-security" not in o

    picks = []
    seen_titles, seen_owners = set(), set()
    for p in sorted(flat, key=lambda p: p.get("created_at") or "", reverse=True):
        if p.get("author") != "step-security-bot" or not ok(p):
            continue
        t = p["title"]; o = p["repo"].split("/")[0]
        if t in seen_titles and len(picks) >= 3 or o in seen_owners:
            continue
        seen_titles.add(t); seen_owners.add(o); picks.append(p)
        if len(picks) == 6:
            break
    app = [p for p in flat if p.get("author") == "stepsecurity-app[bot]" and ok(p)][:2]
    rev = [p for p in flat if (p.get("title") or "").startswith('Revert "')][:2]
    intb = [p for p in flat if p.get("author") == "stepsecurity-int[bot]"][:1]
    old = [p for p in sorted(flat, key=lambda p: p.get("created_at") or "") if p.get("author") == "step-security-bot" and ok(p)][:2]

    for group, lst in (("secure-repo (recent)", picks), ("secure-repo (oldest)", old), ("app", app), ("revert", rev), ("int", intb)):
        for p in lst:
            o, r = p["repo"].split("/", 1)
            d = get(f"/repos/{o}/{r}/pulls/{p['number']}")
            if not d:
                continue
            u = d.get("user") or {}
            print(f"[{group}] {p['repo']}#{p['number']} author={u.get('login')} type={u.get('type')} created={d.get('created_at')} merged={d.get('merged_at')} merged_by={(d.get('merged_by') or {}).get('login')} title={d.get('title')!r}")
            show_body(f"{group} {d.get('html_url')}", d.get("body"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
