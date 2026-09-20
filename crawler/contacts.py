"""People behind each repo: recent committers, the person who introduced
harden-runner, and whoever merged the StepSecurity bot's PR.

Emails come from public commit metadata. GitHub's noreply addresses are
dropped; the rest are kept and classified corporate vs. freemail.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict

from .domains import classify_email, is_bot
from .gh import GitHub, GitHubError, gql_str, run_batched

log = logging.getLogger(__name__)

USER_FIELDS = "login name company email websiteUrl location twitterUsername"
COMMIT_NODE = f"nodes {{ oid authoredDate author {{ name email user {{ {USER_FIELDS} }} }} }}"


def _contacts_query(batch: list[dict]) -> str:
    parts = ["query { rateLimit { cost remaining resetAt }"]
    for i, item in enumerate(batch):
        owner, name = item["repo"].split("/", 1)
        parts.append(
            f"r{i}: repository(owner: {gql_str(owner)}, name: {gql_str(name)}) {{ nameWithOwner "
            f"defaultBranchRef {{ target {{ ... on Commit {{ "
            f"recent: history(first: 100) {{ totalCount {COMMIT_NODE} }} "
        )
        for j, p in enumerate(item.get("hr_paths", [])[:2]):
            parts.append(
                f"hr{j}: history(first: 100, path: {gql_str(p)}) {{ totalCount pageInfo {{ hasNextPage endCursor }} {COMMIT_NODE} }} "
            )
        parts.append("} } } ")
        for j, n in enumerate(item.get("pr_numbers", [])[:3]):
            parts.append(
                f"pr{j}: pullRequest(number: {int(n)}) {{ number state mergedAt "
                f"mergedBy {{ login ... on User {{ {USER_FIELDS} }} }} }} "
            )
        parts.append("} ")
    parts.append("}")
    return "".join(parts)


def _person_key(author: dict) -> str | None:
    user = author.get("user") or {}
    if user.get("login"):
        return "login:" + user["login"].lower()
    email = (author.get("email") or "").strip().lower()
    if email and "noreply" not in email:
        return "email:" + email
    name = (author.get("name") or "").strip()
    return ("name:" + name.lower()) if name else None


def _oldest_commit(gh: GitHub, repo: str, path: str, cursor: str | None, max_pages: int = 6) -> dict | None:
    """Walk to the end of a file's history to find its first commit."""
    owner, name = repo.split("/", 1)
    last = None
    for _ in range(max_pages):
        if not cursor:
            break
        q = (
            f"query {{ repository(owner: {gql_str(owner)}, name: {gql_str(name)}) {{ defaultBranchRef {{ target {{ ... on Commit {{ "
            f"history(first: 100, path: {gql_str(path)}, after: {gql_str(cursor)}) {{ pageInfo {{ hasNextPage endCursor }} {COMMIT_NODE} }} "
            f"}} }} }} }} }}"
        )
        try:
            data = gh.graphql(q)
        except GitHubError as e:
            log.warning("history walk failed for %s %s: %s", repo, path, str(e)[:120])
            return last
        hist = (((((data.get("data") or {}).get("repository") or {}).get("defaultBranchRef") or {}).get("target") or {}).get("history") or {})
        nodes = hist.get("nodes") or []
        if nodes:
            last = nodes[-1]
        pi = hist.get("pageInfo") or {}
        cursor = pi.get("endCursor") if pi.get("hasNextPage") else None
    return last


def collect_contacts(gh: GitHub, items: list[dict], batch_size: int = 10, walk_history: bool = True) -> dict[str, dict]:
    """items: [{repo, hr_paths, pr_numbers}] -> repo -> contact summary."""
    out: dict[str, dict] = {}
    for batch, data in run_batched(gh, items, _contacts_query, batch_size):
        d = data.get("data") or {}
        for i, item in enumerate(batch):
            repo = item["repo"]
            node = d.get(f"r{i}")
            if not node:
                out[repo] = {"missing": True}
                continue
            target = ((node.get("defaultBranchRef") or {}).get("target")) or {}
            recent = (target.get("recent") or {}).get("nodes") or []
            people: dict[str, dict] = {}
            counts: Counter = Counter()
            last_seen: dict[str, str] = {}
            for c in recent:
                a = c.get("author") or {}
                user = a.get("user") or {}
                if is_bot(user.get("login"), a.get("name"), a.get("email")):
                    continue
                key = _person_key(a)
                if not key:
                    continue
                counts[key] += 1
                last_seen[key] = max(last_seen.get(key, ""), c.get("authoredDate") or "")
                p = people.setdefault(key, {"login": None, "name": None, "emails": set(), "company": None, "website": None, "location": None, "twitter": None})
                p["login"] = p["login"] or user.get("login")
                p["name"] = p["name"] or user.get("name") or a.get("name")
                p["company"] = p["company"] or user.get("company")
                p["website"] = p["website"] or user.get("websiteUrl")
                p["location"] = p["location"] or user.get("location")
                p["twitter"] = p["twitter"] or user.get("twitterUsername")
                for e in (a.get("email"), user.get("email")):
                    if e and classify_email(e) != "noreply":
                        p["emails"].add(e.strip().lower())

            # who introduced harden-runner: oldest commit touching the workflow file
            adopters = []
            for j, path in enumerate(item.get("hr_paths", [])[:2]):
                hist = target.get(f"hr{j}") or {}
                nodes = hist.get("nodes") or []
                if not nodes:
                    continue
                oldest = nodes[-1]
                pi = hist.get("pageInfo") or {}
                if walk_history and pi.get("hasNextPage") and pi.get("endCursor"):
                    deeper = _oldest_commit(gh, repo, path, pi["endCursor"])
                    if deeper:
                        oldest = deeper
                a = oldest.get("author") or {}
                user = a.get("user") or {}
                if is_bot(user.get("login"), a.get("name"), a.get("email")):
                    # bot-authored (e.g. secure-repo PR); fall back to the next human in the file history
                    humans = [n for n in reversed(nodes) if not is_bot(((n.get("author") or {}).get("user") or {}).get("login"), (n.get("author") or {}).get("name"), (n.get("author") or {}).get("email"))]
                    if humans:
                        a = humans[0].get("author") or {}
                        user = a.get("user") or {}
                adopters.append(
                    {
                        "path": path,
                        "login": user.get("login"),
                        "name": user.get("name") or a.get("name"),
                        "email": a.get("email") if classify_email(a.get("email")) != "noreply" else None,
                        "company": user.get("company"),
                        "date": oldest.get("authoredDate"),
                        "file_commits": hist.get("totalCount"),
                    }
                )

            mergers = []
            for j in range(3):
                pr = node.get(f"pr{j}")
                if pr and pr.get("mergedBy"):
                    mb = pr["mergedBy"]
                    mergers.append({"pr": pr.get("number"), "login": mb.get("login"), "name": mb.get("name"), "email": mb.get("email"), "company": mb.get("company"), "merged_at": pr.get("mergedAt")})

            top = []
            for key, n in counts.most_common(5):
                p = people[key]
                top.append({**p, "emails": sorted(p["emails"]), "commits": n, "last_commit": last_seen.get(key)})
            out[repo] = {
                "missing": False,
                "recent_commits": (target.get("recent") or {}).get("totalCount"),
                "distinct_humans_recent": len(counts),
                "top_committers": top,
                "adopters": adopters,
                "stepsecurity_pr_mergers": mergers,
            }
    return out


