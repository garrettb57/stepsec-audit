import logging

from crawler.prs import (
    classify_pr,
    merge_latency_h,
    migrate_prs,
    parse_requester,
    parse_revert_target,
    slim_pr,
    summarize_repo_prs,
)
from crawler.vendor import Staff
from tests.fixtures.pr_bodies import APP_BODY, INT_BODY, REVERT_BODY, SECURE_REPO_BODY, SECURE_REPO_BODY_2022

TITLE = "[StepSecurity] Apply security best practices"


def test_requester_regex_match_and_no_match():
    assert parse_requester(SECURE_REPO_BODY) == "pniedzielski"
    assert parse_requester("... at the request of @Some-User. Thanks") == "Some-User"
    assert parse_requester(SECURE_REPO_BODY_2022) is None
    assert parse_requester(APP_BODY) is None
    assert parse_requester(None) is None


def test_revert_target():
    assert parse_revert_target(REVERT_BODY) == 689
    assert parse_revert_target("Reverts asmit27rai/KubeArmor#4") == 4
    assert parse_revert_target(SECURE_REPO_BODY) is None


def test_merge_latency():
    assert merge_latency_h("2026-01-20T09:40:21Z", "2026-01-22T13:29:52Z") == 51.83
    assert merge_latency_h("2026-01-20T09:40:21Z", None) is None


def _item(author, body, title=TITLE, number=1, merged="2026-01-01T01:00:00Z", author_type="User"):
    return {
        "repository_url": "https://api.github.com/repos/acme/app",
        "number": number,
        "title": title,
        "state": "closed",
        "user": {"login": author, "type": author_type},
        "created_at": "2026-01-01T00:00:00Z",
        "closed_at": merged,
        "pull_request": {"merged_at": merged},
        "html_url": f"https://github.com/acme/app/pull/{number}",
        "body": body,
    }


def test_slim_pr_keeps_body_and_parses(caplog):
    p = slim_pr(_item("step-security-bot", SECURE_REPO_BODY))
    assert p["repo"] == "acme/app" and p["requested_by"] == "pniedzielski" and p["merge_latency_h"] == 1.0
    assert p["body"].startswith("## Summary")
    with caplog.at_level(logging.INFO):
        p = slim_pr(_item("step-security-bot", SECURE_REPO_BODY_2022))
    assert p["requested_by"] is None
    assert "requester parse failed" in caplog.text
    long = slim_pr(_item("step-security-bot", "x" * 5000))
    assert len(long["body"]) == 2000


def test_classify_each_class():
    staff = Staff({"logins": ["vendor-eng"]})
    owner = "acme"
    members = {"alice"}
    committers = {"carol"}

    def cls(author, body, requester=None, title=TITLE):
        pr = slim_pr(_item(author, body, title=title))
        if requester is not None:
            pr["requested_by"] = requester
        return classify_pr(pr, owner, staff, members, committers)

    assert cls("stepsecurity-int[bot]", INT_BODY) == "internal"
    assert cls("step-security-bot-int", SECURE_REPO_BODY) == "internal"
    assert cls("stepsecurity-app[bot]", APP_BODY) == "app_installed"
    assert cls("step-security-bot", SECURE_REPO_BODY, requester="acme") == "secure_repo_self"
    assert cls("step-security-bot", SECURE_REPO_BODY, requester="Alice") == "secure_repo_self"
    assert cls("step-security-bot", SECURE_REPO_BODY, requester="carol") == "secure_repo_self"
    assert cls("step-security-bot", SECURE_REPO_BODY, requester="vendor-eng") == "secure_repo_vendor"
    assert cls("step-security-bot", SECURE_REPO_BODY) == "secure_repo_third_party"  # pniedzielski is none of the above
    assert cls("step-security-bot", SECURE_REPO_BODY_2022) == "secure_repo_unknown"
    assert cls("cpanato", REVERT_BODY, title='Revert "[StepSecurity] Apply security best practices"') == "revert"
    assert cls("dave", "manual hardening", title="ci: add harden-runner") == "human_pr"


def test_summarize_repo_prs():
    plist = [
        slim_pr(_item("stepsecurity-app[bot]", APP_BODY, number=1, merged="2026-01-01T12:00:00Z")),
        slim_pr(_item("step-security-bot", SECURE_REPO_BODY, number=2, merged="2026-01-01T00:06:00Z")),
        slim_pr(_item("cpanato", REVERT_BODY, title='Revert "' + TITLE + '"', number=3)),
        slim_pr(_item("dave", "hand rolled", title="ci: harden", number=4, merged=None)),
    ]
    s = summarize_repo_prs(plist, "acme", Staff(), set(), set())
    assert s["pr_classes"] == ["app_installed", "human_pr", "revert", "secure_repo_third_party"]
    assert s["pr_requesters"] == ["pniedzielski"]
    assert s["pr_human_authors"] == ["dave"]
    assert s["reverted"] and s["reverted_prs"] == [689]
    assert s["app_installed"]
    assert s["stepsecurity_prs_merged"] == 2
    assert s["pr_median_latency_h"] == 6.05  # median of 12.0 and 0.1
    assert not s["internal_only"]


def test_migrate_prs_adds_nulls_and_drops_internal():
    prs = {
        "acme/app": [{"author": "step-security-bot", "number": 1, "title": TITLE, "created_at": "2026-01-01T00:00:00Z", "merged_at": "2026-01-01T02:00:00Z"}],
        "sandbox/x": [{"author": "stepsecurity-int[bot]", "number": 1, "title": TITLE, "created_at": "2026-01-01T00:00:00Z", "merged_at": None}],
    }
    out, dropped, sandbox_owners = migrate_prs(prs)
    assert dropped == 1 and "sandbox/x" not in out and sandbox_owners == {"sandbox"}
    p = out["acme/app"][0]
    assert p["body"] is None and p["requested_by"] is None and p["author_type"] is None and p["merge_latency_h"] == 2.0
    out2, dropped2, _ = migrate_prs(out)
    assert out2 == out and dropped2 == 0
