"""Repository and owner enrichment via batched GraphQL."""
from __future__ import annotations

import logging

from .gh import GitHub, gql_str, run_batched

log = logging.getLogger(__name__)

# Light pass: every surviving repo. Cheap scalars and single-hop fields only.
REPO_FIELDS_LIGHT = """
  nameWithOwner name url description homepageUrl
  isFork isArchived isMirror isTemplate isInOrganization isSecurityPolicyEnabled
  stargazerCount forkCount createdAt pushedAt updatedAt
  primaryLanguage { name }
  licenseInfo { spdxId }
  defaultBranchRef { name }
  parent { nameWithOwner }
  owner { login __typename url }
"""

# Deep pass: repos of the top-N accounts only. Connections cost more.
REPO_FIELDS_DEEP = """
  nameWithOwner
  hasIssuesEnabled diskUsage
  watchers { totalCount }
  openIssues: issues(states: OPEN) { totalCount }
  openPRs: pullRequests(states: OPEN) { totalCount }
  languages(first: 5, orderBy: {field: SIZE, direction: DESC}) { nodes { name } }
  repositoryTopics(first: 20) { nodes { topic { name } } }
  latestRelease { tagName publishedAt }
  fundingLinks { platform url }
"""

REPO_FIELDS = REPO_FIELDS_LIGHT + REPO_FIELDS_DEEP  # legacy: full fetch in one go


def _repo_query(batch: list[str], fields: str = REPO_FIELDS) -> str:
    parts = ["query { rateLimit { cost remaining resetAt }"]
    for i, repo in enumerate(batch):
        owner, name = repo.split("/", 1)
        parts.append(f"r{i}: repository(owner: {gql_str(owner)}, name: {gql_str(name)}) {{ {fields} }} ")
    parts.append("}")
    return "".join(parts)


def _flatten_repo(node: dict) -> dict:
    """Flatten whichever fields are present; absent ones come out as None/[]."""
    return {
        "repo": node.get("nameWithOwner"),
        "name": node.get("name"),
        "url": node.get("url"),
        "description": (node.get("description") or "").strip(),
        "homepage": node.get("homepageUrl"),
        "is_fork": node.get("isFork"),
        "parent": (node.get("parent") or {}).get("nameWithOwner"),
        "is_archived": node.get("isArchived"),
        "is_mirror": node.get("isMirror"),
        "is_template": node.get("isTemplate"),
        "is_in_organization": node.get("isInOrganization"),
        "security_policy_enabled": node.get("isSecurityPolicyEnabled"),
        "stars": node.get("stargazerCount"),
        "forks": node.get("forkCount"),
        "watchers": (node.get("watchers") or {}).get("totalCount"),
        "open_issues": (node.get("openIssues") or {}).get("totalCount"),
        "open_prs": (node.get("openPRs") or {}).get("totalCount"),
        "disk_kb": node.get("diskUsage"),
        "created_at": node.get("createdAt"),
        "pushed_at": node.get("pushedAt"),
        "updated_at": node.get("updatedAt"),
        "language": (node.get("primaryLanguage") or {}).get("name"),
        "languages": [n["name"] for n in ((node.get("languages") or {}).get("nodes") or [])],
        "license": (node.get("licenseInfo") or {}).get("spdxId"),
        "topics": [n["topic"]["name"] for n in ((node.get("repositoryTopics") or {}).get("nodes") or []) if n.get("topic")],
        "default_branch": (node.get("defaultBranchRef") or {}).get("name"),
        "latest_release": (node.get("latestRelease") or {}).get("tagName"),
        "latest_release_at": (node.get("latestRelease") or {}).get("publishedAt"),
        "funding": [f"{f['platform']}:{f['url']}" for f in (node.get("fundingLinks") or []) if f.get("url")],
        "owner": (node.get("owner") or {}).get("login"),
        "owner_type": (node.get("owner") or {}).get("__typename"),
    }


def enrich_repos(gh: GitHub, repos: list[str], batch_size: int = 100, fields: str = REPO_FIELDS_LIGHT) -> dict[str, dict]:
    """Light metadata for every repo in `repos`. run_batched halves the batch on 502."""
    out: dict[str, dict] = {}
    for batch, data in run_batched(gh, repos, lambda b: _repo_query(b, fields), batch_size):
        d = data.get("data") or {}
        for i, repo in enumerate(batch):
            node = d.get(f"r{i}")
            out[repo] = _flatten_repo(node) | {"deep": fields is REPO_FIELDS} if node else {"repo": repo, "missing": True}
    log.info("enriched %d repos (%d missing)", len(out), sum(1 for v in out.values() if v.get("missing")))
    return out


