"""Aggregate to account level and write CSVs."""
from __future__ import annotations

import csv
import datetime as dt
import logging
import os
from collections import Counter

from .domains import infer_account_domain
from .prs import summarize_repo_prs
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
    "pr_classes", "pr_requesters", "pr_human_authors", "pr_median_latency_h", "reverted", "reverted_prs", "app_installed",
    "adoption_date", "adopter_login", "adopter_name", "adopter_email", "top_committers", "recent_commits",
    "distinct_humans_recent", "discovery_sources", "filtered_reason",
]

INDIVIDUAL_COLUMNS = ["login", "name", "company", "website", "location", "repos", "repos_with_harden_runner", "stepsecurity_prs", "profile_url"]

ACCOUNT_COLUMNS = [
    "account", "account_type", "rank", "account_url", "account_name", "company_hint", "inferred_domain", "domain_source",
    "website", "public_email", "location", "twitter", "description", "is_verified_org",
    "public_repos", "followers", "user_orgs", "account_created_at", "tier", "tier_reason",
    "repos_using_stepsecurity", "repos_with_harden_runner", "repos_bot_pr_only", "repo_list", "total_stars", "max_stars",
    "top_repo", "languages", "first_adoption", "latest_push", "egress_block_repos", "egress_audit_repos",
    "egress_unset_repos", "policy_store_repos", "self_hosted_repos", "disable_telemetry_repos", "avg_pinned_sha_ratio",
    "other_stepsecurity_actions", "workflows_total", "workflows_with_stepsecurity", "workflow_coverage_pct",
    "stepsecurity_prs", "stepsecurity_prs_merged", "stepsecurity_pr_first", "app_installed", "secure_repo_self_prs",
    "secure_repo_vendor_prs", "secure_repo_third_party_prs", "secure_repo_unknown_prs", "human_prs", "reverts",
    "pr_median_latency_h", "contacts_count", "corporate_emails",
    "corporate_email_domains", "adopters", "top_contacts",
]

CONTACT_COLUMNS = [
    "login", "name", "emails", "corporate_emails", "company", "website", "location", "twitter", "roles",
    "accounts", "repos", "commits", "last_commit", "first_adoption", "profile_url",
]

FILE_COLUMNS = ["repo", "path", "action", "ref", "pinned_sha", "version", "egress_policy", "allowed_endpoints_count",
                "disable_sudo", "disable_telemetry", "policy_store", "self_hosted", "runs_on", "secure_repo_marker"]


def build_repo_rows(
    repos: dict, wf: dict, prs: dict, contacts: dict, discovered: dict,
    staff: Staff | None = None, public_members: dict | None = None,
    owner_types: dict | None = None, filtered: dict | None = None, wf_sources: dict | None = None,
) -> list[dict]:
    """One row per repo. Denylisted owners and forks of their repos are dropped;
    the count of dropped rows is returned on the list as `.denylisted`.
    Rows filtered for fork/archived/mirror/template stay, with `filtered_reason`."""
    staff = staff or Staff()
    public_members = public_members or {}
    owner_types = owner_types or {}
    filtered = filtered or {}
    wf_sources = wf_sources or {}
    rows = []
    denylisted = 0
    for repo in sorted(set(repos) | set(wf) | set(prs)):
        r = dict(repos.get(repo) or {"repo": repo})
        if r.get("missing"):
            continue
        r["filtered_reason"] = filtered.get(repo)
        r["owner_type"] = r.get("owner_type") or owner_types.get(repo) or ((discovered.get(repo) or [{}])[0].get("owner_type"))
        if staff.is_denylisted(repo.split("/")[0]) or (r.get("is_fork") and staff.is_denylisted((r.get("parent") or "").split("/")[0])):
            denylisted += 1
            continue
        r.update(wf.get(repo) or {})
        pr_list = prs.get(repo) or []
        merged = [p for p in pr_list if p.get("merged_at")]
        owner_login = r.get("owner") or repo.split("/")[0]
        c = contacts.get(repo) or {}
        committers = {t.get("login") for t in c.get("top_committers", []) if t.get("login")}
        r.update(summarize_repo_prs(pr_list, owner_login, staff, set(public_members.get(owner_login) or []), committers))
        r["stepsecurity_prs"] = len(pr_list)
        r["stepsecurity_pr_first"] = min((p["created_at"] for p in pr_list if p.get("created_at")), default=None)
        r["stepsecurity_pr_urls"] = [p["html_url"] for p in pr_list[:3]]
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
        if wf_sources.get(repo) == "tree_scan":
            src.append("tree_scan")
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


