"""Account ranking. P0.1 skeleton: organizations with live harden-runner
first, then by stars. Replaced by the orthogonal score in P0.4."""
from __future__ import annotations


def rank_accounts(accounts: dict[str, dict]) -> list[str]:
    """accounts: login -> {type, hr_repos, stars}. Returns logins, best first."""
    return sorted(
        accounts,
        key=lambda a: (
            0 if accounts[a].get("type") == "Organization" else 1,
            -(accounts[a].get("hr_repos") or 0),
            -(accounts[a].get("stars") or 0),
            a.lower(),
        ),
    )