DEEP_KEYS = ("watchers", "open_issues", "open_prs", "disk_kb", "languages", "topics", "latest_release", "latest_release_at", "funding")


def enrich_repos_deep(gh: GitHub, repos: dict[str, dict], names: list[str], batch_size: int = 25) -> int:
    """Merge the deep fields into existing light records. Returns count updated."""
    todo = [n for n in names if n in repos and not repos[n].get("missing") and not repos[n].get("deep")]
    if not todo:
        return 0
    n = 0
    for batch, data in run_batched(gh, todo, lambda b: _repo_query(b, REPO_FIELDS_DEEP), batch_size):
        d = data.get("data") or {}
        for i, repo in enumerate(batch):
            node = d.get(f"r{i}")
            if not node:
                continue
            flat = _flatten_repo(node)
            repos[repo].update({k: flat.get(k) for k in DEEP_KEYS})
            repos[repo]["deep"] = True
            n += 1
    log.info("deep-enriched %d repos", n)
    return n


OWNER_FIELDS = """
  login __typename url
  ... on Organization {
    name description email websiteUrl location twitterUsername isVerified createdAt avatarUrl
    publicRepos: repositories(privacy: PUBLIC) { totalCount }
    hasSponsorsListing
  }
  ... on User {
    name email company websiteUrl location bio twitterUsername isHireable createdAt avatarUrl
    followers { totalCount }
    publicRepos: repositories(privacy: PUBLIC) { totalCount }
    organizations(first: 10) { nodes { login name websiteUrl } }
    hasSponsorsListing
  }
"""


def _owner_query(batch: list[str]) -> str:
    parts = ["query { rateLimit { cost remaining resetAt }"]
    for i, login in enumerate(batch):
        parts.append(f"o{i}: repositoryOwner(login: {gql_str(login)}) {{ {OWNER_FIELDS} }} ")
    parts.append("}")
    return "".join(parts)


def _flatten_owner(node: dict) -> dict:
    orgs = [(n.get("login"), n.get("name"), n.get("websiteUrl")) for n in ((node.get("organizations") or {}).get("nodes") or [])]
    return {
        "login": node.get("login"),
        "type": node.get("__typename"),
        "url": node.get("url"),
        "name": node.get("name"),
        "description": (node.get("description") or node.get("bio") or "").strip(),
        "email": node.get("email"),
        "websiteUrl": node.get("websiteUrl"),
        "location": node.get("location"),
        "twitter": node.get("twitterUsername"),
        "company": node.get("company"),
        "is_verified": node.get("isVerified"),
        "is_hireable": node.get("isHireable"),
        "created_at": node.get("createdAt"),
        "public_repos": (node.get("publicRepos") or {}).get("totalCount"),
        "followers": (node.get("followers") or {}).get("totalCount"),
        "has_sponsors": node.get("hasSponsorsListing"),
        "user_orgs": [o[0] for o in orgs if o[0]],
        "user_org_sites": [o[2] for o in orgs if o[2]],
    }


def enrich_owners(gh: GitHub, logins: list[str], batch_size: int = 50) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for batch, data in run_batched(gh, logins, _owner_query, batch_size):
        d = data.get("data") or {}
        for i, login in enumerate(batch):
            node = d.get(f"o{i}")
            out[login] = _flatten_owner(node) if node else {"login": login, "missing": True}
    log.info("enriched %d owners", len(out))
    return out


def public_members(gh: GitHub, orgs: list[str], existing: dict | None = None, max_pages: int = 1) -> dict[str, list[str]]:
    """login -> public member logins (first page, 100). Only public members are
    visible for orgs the token is not a member of. Users (not orgs) yield []."""
    out = dict(existing or {})
    todo = [o for o in orgs if o not in out]
    for org in todo:
        try:
            members = [m.get("login") for m in gh.paginate(f"/orgs/{org}/public_members", max_pages=max_pages)]
            out[org] = [m for m in members if m]
        except Exception as e:  # noqa: BLE001 - 404 for users, 403 for odd orgs
            log.debug("public_members(%s): %s", org, str(e)[:80])
            out[org] = []
    if todo:
        log.info("public members fetched for %d orgs", len(todo))
    return out
