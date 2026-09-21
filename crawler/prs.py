"""StepSecurity pull requests: slimming, provenance parsing, classification.

Three bot identities open "[StepSecurity] Apply security best practices"
PRs and they mean different things:

- step-security-bot        secure-repo one-shot tool; body names the requester
- stepsecurity-app[bot]    the GitHub App; body says "enterprise subscription"
- stepsecurity-int[bot]    integration environment; internal noise

Classification is computed at export time from stored facts, so it costs
no API calls to change.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import statistics

from .gh import GitHub, gql_str, run_batched
from .vendor import APP_BOT, SECURE_REPO_BOT, Staff, is_internal_author

log = logging.getLogger(__name__)

BODY_MAX = 2000
# Verified on real PRs 2026-09-21: "created by [StepSecurity](...) at the request of @login."
REQUESTER_RE = re.compile(r"at the request of\s+@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)", re.IGNORECASE)
REVERT_TITLE_RE = re.compile(r'^Revert\s+"', re.IGNORECASE)
REVERT_BODY_RE = re.compile(r"Reverts\s+[\w.-]+/[\w.-]+#(\d+)", re.IGNORECASE)
APP_BODY_MARKER = "enterprise subscription"
INT_HOST_MARKER = "int1.stepsecurity.io"

PR_CLASSES = (
    "internal", "app_installed", "secure_repo_self", "secure_repo_vendor", "secure_repo_third_party",
    "secure_repo_unknown", "human_pr", "revert",
)


def parse_requester(body: str | None) -> str | None:
    if not body:
        return None
    m = REQUESTER_RE.search(body)
    return m.group(1) if m else None


def parse_revert_target(body: str | None) -> int | None:
    if not body:
        return None
    m = REVERT_BODY_RE.search(body)
    return int(m.group(1)) if m else None


def _parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def merge_latency_h(created_at: str | None, merged_at: str | None) -> float | None:
    a, b = _parse_ts(created_at), _parse_ts(merged_at)
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600, 2)


def slim_pr(item: dict) -> dict:
    """Search-API issue item -> stored PR record. Additive fields are nullable."""
    repo_url = item.get("repository_url") or ""
    repo = "/".join(repo_url.rsplit("/", 2)[-2:]) if repo_url else None
    pr = item.get("pull_request") or {}
    user = item.get("user") or {}
    body = item.get("body")
    body = body[:BODY_MAX] if isinstance(body, str) else None
    created, merged = item.get("created_at"), pr.get("merged_at")
    requester = parse_requester(body)
    if body is not None and user.get("login") == SECURE_REPO_BOT and not requester:
        log.info("requester parse failed for %s#%s (body %d chars)", repo, item.get("number"), len(body))
    return {
        "repo": repo,
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "author": user.get("login"),
        "author_type": user.get("type"),
        "created_at": created,
        "closed_at": item.get("closed_at"),
        "merged_at": merged,
        "merge_latency_h": merge_latency_h(created, merged),
        "html_url": item.get("html_url"),
        "body": body,
        "requested_by": requester,
    }


def migrate_prs(prs: dict[str, list[dict]]) -> tuple[dict[str, list[dict]], int, set[str]]:
    """Add v2 keys as null to existing records and drop internal-bot PRs.
    Returns (prs, dropped_count, owners_targeted_by_internal_bots). Idempotent."""
    out: dict[str, list[dict]] = {}
    dropped = 0
    sandbox_owners: set[str] = set()
    for repo, plist in prs.items():
        kept = []
        for p in plist:
            if is_internal_author(p.get("author")):
                dropped += 1
                sandbox_owners.add(repo.split("/")[0].lower())
                continue
            p.setdefault("author_type", None)
            p.setdefault("body", None)
            if "requested_by" not in p:
                p["requested_by"] = parse_requester(p.get("body"))
            if p.get("merge_latency_h") is None:
                p["merge_latency_h"] = merge_latency_h(p.get("created_at"), p.get("merged_at"))
            kept.append(p)
        if kept:
            out[repo] = kept
    return out, dropped, sandbox_owners


# ------------------------------------------------------------ body backfill
def _body_query(batch: list[tuple[str, int]]) -> str:
    parts = ["query { rateLimit { cost remaining }"]
    for i, (repo, number) in enumerate(batch):
        owner, name = repo.split("/", 1)
        parts.append(
            f"p{i}: repository(owner: {gql_str(owner)}, name: {gql_str(name)}) {{ "
            f"pullRequest(number: {int(number)}) {{ body author {{ login __typename }} }} }} "
        )
    parts.append("}")
    return "".join(parts)


def backfill_bodies(gh: GitHub, prs: dict[str, list[dict]], repos: set[str], batch_size: int = 50) -> int:
    """Fetch bodies for secure-repo PRs (the only ones that name a requester)
    on `repos` that still have body=None. Mutates prs in place; returns count."""
    todo = [(r, p["number"]) for r in repos for p in prs.get(r, []) if p.get("body") is None and p.get("author") == SECURE_REPO_BOT and p.get("number")]
    if not todo:
        return 0
    log.info("pr bodies: backfilling %d secure-repo PRs", len(todo))
    index = {(r, p["number"]): p for r in repos for p in prs.get(r, []) if p.get("number")}
    done = 0
    for batch, data in run_batched(gh, todo, _body_query, batch_size):
        d = data.get("data") or {}
        for i, key in enumerate(batch):
            node = ((d.get(f"p{i}") or {}).get("pullRequest")) or {}
            p = index.get(key)
            if not p:
                continue
            body = node.get("body")
            p["body"] = body[:BODY_MAX] if isinstance(body, str) else ""
            p["requested_by"] = parse_requester(p["body"])
            if not p["requested_by"]:
                log.info("requester parse failed for %s#%s (body %d chars)", key[0], key[1], len(p["body"]))
            done += 1
    return done


# ------------------------------------------------------------ classification
def classify_pr(
    pr: dict,
    owner: str,
    staff: Staff | None = None,
    public_members: set[str] | None = None,
    committers: set[str] | None = None,
) -> str:
    """Return one of PR_CLASSES for a single stored PR record."""
    staff = staff or Staff()
    author = (pr.get("author") or "").strip()
    title = pr.get("title") or ""
    body = pr.get("body") or ""
    if is_internal_author(author) or INT_HOST_MARKER in body:
        return "internal"
    if REVERT_TITLE_RE.match(title):
        return "revert"
    if author.lower() == APP_BOT:
        return "app_installed"
    if author.lower() == SECURE_REPO_BOT:
        req = (pr.get("requested_by") or "").lower()
        if not req:
            return "secure_repo_unknown"
        if staff.is_staff(req):
            return "secure_repo_vendor"
        if req == owner.lower() or req in {m.lower() for m in (public_members or set())} or req in {c.lower() for c in (committers or set())}:
            return "secure_repo_self"
        return "secure_repo_third_party"
    if author.lower() == "ghost" or "[bot]" in author.lower():
        return "internal" if APP_BODY_MARKER in body and INT_HOST_MARKER in body else "app_installed" if APP_BODY_MARKER in body else "human_pr"
    return "human_pr"


def summarize_repo_prs(
    plist: list[dict],
    owner: str,
    staff: Staff | None = None,
    public_members: set[str] | None = None,
    committers: set[str] | None = None,
) -> dict:
    """Per-repo PR provenance fields for export."""
    classes: list[str] = []
    requesters: list[str] = []
    latencies: list[float] = []
    human_authors: list[str] = []
    reverted_prs: list[int] = []
    merged_bot = 0
    for p in sorted(plist, key=lambda x: x.get("created_at") or ""):
        c = classify_pr(p, owner, staff, public_members, committers)
        p["pr_class"] = c
        classes.append(c)
        if p.get("requested_by"):
            requesters.append(p["requested_by"])
        if c == "human_pr" and p.get("author"):
            human_authors.append(p["author"])
        if c == "revert":
            t = parse_revert_target(p.get("body"))
            if t:
                reverted_prs.append(t)
        if p.get("merged_at") and c not in ("revert", "internal"):
            merged_bot += 1
            lat = p.get("merge_latency_h")
            if lat is None:
                lat = merge_latency_h(p.get("created_at"), p.get("merged_at"))
            if lat is not None:
                latencies.append(lat)
    return {
        "pr_classes": sorted(set(classes)),
        "pr_class_counts": {c: classes.count(c) for c in sorted(set(classes))},
        "pr_requesters": sorted(set(requesters)),
        "pr_human_authors": sorted(set(human_authors)),
        "pr_median_latency_h": round(statistics.median(latencies), 2) if latencies else None,
        "reverted": "revert" in classes,
        "reverted_prs": reverted_prs,
        "app_installed": "app_installed" in classes,
        "stepsecurity_prs_merged": merged_bot,
        "internal_only": bool(classes) and set(classes) == {"internal"},
    }
