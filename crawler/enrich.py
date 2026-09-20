"""Repository and owner enrichment via batched GraphQL."""
from __future__ import annotations

import logging

from .gh import GitHub, gql_str, run_batched

log = logging.getLogger(__name__)

REPO_FIELDS = """
  nameWithOwner name url description homepageUrl
  isFork isArchived isMirror isTemplate isInOrganization isSecurityPolicyEnabled hasIssuesEnabled
  stargazerCount forkCount diskUsage
  watchers { totalCount }
  openIssues: issues(states: OPEN) { totalCount }
  openPRs: pullRequests(states: OPEN) { totalCount }
  createdAt pushedAt updatedAt
  primaryLanguage { name }
  languages(first: 5, orderBy: {field: SIZE, direction: DESC}) { nodes { name } }
  licenseInfo { spdxId }
  repositoryTopics(first: 20) { nodes { topic { name } } }
  defaultBranchRef { name }
  latestRelease { tagName publishedAt }
  fundingLinks { platform url }
  parent { nameWithOwner }
  owner { login __typename url }
"""


def _repo_query(batch: list[str]) -> str:
    parts = ["query { rateLimit { cost remaining resetAt }"]
    for i, repo in enumerate(batch):
        owner, name = repo.split("/", 1)
        parts.append(f"r{i}: repository(owner: {gql_str(owner)}, name: {gql_str(name)}) {{ {REPO_FIELDS} }} ")
    parts.append("}")
    return "".join(parts)


def _flatten_repo(node: dict) -> dict:
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


def enrich_repos(gh: GitHub, repos: list[str], batch_size: int = 50) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for batch, data in run_batched(gh, repos, _repo_query, batch_size):
        d = data.get("data") or {}
        for i, repo in enumerate(batch):
            node = d.get(f"r{i}")
            out[repo] = _flatten_repo(node) if node else {"repo": repo, "missing": True}
    log.info("enriched %d repos (%d missing)", len(out), sum(1 for v in out.values() if v.get("missing")))
    return out


OWNER_FIELDS = """
  login __typename url
  ... on Organization {
    name description email websiteUrl location twitterUsername isVerified createdAt avatarUrl
    publicMembers: membersWithRole { totalCount }
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
        "public_members": (node.get("publicMembers") or {}).get("totalCount"),
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
