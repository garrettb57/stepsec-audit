"""Fetch workflow files and extract StepSecurity usage signals.

For every discovered (repo, path) we pull the blob via GraphQL (batched)
and parse it. Output per repo is a compact summary used for segmentation:

- which step-security actions are used, with refs and SHA-pinning
- harden-runner configuration (egress policy, sudo, telemetry, endpoints)
- enterprise-tier indicators (policy store, API key, self-hosted VM, ARC)
- how many workflow files exist vs. how many use StepSecurity
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

import yaml

from .gh import GitHub, gql_str, run_batched

log = logging.getLogger(__name__)

USES_RE = re.compile(
    r"""uses:\s*['"]?(step-security/([A-Za-z0-9_.-]+))(?:/[^@'"\s]+)?@([^'"\s#]+)['"]?[ \t]*(?:#\s*(v?[\w.+-]+))?""",
    re.IGNORECASE,
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SECURE_REPO_MARKER = "step-security/secure-repo"

HR_ENTERPRISE_INPUTS = ("policy", "use-policy-store", "api-key", "deploy-on-self-hosted-vm")


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).replace(",", "\n").split() if x.strip()]


def _walk_steps(doc) -> Iterable[tuple[list, str]]:
    """Yield (steps, runs_on) for workflow jobs and composite action runs."""
    if not isinstance(doc, dict):
        return
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            if isinstance(job, dict) and isinstance(job.get("steps"), list):
                yield job["steps"], str(job.get("runs-on", ""))
    runs = doc.get("runs")
    if isinstance(runs, dict) and isinstance(runs.get("steps"), list):
        yield runs["steps"], "composite"


def parse_workflow(text: str, path: str = "") -> dict:
    """Parse a workflow/action YAML into a StepSecurity usage summary for one file."""
    usages: list[dict] = []
    version_comments: dict[str, str] = {}
    for m in USES_RE.finditer(text):
        ref, comment = m.group(3), m.group(4)
        if comment:
            version_comments[ref] = comment

    doc = None
    try:
        doc = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - fall back to regex-only parse
        doc = None

    if doc is not None:
        for steps, runs_on in _walk_steps(doc):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                uses = step.get("uses")
                if not isinstance(uses, str) or not uses.lower().startswith("step-security/"):
                    continue
                m = USES_RE.match("uses: " + uses)
                if not m:
                    continue
                name, ref = m.group(2).lower(), m.group(3)
                with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
                usages.append(_usage(name, ref, with_, runs_on, version_comments.get(ref)))
    if not usages:
        for m in USES_RE.finditer(text):
            usages.append(_usage(m.group(2).lower(), m.group(3), {}, "", m.group(4)))

    return {
        "path": path,
        "usages": usages,
        "secure_repo_marker": SECURE_REPO_MARKER in text,
        "yaml_ok": doc is not None,
    }


def _usage(name: str, ref: str, with_: dict, runs_on: str, version_comment: str | None) -> dict:
    u = {
        "action": name,
        "ref": ref,
        "pinned_sha": bool(SHA_RE.match(ref)),
        "version": version_comment or (ref if not SHA_RE.match(ref) else None),
        "runs_on": runs_on,
        "self_hosted": "self-hosted" in runs_on.lower() or "arc-" in runs_on.lower(),
    }
    if name == "harden-runner":
        w = {str(k).lower(): v for k, v in (with_ or {}).items()}
        egress = str(w.get("egress-policy", "")).strip().lower()
        u.update(
            {
                "egress_policy": egress if egress in ("audit", "block") else ("unset" if not egress else "expr"),
                "allowed_endpoints_count": len(_as_list(w.get("allowed-endpoints"))),
                "disable_sudo": _truthy(w.get("disable-sudo")) or _truthy(w.get("disable-sudo-and-containers")),
                "disable_telemetry": _truthy(w.get("disable-telemetry")),
                "disable_file_monitoring": _truthy(w.get("disable-file-monitoring")),
                "policy_store": bool(w.get("policy")) or _truthy(w.get("use-policy-store")) or bool(w.get("api-key")),
                "self_hosted_vm": _truthy(w.get("deploy-on-self-hosted-vm")),
            }
        )
    return u


# ---------------------------------------------------------------- fetching
def _build_query(batch: list[tuple[str, list[str]]]) -> str:
    parts = ["query { rateLimit { cost remaining resetAt }"]
    for i, (repo, paths) in enumerate(batch):
        owner, name = repo.split("/", 1)
        parts.append(
            f"r{i}: repository(owner: {gql_str(owner)}, name: {gql_str(name)}) {{ "
            f"nameWithOwner defaultBranchRef {{ name }} "
            f'tree: object(expression: "HEAD:.github/workflows") {{ ... on Tree {{ entries {{ name type }} }} }} '
        )
        for j, p in enumerate(paths):
            parts.append(f"f{j}: object(expression: {gql_str('HEAD:' + p)}) {{ ... on Blob {{ text byteSize isBinary isTruncated }} }} ")
        parts.append("} ")
    parts.append("}")
    return "".join(parts)


