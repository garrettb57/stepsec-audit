from crawler.contacts import account_contact_summary, aggregate_people
from crawler.vendor import Staff


def _contacts():
    return {
        "acme/app": {
            "missing": False,
            "recent_commits": 50,
            "distinct_humans_recent": 3,
            "top_committers": [
                {"login": "alice", "name": "Alice", "emails": ["alice@acme.com"], "company": None, "website": None, "location": None, "twitter": None, "commits": 20, "last_commit": "2026-09-01"},
                {"login": "vendor-eng", "name": "V", "emails": ["v@stepsecurity.io"], "company": None, "website": None, "location": None, "twitter": None, "commits": 5, "last_commit": "2026-08-01"},
            ],
            "adopters": [
                {"path": ".github/workflows/ci.yml", "login": "vendor-eng", "name": "V", "email": "v@stepsecurity.io", "company": None, "date": "2025-01-01T00:00:00Z", "file_commits": 3, "vendor_initiated": True, "is_staff": True}
            ],
            "stepsecurity_pr_mergers": [
                {"pr": 7, "login": "bob", "name": "Bob", "email": "bob@acme.com", "company": None, "merged_at": "2025-01-02T00:00:00Z"}
            ],
        }
    }


def test_staff_excluded_and_merger_becomes_org_side_contact():
    staff = Staff({"logins": ["vendor-eng"]})
    people = aggregate_people(_contacts(), {"acme/app": "acme"}, staff)
    logins = {p["login"] for p in people}
    assert "vendor-eng" not in logins
    bob = next(p for p in people if p["login"] == "bob")
    assert {"merged_stepsecurity_pr", "org_side_contact"} <= set(bob["roles"])
    alice = next(p for p in people if p["login"] == "alice")
    assert "top_committer" in alice["roles"]
    summary = account_contact_summary(people)
    assert "bob" in summary["acme"]["adopters"]


def test_vendor_email_excludes_even_without_login_list():
    people = aggregate_people(_contacts(), {"acme/app": "acme"}, Staff())
    assert "vendor-eng" not in {p["login"] for p in people}
