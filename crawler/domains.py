"""Email/domain classification and account domain inference."""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.jp", "yahoo.fr", "yahoo.de", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "hotmail.fr", "hotmail.de", "outlook.com", "outlook.de", "live.com", "live.co.uk",
    "msn.com", "icloud.com", "me.com", "mac.com", "protonmail.com", "protonmail.ch", "proton.me", "pm.me",
    "gmx.com", "gmx.de", "gmx.net", "gmx.at", "web.de", "mail.com", "aol.com", "qq.com", "163.com", "126.com",
    "foxmail.com", "yandex.com", "yandex.ru", "ya.ru", "fastmail.com", "fastmail.fm", "hey.com", "zoho.com",
    "tutanota.com", "tuta.io", "mail.ru", "naver.com", "hushmail.com", "posteo.de", "posteo.net", "mailbox.org",
    "duck.com", "example.com", "localhost", "localhost.localdomain", "users.noreply.github.com", "noreply.github.com",
    # ISP mailboxes
    "sbcglobal.net", "comcast.net", "att.net", "verizon.net", "bellsouth.net", "cox.net", "charter.net", "optonline.net",
    "earthlink.net", "btinternet.com", "sky.com", "virginmedia.com", "orange.fr", "free.fr", "wanadoo.fr", "laposte.net",
    "t-online.de", "freenet.de", "arcor.de", "libero.it", "alice.it", "telus.net", "shaw.ca", "rogers.com", "sympatico.ca",
    "bigpond.com", "optusnet.com.au", "xtra.co.nz", "rediffmail.com", "seznam.cz", "wp.pl", "o2.pl", "onet.pl", "ukr.net",
    "rambler.ru", "bk.ru", "list.ru", "inbox.ru", "daum.net", "hanmail.net", "sina.com", "sohu.com", "yeah.net", "139.com",
}
FREEMAIL_SUFFIXES = (".noreply.github.com", ".local", ".localdomain", ".lan", ".internal", ".test", ".invalid")

# hosting domains that say nothing about the company behind them
HOSTING_SUFFIXES = (
    "github.io", "github.com", "gitlab.io", "gitlab.com", "bitbucket.org", "readthedocs.io", "readthedocs.org",
    "netlify.app", "vercel.app", "pages.dev", "herokuapp.com", "azurewebsites.net", "web.app", "firebaseapp.com",
    "surge.sh", "fly.dev", "onrender.com", "npmjs.com", "pypi.org", "crates.io", "docs.rs", "pkg.go.dev",
    "twitter.com", "x.com", "linkedin.com", "youtube.com", "medium.com", "dev.to", "substack.com",
    "discord.gg", "discord.com", "slack.com", "t.me", "mastodon.social", "bsky.app", "patreon.com",
    "opencollective.com", "ko-fi.com", "buymeacoffee.com", "gitbook.io", "notion.site", "wordpress.com", "blogspot.com",
)

BOT_LOGIN_RE = re.compile(
    r"(\[bot\]$|^dependabot|^renovate|^github-actions|^step-security-bot|^snyk-bot|^allcontributors|^pre-commit-ci"
    r"|^imgbot|^mergify|^greenkeeper|^copilot|^codecov|^semantic-release|^release-please|^goreleaserbot"
    r"|^sonarcloud|^web-flow$|^kodiakhq|^azure-pipelines|^netlify|^vercel|^deepsource|^whitesource|^mend-|^bors)",
    re.IGNORECASE,
)
BOT_NAME_RE = re.compile(r"\b(bot|automation|ci|pipeline|actions)\b", re.IGNORECASE)


def is_bot(login: str | None, name: str | None, email: str | None) -> bool:
    if login and BOT_LOGIN_RE.search(login):
        return True
    if not login and name and BOT_NAME_RE.search(name) and ("[bot]" in name or "bot" in name.lower()):
        return True
    if email and ("noreply" in email.lower() and "users.noreply.github.com" not in email.lower()):
        return True
    return False


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    dom = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    return dom or None


def is_noreply(email: str | None) -> bool:
    return bool(email) and "noreply" in email.lower()


def is_corporate_domain(dom: str | None) -> bool:
    if not dom or "." not in dom:
        return False
    if dom in FREEMAIL or dom.endswith(FREEMAIL_SUFFIXES):
        return False
    if dom.endswith(HOSTING_SUFFIXES) or any(dom == h for h in HOSTING_SUFFIXES):
        return False
    return True


def classify_email(email: str | None) -> str:
    """'corporate' | 'freemail' | 'noreply' | 'invalid'."""
    if not email or "@" not in email:
        return "invalid"
    if is_noreply(email):
        return "noreply"
    return "corporate" if is_corporate_domain(email_domain(email)) else "freemail"


_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "or", "ne", "go", "in", "id", "ltd", "plc", "me"}


def registrable(host: str) -> str:
    """Crude eTLD+1: foo.bar.example.com -> example.com; a.b.example.co.uk -> example.co.uk."""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    u = url.strip()
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u
    try:
        host = urlparse(u).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or host.endswith(HOSTING_SUFFIXES) or host in HOSTING_SUFFIXES:
        return None
    return registrable(host)


_NORM_RE = re.compile(r"[^a-z0-9]")
_GENERIC_SUFFIXES = ("hq", "inc", "labs", "lab", "dev", "io", "oss", "org", "team", "tech", "official", "community", "sandbox")


def _name_tokens(*names: str | None) -> set[str]:
    out = set()
    for n in names:
        if not n:
            continue
        t = _NORM_RE.sub("", n.lower())
        if len(t) >= 3:
            out.add(t)
            for suf in _GENERIC_SUFFIXES:
                if t.endswith(suf) and len(t) - len(suf) >= 3:
                    out.add(t[: -len(suf)])
    return out


def _domain_label(dom: str) -> str:
    parts = dom.split(".")
    # foo.co.uk -> foo ; foo.com -> foo
    if len(parts) >= 3 and len(parts[-2]) <= 3:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[0]


def domain_matches_name(dom: str, *names: str | None) -> bool:
    label = _NORM_RE.sub("", _domain_label(dom))
    if len(label) < 3:
        return False
    for t in _name_tokens(*names):
        if label == t or (len(t) >= 5 and (t in label or label in t)):
            return True
    return False


def infer_account_domain(
    owner: dict,
    repo_homepages: list[str | None],
    committer_emails: list[str],
    login: str | None = None,
) -> tuple[str | None, str | None]:
    """Return (domain, source). Precedence: profile website, profile email,
    committer domain matching the account name, majority corporate committer
    domain (≥2 emails), repo homepage, then a single committer domain as weak."""
    d = domain_from_url(owner.get("websiteUrl"))
    if d:
        return d, "owner_website"
    d = email_domain(owner.get("email"))
    if is_corporate_domain(d):
        return d, "owner_email"
    counts = Counter(dom for dom in (email_domain(e) for e in committer_emails) if is_corporate_domain(dom))
    names = (login, owner.get("login"), owner.get("name"))
    for dom, _ in counts.most_common():
        if domain_matches_name(dom, *names):
            return dom, "committer_emails_name_match"
    for hp in repo_homepages:
        d = domain_from_url(hp)
        if d and domain_matches_name(d, *names):
            return d, "repo_homepage_name_match"
    if counts:
        dom, n = counts.most_common(1)[0]
        if n >= 2:
            return dom, "committer_emails"
    for hp in repo_homepages:
        d = domain_from_url(hp)
        if d:
            return d, "repo_homepage"
    if counts:
        return counts.most_common(1)[0][0], "committer_emails_weak"
    return None, None