# Workflow file names most likely to carry harden-runner, for repos where we
# have no code-search hit and must pick from the tree listing.
PREFERRED_WF_RE = re.compile(r"ci|build|release|publish|test|codeql|scorecard|dependency", re.IGNORECASE)


def select_tree_paths(names: list[str], cap: int = 12) -> list[str]:
    """Choose up to `cap` workflow files from a .github/workflows listing,
    preferred names first, then the rest alphabetically."""
    yml = sorted({n for n in names if n.lower().endswith((".yml", ".yaml"))}, key=str.lower)
    preferred = [n for n in yml if PREFERRED_WF_RE.search(n)]
    rest = [n for n in yml if n not in preferred]
    return [".github/workflows/" + n for n in (preferred + rest)[:cap]]


def _parse_batch(batch, data, out, source: str) -> list[tuple[str, list[str]]]:
    """Fill `out` from one GraphQL response. Returns repos that had no paths
    but do have a workflow tree, with the paths to fetch on a second pass."""
    d = data.get("data") or {}
    second: list[tuple[str, list[str]]] = []
    for i, (repo, paths) in enumerate(batch):
        node = d.get(f"r{i}")
        if not node:
            out[repo] = {"missing": True, "files": [], "source": source}
            continue
        entries = ((node.get("tree") or {}).get("entries")) or []
        wf_names = [e["name"] for e in entries if e.get("type") == "blob" and e["name"].lower().endswith((".yml", ".yaml"))]
        files = []
        for j, p in enumerate(paths):
            blob = node.get(f"f{j}")
            if not blob or blob.get("isBinary") or not blob.get("text"):
                continue
            files.append(parse_workflow(blob["text"], p) | {"bytes": blob.get("byteSize")})
        out[repo] = {
            "missing": False,
            "default_branch": (node.get("defaultBranchRef") or {}).get("name"),
            "workflows_total": len(wf_names),
            "files": files,
            "source": source,
        }
        if not paths and wf_names:
            second.append((repo, select_tree_paths(wf_names)))
    return second


def fetch_and_parse(gh: GitHub, discovered: dict[str, list[dict]], batch_size: int = 25, max_files_per_repo: int = 12) -> dict[str, dict]:
    """discovered: repo -> [file hit dicts]; returns repo -> summary.

    Repos with no code-search hit (PR-only discovery) get a second pass that
    fetches up to `max_files_per_repo` workflow files chosen from the tree
    listing the first pass already returned (P0.5). `source` records which.
    """
    items = []
    for repo, files in discovered.items():
        paths = sorted({f["path"] for f in files if f.get("path")})[:max_files_per_repo]
        items.append((repo, paths))
    out: dict[str, dict] = {}
    second: list[tuple[str, list[str]]] = []
    for batch, data in run_batched(gh, items, _build_query, batch_size):
        second.extend(_parse_batch(batch, data, out, "code_search"))
    if second:
        log.info("workflows: tree-scan second pass for %d PR-only repos", len(second))
        for batch, data in run_batched(gh, second, _build_query, batch_size):
            _parse_batch(batch, data, out, "tree_scan")
    return out


# ------------------------------------------------------------- aggregation
def summarize_repo(summary: dict) -> dict:
    """Collapse per-file parse results into repo-level StepSecurity fields."""
    files = summary.get("files") or []
    usages = [u for f in files for u in f["usages"]]
    hr = [u for u in usages if u["action"] == "harden-runner"]
    actions = sorted({u["action"] for u in usages})
    egress = [u.get("egress_policy") for u in hr]
    hr_paths = sorted({f["path"] for f in files if any(u["action"] == "harden-runner" for u in f["usages"])})
    ss_paths = sorted({f["path"] for f in files if f["usages"]})
    versions = sorted({u["version"] for u in hr if u.get("version")})
    return {
        "uses_stepsecurity": bool(usages),
        "uses_harden_runner": bool(hr),
        "stepsecurity_actions": actions,
        "other_stepsecurity_actions": [a for a in actions if a != "harden-runner"],
        "harden_runner_steps": len(hr),
        "harden_runner_versions": versions,
        "egress_block": egress.count("block"),
        "egress_audit": egress.count("audit"),
        "egress_unset": egress.count("unset") + egress.count("expr"),
        "allowed_endpoints_max": max([u.get("allowed_endpoints_count", 0) for u in hr] or [0]),
        "disable_sudo_any": any(u.get("disable_sudo") for u in hr),
        "disable_telemetry_any": any(u.get("disable_telemetry") for u in hr),
        "policy_store_any": any(u.get("policy_store") for u in hr),
        "self_hosted_any": any(u.get("self_hosted") or u.get("self_hosted_vm") for u in hr),
        "pinned_sha_ratio": round(sum(1 for u in usages if u["pinned_sha"]) / len(usages), 2) if usages else None,
        "secure_repo_marker": any(f.get("secure_repo_marker") for f in files),
        "workflows_total": summary.get("workflows_total"),
        "workflows_with_stepsecurity": len(ss_paths),
        "harden_runner_paths": hr_paths,
        "stepsecurity_paths": ss_paths,
        "default_branch": summary.get("default_branch"),
    }
