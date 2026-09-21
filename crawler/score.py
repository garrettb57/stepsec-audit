"""Orthogonal account fields and a single ranking score.

The old tier ladder mixed usage depth, account value and paid-feature
signals into one label. These fields keep them apart:

- provenance     who put harden-runner there (best available evidence)
- depth_score    0-100, how seriously harden-runner is configured
- path_class     where it runs: hygiene / build / release workflows
- breadth        repos with harden-runner live
- rollout        five or more adoptions inside any 30-day window
- recency        latest push across the account's repos
- suppress_reason  why the account should not be worked as a prospect
- score          fit x depth x recency; fit is 1 for an unsuppressed org

`score` drives priority for contacts, top-N selection and row order.
"""
from __future__ import annotations

import datetime as dt
import os
import re

# Basename patterns for harden-runner workflow files.
PATH_CLASSES = (
    ("hygiene", re.compile(r"^(codeql|scorecard|dependency-review|security|ossf|semgrep|trivy|snyk)", re.I)),
    ("release", re.compile(r"^(release|publish|deploy|docker|cd|goreleaser|npm-publish|pypi)", re.I)),
    ("build", re.compile(r"^(ci|build|test|lint|check|main|pr|pull|push|workflow|unit|integration|e2e|go|python|node|rust|java|maven|gradle)", re.I)),
)

PROVENANCE_ORDER = (
    "org_standard", "app_installed", "secure_repo_self", "human_pr", "hand_written",
    "secure_repo_third_party", "secure_repo_vendor", "secure_repo_unknown", "inherited", "unknown",
)


def path_class(paths: list[str] | None) -> list[str]:
    """Classes present among harden-runner workflow paths, sorted."""
    out: set[str] = set()
    for p in paths or []:
        base = os.path.basename(p)
        for name, rx in PATH_CLASSES:
            if rx.search(base):
                out.add(name)
                break
        else:
            out.add("other")
    return sorted(out)


def provenance(a: dict) -> str:
    """Best available evidence for who introduced harden-runner at this account.
    `a` is an account row; org_standard/inherited are P1 and stay unset."""
    if a.get("org_standard"):
        return "org_standard"
    if a.get("app_installed"):
        return "app_installed"
    if (a.get("secure_repo_self_prs") or 0) > 0:
        return "secure_repo_self"
    if (a.get("human_prs") or 0) > 0:
        return "human_pr"
    bot_prs = sum((a.get(k) or 0) for k in ("secure_repo_third_party_prs", "secure_repo_vendor_prs", "secure_repo_unknown_prs"))
    if (a.get("repos_with_harden_runner") or 0) > 0 and bot_prs == 0 and not (a.get("stepsecurity_prs") or 0):
        return "hand_written"
    if (a.get("secure_repo_third_party_prs") or 0) > 0:
        return "secure_repo_third_party"
    if (a.get("secure_repo_vendor_prs") or 0) > 0:
        return "secure_repo_vendor"
    if (a.get("secure_repo_unknown_prs") or 0) > 0:
        return "secure_repo_unknown"
    if a.get("inherited_repos"):
        return "inherited"
    return "unknown"


def depth_score(a: dict) -> int:
    """0-100. Presence 10; breadth 2/repo up to 30; egress-block share up to 20,
    discounted for accounts with fewer than three repos; allowed endpoints 7;
    policy store 10; disable-sudo 5; expression-managed egress 5; harden-runner
    in release/publish workflows 8; workflow coverage up to 5."""
    hr = a.get("repos_with_harden_runner") or 0
    if hr <= 0:
        return 0
    s = 10.0
    s += min(hr, 15) * 2
    confidence = min(1.0, (hr + 1) / 4)  # 1 repo -> 0.5, 3+ repos -> 1.0
    s += 20 * min(1.0, (a.get("egress_block_repos") or 0) / hr) * confidence
    s += 7 if (a.get("allowed_endpoints_max") or 0) > 0 else 0
    s += 10 if (a.get("policy_store_repos") or 0) > 0 else 0
    s += 5 if (a.get("disable_sudo_repos") or 0) > 0 else 0
    s += 5 if (a.get("egress_expr_repos") or 0) > 0 else 0
    s += 8 if (a.get("path_release_repos") or 0) > 0 else 0
    s += 5 * min(1.0, (a.get("workflow_coverage_pct") or 0) / 100)
    return int(round(min(100.0, s)))


def _parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def rollout(adoption_dates: list[str | None], window_days: int = 30, threshold: int = 5) -> bool:
    days = sorted(d for d in (_parse_date(x) for x in adoption_dates) if d)
    if len(days) < threshold:
        return False
    j = 0
    for i in range(len(days)):
        while (days[i] - days[j]).days > window_days:
            j += 1
        if i - j + 1 >= threshold:
            return True
    return False


def recency_factor(latest_push: str | None, today: dt.date | None = None) -> float:
    d = _parse_date(latest_push)
    if not d:
        return 0.4
    age = ((today or dt.date.today()) - d).days
    if age <= 90:
        return 1.0
    if age <= 180:
        return 0.7
    if age <= 365:
        return 0.4
    return 0.15


def suppress_reasons(a: dict) -> list[str]:
    out = []
    if a.get("app_installed"):
        out.append("app_installed")
    if (a.get("policy_store_repos") or 0) > 0:
        out.append("policy_store")
    if a.get("account_type") == "User":
        out.append("individual")
    if (a.get("reverts") or 0) > 0 and (a.get("repos_with_harden_runner") or 0) == 0:
        out.append("all_reverted")
    if a.get("foundation"):
        out.append("foundation")
    if a.get("demo_like"):
        out.append("demo_like")
    return out


def score_account(a: dict, today: dt.date | None = None) -> dict:
    """Add provenance, depth_score, breadth, rollout, suppress_reason, fit,
    recency_factor and score to an account row. Deterministic for a fixed `today`."""
    a["provenance"] = provenance(a)
    a["depth_score"] = depth_score(a)
    a["breadth"] = a.get("repos_with_harden_runner") or 0
    a["rollout"] = rollout(a.get("adoption_dates") or [])
    a["suppress_reason"] = suppress_reasons(a)
    a["fit"] = 1 if a.get("account_type") == "Organization" and not a["suppress_reason"] else 0
    a["recency_factor"] = recency_factor(a.get("latest_push"), today)
    a["score"] = round(a["fit"] * a["depth_score"] * a["recency_factor"], 1)
    return a


def sort_key(a: dict) -> tuple:
    return (-(a.get("score") or 0), -(a.get("depth_score") or 0), -(a.get("total_stars") or 0), (a.get("account") or "").lower())

