"""Discovery: find public repos that reference StepSecurity actions.

Two signals:
1. Code search for `step-security/<action>` inside .github/workflows and
   action.yml files. GitHub caps any single query at 1,000 results, so we
   bisect on the `size:` qualifier (file size in bytes) until every bucket
   fits under the cap.
2. Issue/PR search for pull requests opened by the StepSecurity bot
   (the secure-repo app). Bisected on `created:` date ranges.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from typing import Callable

from .gh import GitHub, pages_for

log = logging.getLogger(__name__)

SEARCH_CAP = 1000
MAX_INDEXED_FILE_BYTES = 393_216  # code search indexes files < 384 KB

STEP_SECURITY_ORG = "step-security"
BOT_LOGINS = ("step-security-bot",)
SECURE_REPO_PR_TITLE = "[StepSecurity] Apply security best practices"


def _cache_path(state_dir: str, key: str) -> str:
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return os.path.join(state_dir, "discovery", f"{h}.json")


def _cached(state_dir: str, key: str, fn: Callable[[], list]) -> list:
    p = _cache_path(state_dir, key)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    result = fn()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(result, f)
    return result


# --------------------------------------------------------------------- code
def list_step_security_actions(gh: GitHub) -> list[str]:
    """All public repos in the step-security org (candidate action names)."""
    names = []
    for r in gh.paginate(f"/orgs/{STEP_SECURITY_ORG}/repos", {"type": "public"}):
        names.append(r["name"])
    log.info("step-security org has %d public repos", len(names))
    return sorted(set(names))


def _slim_code_item(item: dict) -> dict:
    repo = item.get("repository") or {}
    owner = repo.get("owner") or {}
    return {
        "repo": repo.get("full_name"),
        "repo_id": repo.get("id"),
        "owner": owner.get("login"),
        "owner_type": owner.get("type"),
        "fork": repo.get("fork"),
        "path": item.get("path"),
        "sha": item.get("sha"),
        "html_url": item.get("html_url"),
    }


def code_search_all(gh: GitHub, query: str, state_dir: str, lo: int = 0, hi: int = MAX_INDEXED_FILE_BYTES, smoke: bool = False) -> list[dict]:
    """Return every code-search hit for `query`, bisecting size ranges past the cap.

    smoke=True fetches a single page of the unsliced query (for pipeline tests).
    """
    out: list[dict] = []
    stack = [(lo, hi)]
    while stack:
        a, b = stack.pop()
        q = query if smoke else f"{query} size:{a}..{b}"

        def fetch_bucket(q=q, a=a, b=b):
            first = gh.search("code", q, page=1)
            total = first.get("total_count", 0)
            if smoke:
                return {"split": False, "total": total, "items": [_slim_code_item(i) for i in first.get("items", [])]}
            if total > SEARCH_CAP and b > a:
                return {"split": True, "total": total}
            if total > SEARCH_CAP:
                log.warning("bucket %s has %d hits at a single size; truncated at %d", q, total, SEARCH_CAP)
            items = [_slim_code_item(i) for i in first.get("items", [])]
            for page in range(2, pages_for(total) + 1):
                data = gh.search("code", q, page=page)
                items.extend(_slim_code_item(i) for i in data.get("items", []))
            return {"split": False, "total": total, "items": items}

        res = _cached(state_dir, ("smoke:" if smoke else "code:") + q, lambda: [fetch_bucket()])[0]
        if res.get("split"):
            mid = (a + b) // 2
            log.info("%s -> %d hits, splitting", q, res["total"])
            stack.append((a, mid))
            stack.append((mid + 1, b))
        else:
            log.info("%s -> %d hits", q, res["total"])
            out.extend(res.get("items", []))
    return out


def discover_code(gh: GitHub, state_dir: str, actions: list[str] | None = None, smoke: bool = False) -> dict[str, list[dict]]:
    """Map repo full_name -> list of file hits mentioning StepSecurity."""
    if smoke:
        actions = ["harden-runner"]
    elif not actions:
        actions = list_step_security_actions(gh)
    queries = []
    for name in actions:
        queries.append(f'"{STEP_SECURITY_ORG}/{name}" path:.github/workflows')
    if not smoke:
        # composite actions / reusable workflows shipped outside .github/workflows
        queries.append(f'"{STEP_SECURITY_ORG}/harden-runner" path:action.yml')
        queries.append(f'"{STEP_SECURITY_ORG}/harden-runner" path:action.yaml')
        # comment marker left by the secure-repo tool
        queries.append(f'"github.com/{STEP_SECURITY_ORG}/secure-repo" path:.github/workflows')

    hits: dict[str, dict[str, dict]] = {}
    for q in queries:
        for item in code_search_all(gh, q, state_dir, smoke=smoke):
            repo = item.get("repo")
            if not repo or not item.get("path"):
                continue
            hits.setdefault(repo, {})[item["path"]] = item
    result = {repo: list(files.values()) for repo, files in hits.items()}
    log.info("code discovery: %d repos, %d files", len(result), sum(len(v) for v in result.values()))
    return result


# ---------------------------------------------------------------------- PRs
def _slim_issue_item(item: dict) -> dict:
    repo_url = item.get("repository_url") or ""
    repo = "/".join(repo_url.rsplit("/", 2)[-2:]) if repo_url else None
    pr = item.get("pull_request") or {}
    return {
        "repo": repo,
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "author": (item.get("user") or {}).get("login"),
        "created_at": item.get("created_at"),
        "closed_at": item.get("closed_at"),
        "merged_at": pr.get("merged_at"),
        "html_url": item.get("html_url"),
    }


def issue_search_all(gh: GitHub, query: str, state_dir: str, start: dt.date, end: dt.date, smoke: bool = False) -> list[dict]:
    out: list[dict] = []
    stack = [(start, end)]
    while stack:
        a, b = stack.pop()
        q = query if smoke else f"{query} created:{a.isoformat()}..{b.isoformat()}"

        def fetch_bucket(q=q, a=a, b=b):
            first = gh.search("issues", q, page=1)
            total = first.get("total_count", 0)
            if total > SEARCH_CAP and b > a and not smoke:
                return {"split": True, "total": total}
            items = [_slim_issue_item(i) for i in first.get("items", [])]
            for page in range(2, 1 if smoke else pages_for(total) + 1):
                data = gh.search("issues", q, page=page)
                items.extend(_slim_issue_item(i) for i in data.get("items", []))
            return {"split": False, "total": total, "items": items}

        res = _cached(state_dir, ("smoke:" if smoke else "issues:") + q, lambda: [fetch_bucket()])[0]
        if res.get("split"):
            mid = a + (b - a) / 2
            log.info("%s -> %d hits, splitting", q, res["total"])
            stack.append((a, mid))
            stack.append((mid + dt.timedelta(days=1), b))
        else:
            log.info("%s -> %d hits", q, res["total"])
            out.extend(res.get("items", []))
    return out


def discover_prs(gh: GitHub, state_dir: str, start: dt.date = dt.date(2021, 1, 1), smoke: bool = False) -> dict[str, list[dict]]:
    """Map repo -> PRs opened by the StepSecurity bot / secure-repo app."""
    end = dt.date.today()
    queries = [f"is:pr author:{login}" for login in BOT_LOGINS]
    if not smoke:
        queries.append(f'is:pr "{SECURE_REPO_PR_TITLE}" in:title')
    seen: dict[str, dict[int, dict]] = {}
    for q in queries:
        for item in issue_search_all(gh, q, state_dir, start, end, smoke=smoke):
            if item.get("repo") and item.get("number"):
                seen.setdefault(item["repo"], {})[item["number"]] = item
    result = {repo: sorted(prs.values(), key=lambda p: p["created_at"] or "") for repo, prs in seen.items()}
    log.info("PR discovery: %d repos, %d PRs", len(result), sum(len(v) for v in result.values()))
    return result
