"""Aggregate to account level and write CSVs."""
from __future__ import annotations

import csv
import datetime as dt
import logging
import os
from collections import Counter

from .domains import infer_account_domain
from .vendor import Staff

log = logging.getLogger(__name__)

SEP = "; "


def _j(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple, set)):
        return SEP.join(str(x) for x in v if x is not None and str(x) != "")
    return str(v)


def write_csv(path: str, rows: list[dict], columns: list[str]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: _j(r.get(c)) for c in columns})
    log.info("wrote %s (%d rows)", path, len(rows))


REPO_COLUMNS = [
    "repo", "url", "owner", "owner_type", "description", "homepage", "language", "languages", "topics", "license",
    "stars", "forks", "watchers", "open_issues", "open_prs", "is_fork", "parent", "is_archived", "is_template",
    "security_policy_enabled", "created_at", "pushed_at", "latest_release", "latest_release_at", "funding",
    "uses_harden_runner", "stepsecurity_actions", "other_stepsecurity_actions", "harden_runner_steps",
    "harden_runner_versions", "egress_block", "egress_audit", "egress_unset", "allowed_endpoints_max",
    "disable_sudo_any", "disable_telemetry_any", "policy_store_any", "self_hosted_any", "pinned_sha_ratio",
    "secure_repo_marker", "workflows_total", "workflows_with_stepsecurity", "harden_runner_paths",
    "stepsecurity_prs", "stepsecurity_prs_merged", "stepsecurity_pr_first", "stepsecurity_pr_urls",
    "adoption_date", "adopter_login", "adopter_name", "adopter_email", "top_committers", "recent_commits",
    "distinct_humans_recent", "discovery_sources",
]

ACCOUNT_COLUMNS = [
    "account", "account_type", "account_url", "account_name", "company_hint", "inferred_domain", "domain_source",
    "website", "public_email", "location", "twitter", "description", "is_verified_org",
    "public_repos", "followers", "user_orgs", "account_created_at", "tier", "tier_reason",
    "repos_using_stepsecurity", "repos_with_harden_runner", "repos_bot_pr_only", "repo_list", "total_stars", "max_stars",
    "top_repo", "languages", "first_adoption", "latest_push", "egress_block_repos", "egress_audit_repos",
    "egress_unset_repos", "policy_store_repos", "self_hosted_repos", "disable_telemetry_repos", "avg_pinned_sha_ratio",
    "other_stepsecurity_actions", "workflows_total", "workflows_with_stepsecurity", "workflow_coverage_pct",
    "stepsecurity_prs", "stepsecurity_prs_merged", "stepsecurity_pr_first", "contacts_count", "corporate_emails",
    "corporate_email_domains", "adopters", "top_contacts",
]

CONTACT_COLUMNS = [
    "login", "name", "emails", "corporate_emails", "company", "website", "location", "twitter", "roles",
    "accounts", "repos", "commits", "last_commit", "first_adoption", "profile_url",
]

FILE_COLUMNS = ["repo", "path", "action", "ref", "pinned_sha", "version", "egress_policy", "allowed_endpoints_count",
                "disable_sudo", "disable_telemetry", "policy_store", "self_hosted", "runs_on", "secure_repo_marker"]


def build_repo_rows(repos: dict, wf: dict, prs: dict, contacts: dict, discovered: dict, staff: Staff | None = None) -> list[dict]:
    """One row per repo. Denylisted owners and forks of their repos are dropped;
    the count of dropped rows is returned on the list as `.denylisted`."""
    staff = staff or Staff()
    rows = []
    denylisted = 0
    for repo in sorted(set(repos) | set(wf) | set(prs)):
        r = dict(repos.get(repo) or {"repo": repo})
        if r.get("missing"):
            continue
        if staff.is_denylisted(repo.split("/")[0]) or (r.get("is_fork") and staff.is_denylisted((r.get("parent") or "").split("/")[0])):
            denylisted += 1
            continue
        r.update(wf.get(repo) or {})
        pr_list = prs.get(repo) or []
        merged = [p for p in pr_list if p.get("merged_at")]
        r["stepsecurity_prs"] = len(pr_list)
        r["stepsecurity_prs_merged"] = len(merged)
        r["stepsecurity_pr_first"] = min((p["created_at"] for p in pr_list if p.get("created_at")), default=None)
        r["stepsecurity_pr_urls"] = [p["html_url"] for p in pr_list[:3]]
        c = contacts.get(repo) or {}
        adopters = c.get("adopters") or []
        dates = [a["date"] for a in adopters if a.get("date")]
        if merged:
            dates.extend(p["merged_at"] for p in merged)
        r["adoption_date"] = min(dates) if dates else None
        if adopters:
            a = sorted(adopters, key=lambda x: x.get("date") or "")[0]
            r["adopter_login"], r["adopter_name"], r["adopter_email"] = a.get("login"), a.get("name"), a.get("email")
        r["top_committers"] = [f"{t.get('login') or t.get('name')} ({t.get('commits')})" for t in c.get("top_committers", [])[:3]]
        r["recent_commits"] = c.get("recent_commits")
        r["distinct_humans_recent"] = c.get("distinct_humans_recent")
        src = []
        if discovered.get(repo):
            src.append("code_search")
        if pr_list:
            src.append("bot_pr")
        r["discovery_sources"] = src
        if not r.get("owner"):
            r["owner"] = repo.split("/")[0]
        rows.append(r)
    rows.sort(key=lambda x: -(x.get("stars") or 0))
    rows = RepoRows(rows)
    rows.denylisted = denylisted
    return rows


