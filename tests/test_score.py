import datetime as dt

from crawler.score import depth_score, path_class, provenance, recency_factor, rollout, score_account, sort_key, suppress_reasons

TODAY = dt.date(2026, 9, 21)


def test_path_class():
    assert path_class([".github/workflows/codeql.yml", ".github/workflows/scorecards.yml"]) == ["hygiene"]
    assert path_class([".github/workflows/release.yml", ".github/workflows/ci.yml"]) == ["build", "release"]
    assert path_class([".github/workflows/publish-npm.yaml"]) == ["release"]
    assert path_class([".github/workflows/dependency-review.yml", ".github/workflows/weird.yml"]) == ["hygiene", "other"]
    assert path_class([]) == []


def test_rollout_window():
    assert rollout([f"2026-01-{d:02d}T00:00:00Z" for d in (1, 5, 9, 20, 28)])
    assert not rollout(["2026-01-01T00:00:00Z", "2026-02-15T00:00:00Z", "2026-03-20T00:00:00Z", "2026-05-01T00:00:00Z", "2026-07-01T00:00:00Z"])
    assert not rollout(["2026-01-01T00:00:00Z"] * 4)


def test_recency_factor():
    assert recency_factor("2026-09-01T00:00:00Z", TODAY) == 1.0
    assert recency_factor("2026-05-01T00:00:00Z", TODAY) == 0.7
    assert recency_factor("2025-12-01T00:00:00Z", TODAY) == 0.4
    assert recency_factor("2024-01-01T00:00:00Z", TODAY) == 0.15
    assert recency_factor(None, TODAY) == 0.4


def _acct(**kw):
    base = {
        "account": "acme", "account_type": "Organization", "repos_with_harden_runner": 0, "egress_block_repos": 0,
        "allowed_endpoints_max": 0, "policy_store_repos": 0, "disable_sudo_repos": 0, "egress_expr_repos": 0,
        "path_release_repos": 0, "workflow_coverage_pct": 0, "app_installed": False, "secure_repo_self_prs": 0,
        "secure_repo_third_party_prs": 0, "secure_repo_vendor_prs": 0, "secure_repo_unknown_prs": 0, "human_prs": 0,
        "reverts": 0, "stepsecurity_prs": 0, "latest_push": "2026-09-10T00:00:00Z", "adoption_dates": [], "total_stars": 0,
    }
    base.update(kw)
    return base


def test_depth_score_components():
    assert depth_score(_acct()) == 0
    assert depth_score(_acct(repos_with_harden_runner=1)) == 12                          # presence 10 + breadth 2
    assert depth_score(_acct(repos_with_harden_runner=1, egress_block_repos=1)) == 22      # block share discounted to 0.5
    assert depth_score(_acct(repos_with_harden_runner=10, egress_block_repos=10)) == 50    # 10 + 20 + 20
    assert depth_score(_acct(repos_with_harden_runner=28, egress_block_repos=4)) == 43     # 10 + 30 + 20*4/28
    full = _acct(repos_with_harden_runner=15, egress_block_repos=15, allowed_endpoints_max=5, policy_store_repos=1,
                 disable_sudo_repos=1, egress_expr_repos=1, path_release_repos=1, workflow_coverage_pct=100)
    assert depth_score(full) == 100


def test_provenance_precedence():
    assert provenance(_acct(app_installed=True, secure_repo_self_prs=3)) == "app_installed"
    assert provenance(_acct(secure_repo_self_prs=1, human_prs=1)) == "secure_repo_self"
    assert provenance(_acct(human_prs=1, secure_repo_third_party_prs=1)) == "human_pr"
    assert provenance(_acct(repos_with_harden_runner=2)) == "hand_written"
    assert provenance(_acct(repos_with_harden_runner=2, secure_repo_third_party_prs=1, stepsecurity_prs=1)) == "secure_repo_third_party"
    assert provenance(_acct(secure_repo_vendor_prs=1, stepsecurity_prs=1)) == "secure_repo_vendor"
    assert provenance(_acct(secure_repo_unknown_prs=1, stepsecurity_prs=1)) == "secure_repo_unknown"
    assert provenance(_acct()) == "unknown"


def test_suppress_reasons():
    assert suppress_reasons(_acct(app_installed=True, policy_store_repos=1)) == ["app_installed", "policy_store"]
    assert suppress_reasons(_acct(account_type="User")) == ["individual"]
    assert suppress_reasons(_acct(reverts=1)) == ["all_reverted"]
    assert suppress_reasons(_acct(reverts=1, repos_with_harden_runner=1)) == []


def test_score_is_deterministic_and_orders_fixture_set():
    rows = [
        _acct(account="blocker", repos_with_harden_runner=4, egress_block_repos=4, path_release_repos=1),   # depth 10+8+20+8=46
        _acct(account="auditor", repos_with_harden_runner=4),                                                # depth 18
        _acct(account="customer", repos_with_harden_runner=4, egress_block_repos=4, policy_store_repos=1),   # suppressed
        _acct(account="stale", repos_with_harden_runner=4, egress_block_repos=4, latest_push="2024-01-01T00:00:00Z"),
        _acct(account="person", account_type="User", repos_with_harden_runner=9, egress_block_repos=9),
    ]
    scored = sorted((score_account(dict(r), TODAY) for r in rows), key=sort_key)
    by = {a["account"]: a for a in scored}
    assert [a["account"] for a in scored] == ["blocker", "auditor", "stale", "customer", "person"]
    assert by["blocker"]["score"] == 46.0 and by["blocker"]["provenance"] == "hand_written"
    assert by["auditor"]["score"] == 18.0
    assert by["stale"]["score"] == round(38 * 0.15, 1)
    assert by["customer"]["score"] == 0 and by["customer"]["suppress_reason"] == ["policy_store"]
    assert by["person"]["score"] == 0 and by["person"]["fit"] == 0
    # same input, same output
    again = sorted((score_account(dict(r), TODAY) for r in rows), key=sort_key)
    assert [(a["account"], a["score"]) for a in again] == [(a["account"], a["score"]) for a in scored]