def aggregate_people(contacts: dict[str, dict], repo_owner: dict[str, str]) -> list[dict]:
    """Roll per-repo contact summaries up to one row per person."""
    people: dict[str, dict] = {}

    def get(login, name, email):
        key = ("login:" + login.lower()) if login else (("email:" + email.lower()) if email else ("name:" + (name or "").lower()))
        if not key.strip(":"):
            return None
        return people.setdefault(
            key,
            {"login": login, "name": name, "emails": set(), "company": None, "website": None, "location": None, "twitter": None,
             "accounts": set(), "repos": set(), "roles": set(), "commits": 0, "adoptions": [], "last_commit": ""},
        )

    for repo, c in contacts.items():
        if c.get("missing"):
            continue
        acct = repo_owner.get(repo, repo.split("/")[0])
        for rank, tc in enumerate(c.get("top_committers", []), start=1):
            p = get(tc.get("login"), tc.get("name"), (tc.get("emails") or [None])[0])
            if not p:
                continue
            p["emails"].update(tc.get("emails") or [])
            for f in ("company", "website", "location", "twitter", "name", "login"):
                p[f] = p[f] or tc.get(f)
            p["accounts"].add(acct)
            p["repos"].add(repo)
            p["commits"] += tc.get("commits", 0)
            p["last_commit"] = max(p["last_commit"], tc.get("last_commit") or "")
            p["roles"].add("top_committer" if rank <= 3 else "committer")
            if tc.get("login") and tc["login"].lower() == acct.lower():
                p["roles"].add("repo_owner")
        for ad in c.get("adopters", []):
            p = get(ad.get("login"), ad.get("name"), ad.get("email"))
            if not p:
                continue
            if ad.get("email"):
                p["emails"].add(ad["email"].lower())
            p["company"] = p["company"] or ad.get("company")
            p["accounts"].add(acct)
            p["repos"].add(repo)
            p["roles"].add("harden_runner_adopter")
            p["adoptions"].append({"repo": repo, "date": ad.get("date")})
        for m in c.get("stepsecurity_pr_mergers", []):
            p = get(m.get("login"), m.get("name"), m.get("email"))
            if not p:
                continue
            if m.get("email"):
                p["emails"].add(m["email"].lower())
            p["company"] = p["company"] or m.get("company")
            p["accounts"].add(acct)
            p["repos"].add(repo)
            p["roles"].add("merged_stepsecurity_pr")

    rows = []
    for p in people.values():
        emails = sorted(p["emails"])
        corp = [e for e in emails if classify_email(e) == "corporate"]
        rows.append(
            {
                **p,
                "emails": emails,
                "corporate_emails": corp,
                "accounts": sorted(p["accounts"]),
                "repos": sorted(p["repos"]),
                "roles": sorted(p["roles"]),
                "first_adoption": min((a["date"] for a in p["adoptions"] if a.get("date")), default=None),
            }
        )
    rows.sort(key=lambda r: (-len(r["roles"]), -r["commits"]))
    return rows


def account_contact_summary(people: list[dict]) -> dict[str, dict]:
    by_acct: dict[str, dict] = defaultdict(lambda: {"contacts": [], "corporate_emails": set(), "adopters": set()})
    for p in people:
        for acct in p["accounts"]:
            by_acct[acct]["contacts"].append(p)
            by_acct[acct]["corporate_emails"].update(p["corporate_emails"])
            if "harden_runner_adopter" in p["roles"] or "merged_stepsecurity_pr" in p["roles"]:
                by_acct[acct]["adopters"].add(p.get("login") or (p["emails"][0] if p["emails"] else p.get("name") or "?"))
    return by_acct