class RepoRows(list):
    denylisted = 0


def _tier(acct: dict, owner: dict) -> tuple[str, str]:
    if acct["policy_store_repos"]:
        return "likely_customer", "harden-runner policy store / API key in use (paid tier feature)"
    if (owner.get("type") or acct.get("account_type")) == "User":
        return "individual", "user-owned account"
    if acct["self_hosted_repos"]:
        return "power_user", "harden-runner on self-hosted runners"
    if acct["repos_with_harden_runner"] == 0:
        if acct["repos_using_stepsecurity"]:
            return "other_actions_only", "uses StepSecurity maintained actions but not harden-runner"
        return "bot_pr_only", "StepSecurity bot PRs found but harden-runner not live on default branch"
    if acct["egress_block_repos"] and (acct["repos_with_harden_runner"] >= 3 or (acct["workflow_coverage_pct"] or 0) >= 50):
        return "power_user", "egress block enabled across multiple repos or majority of workflows"
    if acct["egress_block_repos"]:
        return "adopter_block", "harden-runner with egress block in at least one repo"
    return "adopter_audit", "harden-runner in audit-only / default mode"


def build_account_rows(repo_rows: list[dict], owners: dict, people_by_acct: dict) -> list[dict]:
    """One row per owner. Forks are listed in repos.csv but do not count as adoption."""
    by_acct: dict[str, list[dict]] = {}
    for r in repo_rows:
        if r.get("is_fork"):
            continue
        by_acct.setdefault(r["owner"], []).append(r)
    rows = []
    for acct, reps in by_acct.items():
        o = owners.get(acct) or {}
        hr = [r for r in reps if r.get("uses_harden_runner")]
        ss = [r for r in reps if r.get("uses_stepsecurity")]
        langs = Counter(r.get("language") for r in reps if r.get("language"))
        wf_total = sum(r.get("workflows_total") or 0 for r in reps)
        wf_ss = sum(r.get("workflows_with_stepsecurity") or 0 for r in reps)
        pinned = [r["pinned_sha_ratio"] for r in reps if r.get("pinned_sha_ratio") is not None]
        top_repo = max(reps, key=lambda r: r.get("stars") or 0)
        ppl = people_by_acct.get(acct) or {"contacts": [], "corporate_emails": set(), "adopters": set()}
        corp_emails = sorted(ppl["corporate_emails"])
        committer_emails = [e for p in ppl["contacts"] for e in p["emails"]]
        domain, source = infer_account_domain(o, [r.get("homepage") for r in reps], committer_emails, login=acct)
        other_actions = sorted({a for r in reps for a in (r.get("other_stepsecurity_actions") or [])})
        a = {
            "account": acct,
            "account_type": o.get("type") or (reps[0].get("owner_type")),
            "account_url": o.get("url") or f"https://github.com/{acct}",
            "account_name": o.get("name"),
            "company_hint": o.get("company") if o.get("type") == "User" else o.get("name"),
            "inferred_domain": domain,
            "domain_source": source,
            "website": o.get("websiteUrl"),
            "public_email": o.get("email"),
            "location": o.get("location"),
            "twitter": o.get("twitter"),
            "description": o.get("description"),
            "is_verified_org": o.get("is_verified"),
            "public_repos": o.get("public_repos"),
            "followers": o.get("followers"),
            "user_orgs": o.get("user_orgs"),
            "account_created_at": o.get("created_at"),
            "repos_using_stepsecurity": len(ss),
            "repos_with_harden_runner": len(hr),
            "repos_bot_pr_only": sum(1 for r in reps if not r.get("uses_stepsecurity") and r.get("stepsecurity_prs")),
            "repo_list": [r["repo"] for r in sorted(reps, key=lambda r: -(r.get("stars") or 0))],
            "total_stars": sum(r.get("stars") or 0 for r in reps),
            "max_stars": top_repo.get("stars"),
            "top_repo": top_repo["repo"],
            "languages": [l for l, _ in langs.most_common(3)],
            "first_adoption": min((r["adoption_date"] for r in reps if r.get("adoption_date")), default=None),
            "latest_push": max((r["pushed_at"] for r in reps if r.get("pushed_at")), default=None),
            "egress_block_repos": sum(1 for r in hr if r.get("egress_block")),
            "egress_audit_repos": sum(1 for r in hr if r.get("egress_audit") and not r.get("egress_block")),
            "egress_unset_repos": sum(1 for r in hr if not r.get("egress_block") and not r.get("egress_audit")),
            "policy_store_repos": sum(1 for r in hr if r.get("policy_store_any")),
            "self_hosted_repos": sum(1 for r in hr if r.get("self_hosted_any")),
            "disable_telemetry_repos": sum(1 for r in hr if r.get("disable_telemetry_any")),
            "avg_pinned_sha_ratio": round(sum(pinned) / len(pinned), 2) if pinned else None,
            "other_stepsecurity_actions": other_actions,
            "workflows_total": wf_total,
            "workflows_with_stepsecurity": wf_ss,
            "workflow_coverage_pct": round(100 * wf_ss / wf_total) if wf_total else None,
            "stepsecurity_prs": sum(r.get("stepsecurity_prs") or 0 for r in reps),
            "stepsecurity_prs_merged": sum(r.get("stepsecurity_prs_merged") or 0 for r in reps),
            "stepsecurity_pr_first": min((r["stepsecurity_pr_first"] for r in reps if r.get("stepsecurity_pr_first")), default=None),
            "contacts_count": len(ppl["contacts"]),
            "corporate_emails": corp_emails,
            "corporate_email_domains": sorted({e.split("@")[1] for e in corp_emails}),
            "adopters": sorted(ppl["adopters"]),
            "top_contacts": [
                f"{p.get('login') or p.get('name')}|{p.get('name') or ''}|{(p['corporate_emails'] or p['emails'] or [''])[0]}|{'+'.join(p['roles'])}"
                for p in sorted(ppl["contacts"], key=lambda p: (-len(p["roles"]), -p["commits"]))[:5]
            ],
        }
        a["tier"], a["tier_reason"] = _tier(a, o)
        rows.append(a)
    tier_order = {"likely_customer": 0, "power_user": 1, "adopter_block": 2, "adopter_audit": 3, "other_actions_only": 4, "bot_pr_only": 5, "individual": 6}
    rows.sort(key=lambda a: (tier_order.get(a["tier"], 9), -(a["total_stars"] or 0)))
    return rows


