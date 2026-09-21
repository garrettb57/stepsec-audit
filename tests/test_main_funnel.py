import json

from crawler.main import Ctx, State, _metadata_reason, _merge_prs, migrate_repos, migrate_workflows


def _write(tmp_path, name, data):
    (tmp_path / f"{name}.json").write_text(json.dumps(data))


def test_owner_type_fallback_and_candidates(tmp_path):
    _write(tmp_path, "discovered", {
        "acme/app": [{"repo": "acme/app", "owner": "acme", "owner_type": "Organization", "fork": False, "path": ".github/workflows/ci.yml"}],
        "zed/tool": [{"repo": "zed/tool", "owner": "zed", "owner_type": "User", "fork": False, "path": ".github/workflows/ci.yml"}],
    })
    _write(tmp_path, "prs", {"beta/lib": [{"author": "step-security-bot", "number": 1, "title": "x", "created_at": "2026-01-01T00:00:00Z", "merged_at": None}]})
    _write(tmp_path, "owners", {"beta": {"login": "beta", "type": "Organization"}})
    _write(tmp_path, "repos", {"zed/tool": {"repo": "zed/tool", "owner": "zed", "owner_type": "User"}})
    ctx = Ctx(State(str(tmp_path)))
    assert ctx.owner_type("acme/app") == "Organization"   # from discovered hit, owners.json absent
    assert ctx.owner_type("beta/lib") == "Organization"   # from owners.json
    assert ctx.owner_type("zed/tool") == "User"            # from discovered, agrees with repos.json
    assert ctx.owner_type("nobody/x") is None
    assert ctx.candidates() == ["acme/app", "beta/lib", "zed/tool"]
    # --limit puts organizations first
    assert ctx.candidates(limit=2) == ["acme/app", "beta/lib"]


def test_metadata_reason():
    assert _metadata_reason({}) is None
    assert _metadata_reason({"missing": True}) == "repo_missing"
    assert _metadata_reason({"is_fork": True}) == "fork"
    assert _metadata_reason({"is_fork": False, "is_archived": True}) == "archived"
    assert _metadata_reason({"is_fork": False, "is_archived": False, "is_mirror": False, "is_template": True}) == "template"
    assert _metadata_reason({"is_fork": False, "is_archived": False}) is None


def test_merge_prs_keeps_backfilled_bodies():
    existing = {"acme/app": [{"number": 1, "body": "at the request of @alice", "requested_by": "alice", "created_at": "2026-01-01T00:00:00Z"}]}
    fresh = {"acme/app": [
        {"number": 1, "body": None, "requested_by": None, "created_at": "2026-01-01T00:00:00Z"},
        {"number": 2, "body": None, "requested_by": None, "created_at": "2026-02-01T00:00:00Z"},
    ]}
    out = _merge_prs(existing, fresh)
    assert [p["number"] for p in out["acme/app"]] == [1, 2]
    assert out["acme/app"][0]["requested_by"] == "alice"


def test_migrate_workflows_resets_only_v1_pr_only_records():
    wf = {
        "a/pr-only-v1": {"missing": False, "files": [], "workflows_total": 3},          # v1, has workflow files -> refetch
        "a/no-workflows-v1": {"missing": False, "files": [], "workflows_total": 0},     # nothing to scan -> keep
        "a/code-hit-v1": {"missing": False, "files": [{"path": "x"}], "workflows_total": 3},
        "a/pr-only-v2": {"missing": False, "files": [], "workflows_total": 3, "source": "tree_scan"},
        "a/gone": {"missing": True, "files": []},
    }
    assert migrate_workflows(wf) == 1
    assert "a/pr-only-v1" not in wf and len(wf) == 4
    assert migrate_workflows(wf) == 0


def test_migrate_repos_marks_v1_full_records_deep():
    repos = {
        "a/v1": {"repo": "a/v1", "languages": ["Go"], "topics": []},
        "a/light": {"repo": "a/light", "stars": 3},
        "a/gone": {"repo": "a/gone", "missing": True},
    }
    assert migrate_repos(repos) == 1
    assert repos["a/v1"]["deep"] is True and "deep" not in repos["a/light"]
    assert migrate_repos(repos) == 0
