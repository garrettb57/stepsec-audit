import pytest

from crawler.gh import GitHub, GitHubError


def _gh(monkeypatch, core_limit, graphql_limit=5000):
    gh = GitHub("token")
    monkeypatch.setattr(
        gh,
        "rest_json",
        lambda path, **kw: {"resources": {"core": {"limit": core_limit, "remaining": core_limit}, "graphql": {"limit": graphql_limit}, "search": {"limit": 30}, "code_search": {"limit": 10}}},
    )
    return gh


def test_check_budget_rejects_github_token_budget(monkeypatch):
    gh = _gh(monkeypatch, 1000)
    with pytest.raises(GitHubError) as e:
        gh.check_budget()
    assert "CRAWLER_TOKEN" in str(e.value) and "1000/hour" in str(e.value)


def test_check_budget_accepts_pat(monkeypatch):
    gh = _gh(monkeypatch, 5000)
    limits = gh.check_budget()
    assert limits["core"] == 5000 and gh.limits["code_search"] == 10


def test_requests_counted_per_bucket():
    gh = GitHub("token")

    class R:
        headers = {"X-RateLimit-Resource": "graphql", "X-RateLimit-Remaining": "4999", "X-RateLimit-Reset": "1", "X-RateLimit-Limit": "5000"}

    gh._track(R(), "graphql")
    gh._track(R(), "graphql")

    class R2:
        headers = {"X-RateLimit-Remaining": "9", "X-RateLimit-Reset": "1", "X-RateLimit-Limit": "10"}

    gh._track(R2(), "code_search")
    assert gh.requests_by_bucket == {"graphql": 2, "code_search": 1}
    assert gh.limits == {"graphql": 5000, "code_search": 10}