def build_contact_rows(people: list[dict]) -> list[dict]:
    rows = []
    for p in people:
        r = dict(p)
        r["profile_url"] = f"https://github.com/{p['login']}" if p.get("login") else None
        rows.append(r)
    return rows


def build_file_rows(wf_raw: dict) -> list[dict]:
    rows = []
    for repo, s in wf_raw.items():
        for f in s.get("files") or []:
            for u in f.get("usages") or []:
                rows.append({"repo": repo, "path": f["path"], "secure_repo_marker": f.get("secure_repo_marker"), **u})
    return rows


def write_summary(path: str, accounts: list[dict], repos: list[dict], people: list[dict], meta: dict) -> None:
    tiers = Counter(a["tier"] for a in accounts)
    types = Counter(a["account_type"] for a in accounts)
    with_domain = sum(1 for a in accounts if a["inferred_domain"])
    with_corp_email = sum(1 for a in accounts if a["corporate_emails"])
    lines = [
        f"# Crawl summary ({dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
        "",
        f"- Repos discovered: **{meta.get('discovered_repos', 0)}** (code search) + **{meta.get('pr_repos', 0)}** (bot PRs); exported **{len(repos)}**",
        f"- Denylist removed: {meta.get('denylist_repos_removed', 0)} repos, {meta.get('denylist_prs_removed', 0)} PRs; staff logins known: {meta.get('staff_logins', 0)}",
        f"- Accounts: **{len(accounts)}** ({', '.join(f'{k}: {v}' for k, v in types.most_common())})",
        f"- Accounts with an inferred domain: {with_domain} ({100 * with_domain // max(1, len(accounts))}%)",
        f"- Accounts with at least one corporate committer email: {with_corp_email} ({100 * with_corp_email // max(1, len(accounts))}%)",
        f"- People rows: {len(people)}; with any email: {sum(1 for p in people if p['emails'])}; with corporate email: {sum(1 for p in people if p['corporate_emails'])}",
        "",
        "## Tiers",
        "",
        "| tier | accounts |",
        "|---|---|",
    ] + [f"| {t} | {n} |" for t, n in tiers.most_common()] + [
        "",
        "## Phase state",
        "",
    ] + [f"- {k}: {v}" for k, v in meta.items()]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