def _pr_count(reps: list[dict], cls: str) -> int:
    return sum((r.get("pr_class_counts") or {}).get(cls, 0) for r in reps)


def _median(vals: list[float]):
    if not vals:
        return None
    vals = sorted(vals)
    n = len(vals)
    return round(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2, 2)


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


def build_account_rows(repo_rows: list[dict], owners: dict, people_by_acct: dict, scores: dict | None = None) -> list[dict]:
    """One row per owner. Filtered repos (forks, archived, mirrors, templates)
    are listed in repos.csv but do not count toward adoption."""
    scores = scores or {}
    by_acct: dict[str, list[dict]] = {}
    for r in repo_rows:
        if r.get("is_fork") or r.get("filtered_reason"):
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
            "account_type": o.get("type") or next((r.get("owner_type") for r in reps if r.get("owner_type")), None),
            "rank": (scores.get(acct) or {}).get("rank"),
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
            "app_installed": any(r.get("app_installed") for r in reps),
            "secure_repo_self_prs": _pr_count(reps, "secure_repo_self"),
            "secure_repo_vendor_prs": _pr_count(reps, "secure_repo_vendor"),
            "secure_repo_third_party_prs": _pr_count(reps, "secure_repo_third_party"),
            "secure_repo_unknown_prs": _pr_count(reps, "secure_repo_unknown"),
            "human_prs": _pr_count(reps, "human_pr"),
            "reverts": _pr_count(reps, "revert"),
            "pr_median_latency_h": _median([r["pr_median_latency_h"] for r in reps if r.get("pr_median_latency_h") is not None]),
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
    rows.sort(key=lambda a: (a.get("rank") if a.get("rank") is not None else 10**9, -(a["total_stars"] or 0)))
    return rows


def build_individual_rows(repos: list[str], owners: dict, wf: dict, prs: dict) -> list[dict]:
    """User-owned accounts: one row per login, unenriched beyond the owner profile."""
    by_login: dict[str, dict] = {}
    for repo in repos:
        login = repo.split("/")[0]
        o = owners.get(login) or {}
        row = by_login.setdefault(login, {
            "login": login, "name": o.get("name"), "company": o.get("company"), "website": o.get("websiteUrl"),
            "location": o.get("location"), "repos": [], "repos_with_harden_runner": 0, "stepsecurity_prs": 0,
            "profile_url": f"https://github.com/{login}",
        })
        row["repos"].append(repo)
        row["repos_with_harden_runner"] += 1 if (wf.get(repo) or {}).get("uses_harden_runner") else 0
        row["stepsecurity_prs"] += len(prs.get(repo) or [])
    return sorted(by_login.values(), key=lambda r: (-r["repos_with_harden_runner"], -len(r["repos"]), r["login"].lower()))


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
        "## Funnel",
        "",
        "| stage | count |",
        "|---|---|",
    ] + [f"| {k} | {v} |" for k, v in (meta.get("funnel") or {}).items() if not isinstance(v, dict)] + [
        "",
        "Filtered by reason: " + ", ".join(f"{k}={v}" for k, v in ((meta.get("funnel") or {}).get("filtered_reasons") or {}).items()),
        "",
        "## Tiers (legacy)",
        "",
        "| tier | accounts |",
        "|---|---|",
    ] + [f"| {t} | {n} |" for t, n in tiers.most_common()] + [
        "",
        "## Phases (cumulative across runs)",
        "",
        "| phase | wall (min) | last run (min) | requests by bucket | rows |",
        "|---|---|---|---|---|",
    ] + [
        f"| {name} | {p.get('wall_s', 0) / 60:.1f} | {p.get('last_run_wall_s', 0) / 60:.1f} | {', '.join(f'{b}={n}' for b, n in (p.get('requests') or {}).items()) or '-'} | {p.get('rows', 0)} |"
        for name, p in (meta.get("phases") or {}).items()
    ] + [
        "",
        f"Requests this run by bucket: {meta.get('requests_by_bucket_this_run')}; rate limits seen: {meta.get('rate_limits')}",
        f"Remaining work: {meta.get('remaining_work') or 'none'}",
        "",
        "## Phase state",
        "",
    ] + [f"- {k}: {v}" for k, v in meta.items() if k not in ("phases", "funnel")]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
