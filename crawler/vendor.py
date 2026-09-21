"""StepSecurity's own accounts, sandboxes, and staff.

Denylisted owners never appear as accounts. Staff never appear as contacts,
and anything they initiated (a secure-repo PR they requested, an adoption
commit they authored) is marked vendor_initiated so the org-side merger
becomes the contact instead.

Persisted to state/staff.json so a resumed run does not refetch.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict

from .gh import GitHub, GitHubError

log = logging.getLogger(__name__)

VENDOR_ORG = "step-security"
VENDOR_EMAIL_DOMAINS = ("stepsecurity.io",)

# Hard-coded; extended at runtime by build_staff().
DENYLIST = {
    "step-security", "step-security-experiments", "step-security-demo", "step-integration-tests",
    "harden-runner-canary", "actions-security-demo", "varunsh-coder", "ashishkurmi", "kurmiashish",
    "sailikhith-stepsecurity", "raj-stepsecurity", "raj-organization-1234", "anurags-org-returns",
    "vamshi-stepsecurity", "stepsecurity-app-test", "stepsecurity-customer",
}
DENY_RE = re.compile(r"stepsecurity|step-security", re.IGNORECASE)

# PR authors that are StepSecurity's internal / integration identities.
INTERNAL_AUTHOR_RE = re.compile(
    r"^(stepsecurity-int\[bot\]|step-security-bot-int|stepsecurity-advanced-[\w-]*\[bot\])$", re.IGNORECASE
)
SECURE_REPO_BOT = "step-security-bot"
APP_BOT = "stepsecurity-app[bot]"

# Requesters or authors whose StepSecurity PRs land in this many unrelated
# owners are treated as staff running the tool on other people's repos.
CROSS_OWNER_STAFF_THRESHOLD = 5


def is_denylisted_owner(login: str | None, extra: set[str] | None = None) -> bool:
    if not login:
        return False
    l = login.lower()
    return l in DENYLIST or bool(DENY_RE.search(l)) or (extra is not None and l in extra)


def is_internal_author(login: str | None) -> bool:
    return bool(login) and bool(INTERNAL_AUTHOR_RE.match(login))


def is_vendor_email(email: str | None) -> bool:
    if not email or "@" not in email:
        return False
    dom = email.rsplit("@", 1)[1].lower()
    return any(dom == d or dom.endswith("." + d) for d in VENDOR_EMAIL_DOMAINS)


class Staff:
    """Runtime view over state/staff.json."""

    def __init__(self, data: dict | None = None):
        data = data or {}
        self.logins: set[str] = {l.lower() for l in data.get("logins", [])}
        self.sources: dict[str, str] = dict(data.get("sources", {}))
        self.sandbox_owners: set[str] = {l.lower() for l in data.get("sandbox_owners", [])}
        self.built_at: str | None = data.get("built_at")

    def add(self, login: str | None, source: str) -> None:
        if not login:
            return
        l = login.lower()
        if l not in self.logins:
            self.logins.add(l)
            self.sources.setdefault(l, source)

    def is_staff(self, login: str | None, email: str | None = None) -> bool:
        if login and login.lower() in self.logins:
            return True
        return is_vendor_email(email)

    def is_denylisted(self, login: str | None) -> bool:
        return is_denylisted_owner(login, self.sandbox_owners)

    def to_json(self) -> dict:
        return {
            "logins": sorted(self.logins),
            "sources": dict(sorted(self.sources.items())),
            "sandbox_owners": sorted(self.sandbox_owners),
            "built_at": self.built_at,
        }


def _cross_owner_actors(prs: dict[str, list[dict]]) -> dict[str, set[str]]:
    """login -> set of owners where they requested or authored a StepSecurity PR."""
    owners: dict[str, set[str]] = defaultdict(set)
    for repo, plist in prs.items():
        owner = repo.split("/")[0].lower()
        for p in plist:
            for key in ("requested_by", "author"):
                login = p.get(key)
                if not login or login.lower() in (SECURE_REPO_BOT, APP_BOT) or "[bot]" in login:
                    continue
                if login.lower() == owner:
                    continue
                owners[login.lower()].add(owner)
    return owners


def build_staff(gh: GitHub | None, prs: dict[str, list[dict]], existing: dict | None = None, sandbox_owners: set[str] | None = None) -> Staff:
    """Assemble the staff list. Recomputed from `prs` on every run (cheap);
    only the public-member lookup is cached via `existing`. `gh` may be None
    for offline tests."""
    staff = Staff(existing)
    staff.sandbox_owners |= {o.lower() for o in (sandbox_owners or set())}
    # owners of the hard-coded personal sandboxes are staff too
    for l in ("varunsh-coder", "ashishkurmi", "kurmiashish", "sailikhith-stepsecurity", "raj-stepsecurity", "vamshi-stepsecurity"):
        staff.add(l, "sandbox_owner")

    # any owner that StepSecurity's internal bots targeted is a sandbox
    for repo, plist in prs.items():
        owner = repo.split("/")[0].lower()
        if any(is_internal_author(p.get("author")) for p in plist):
            staff.sandbox_owners.add(owner)

    for login, owners in _cross_owner_actors(prs).items():
        unrelated = {o for o in owners if not is_denylisted_owner(o, staff.sandbox_owners)}
        if len(unrelated) >= CROSS_OWNER_STAFF_THRESHOLD:
            staff.add(login, f"cross_owner:{len(unrelated)}")
    for login in list(staff.logins):
        if DENY_RE.search(login):
            staff.sources.setdefault(login, "login_pattern")

    if gh is not None and not staff.built_at:
        try:
            members = list(gh.paginate(f"/orgs/{VENDOR_ORG}/public_members", max_pages=5))
            for m in members:
                staff.add(m.get("login"), "public_member")
            log.info("staff: %d public members of %s", len(members), VENDOR_ORG)
            staff.built_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        except GitHubError as e:
            log.warning("could not list %s public members: %s", VENDOR_ORG, str(e)[:120])
    log.info("staff: %d logins, %d sandbox owners", len(staff.logins), len(staff.sandbox_owners))
    return staff
