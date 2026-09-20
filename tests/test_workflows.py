import os

from crawler.workflows import parse_workflow, summarize_repo

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def test_scorecard_pinned_audit():
    r = parse_workflow(load("scorecard_main.yml"), ".github/workflows/main.yml")
    assert r["yaml_ok"]
    hr = [u for u in r["usages"] if u["action"] == "harden-runner"]
    assert len(hr) >= 4
    assert all(u["pinned_sha"] for u in hr)
    assert hr[0]["version"] == "v2.21.0"
    assert all(u["egress_policy"] == "audit" for u in hr)
    assert not r["secure_repo_marker"]


def test_secure_repo_fixture_marker_and_endpoints():
    r = parse_workflow(load("secure_repo_ci.yml"), ".github/workflows/test.yml")
    hr = r["usages"][0]
    assert hr["action"] == "harden-runner"
    assert hr["pinned_sha"]
    assert hr["version"] is None
    assert hr["egress_policy"] == "audit"
    assert hr["allowed_endpoints_count"] == 10
    assert r["secure_repo_marker"]


def test_block_policy_store_and_self_hosted():
    text = """
name: ci
on: push
jobs:
  build:
    runs-on: [self-hosted, linux]
    steps:
      - uses: step-security/harden-runner@v2
        with:
          egress-policy: block
          disable-sudo: true
          disable-telemetry: "true"
          policy: my-org-policy
      - uses: step-security/changed-files@v45
"""
    r = parse_workflow(text, ".github/workflows/ci.yml")
    hr = [u for u in r["usages"] if u["action"] == "harden-runner"][0]
    assert hr["egress_policy"] == "block"
    assert hr["disable_sudo"] and hr["disable_telemetry"] and hr["policy_store"]
    assert hr["self_hosted"]
    assert not hr["pinned_sha"] and hr["version"] == "v2"
    assert {u["action"] for u in r["usages"]} == {"harden-runner", "changed-files"}


def test_regex_fallback_on_broken_yaml():
    text = "jobs:\n  x:\n    steps:\n      - uses: step-security/harden-runner@abc123 # v2.1.0\n   bad: [unclosed"
    r = parse_workflow(text)
    assert not r["yaml_ok"]
    assert r["usages"][0]["action"] == "harden-runner"
    assert r["usages"][0]["version"] == "v2.1.0"


def test_composite_action_runs_steps():
    text = """
name: my composite
runs:
  using: composite
  steps:
    - uses: step-security/harden-runner@0634a2670c59f64b4a01f0f96f84700a4088b9f0
"""
    r = parse_workflow(text, "action.yml")
    assert r["usages"][0]["runs_on"] == "composite"
    assert r["usages"][0]["egress_policy"] == "unset"


def test_summarize_repo():
    files = [
        parse_workflow(load("scorecard_main.yml"), ".github/workflows/main.yml"),
        parse_workflow(load("secure_repo_ci.yml"), ".github/workflows/test.yml"),
        parse_workflow("name: x\non: push\njobs: {}\n", ".github/workflows/empty.yml"),
    ]
    s = summarize_repo({"files": files, "workflows_total": 5, "default_branch": "main"})
    assert s["uses_harden_runner"]
    assert s["egress_audit"] >= 5 and s["egress_block"] == 0
    assert s["workflows_with_stepsecurity"] == 2 and s["workflows_total"] == 5
    assert s["pinned_sha_ratio"] == 1.0
    assert s["secure_repo_marker"]
    assert s["harden_runner_versions"] == ["v2.21.0"]
