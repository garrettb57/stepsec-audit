"""CLI entry point. Phases are checkpointed to state/ so a run can be
interrupted (Actions job limits) and resumed.

    python -m crawler.main --phase all --time-budget-min 330
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from . import contacts as contacts_mod
from . import discover, enrich, export, workflows
from .gh import GitHub

log = logging.getLogger("crawler")

PHASES = ["discover", "workflows", "enrich", "contacts", "export"]


class State:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(path, exist_ok=True)

    def _p(self, name: str) -> str:
        return os.path.join(self.path, f"{name}.json")

    def load(self, name: str, default):
        p = self._p(name)
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
        return default

    def save(self, name: str, data) -> None:
        tmp = self._p(name) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, separators=(",", ":"), default=_json_default)
        os.replace(tmp, self._p(name))


def _json_default(o):
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(type(o))


class Budget:
    def __init__(self, minutes: float):
        self.deadline = time.time() + minutes * 60

    def left(self) -> float:
        return self.deadline - time.time()

    def ok(self, margin_s: int = 90) -> bool:
        return self.left() > margin_s


def _priority(repo: str, repos: dict, wf: dict) -> tuple:
    r = repos.get(repo) or {}
    w = wf.get(repo) or {}
    return (
        0 if r.get("owner_type") == "Organization" else 1,
        0 if w.get("uses_harden_runner") else 1,
        -(r.get("stars") or 0),
    )


def run(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    gh = GitHub(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "")
    st = State(args.state_dir)
    budget = Budget(args.time_budget_min)
    phases = PHASES if args.phase == "all" else [args.phase]
    meta = st.load("meta", {})

    discovered = st.load("discovered", {})
    prs = st.load("prs", {})
    wf_raw = st.load("workflows_raw", {})
    repos = st.load("repos", {})
    owners = st.load("owners", {})
    contacts = st.load("contacts", {})

    # ------------------------------------------------------------ discover
    if "discover" in phases and budget.ok():
        actions = [a.strip() for a in (args.actions or "").split(",") if a.strip()] or None
        if not meta.get("discover_code_done") or args.rediscover or args.smoke:
            discovered = discover.discover_code(gh, args.state_dir, actions, smoke=args.smoke)
            st.save("discovered", discovered)
            meta["discover_code_done"] = not args.smoke
            meta["discovered_repos"] = len(discovered)
            st.save("meta", meta)
        if (not meta.get("discover_prs_done") or args.rediscover or args.smoke) and not args.skip_prs:
            prs = discover.discover_prs(gh, args.state_dir, smoke=args.smoke)
            st.save("prs", prs)
            meta["discover_prs_done"] = not args.smoke
            meta["pr_repos"] = len(prs)
            st.save("meta", meta)
        log.info("requests so far: %d; buckets: %s", gh.requests_made, gh.bucket_status())

    all_repos = sorted(set(discovered) | set(prs))
    if args.limit:
        all_repos = all_repos[: args.limit]
    log.info("%d candidate repos", len(all_repos))

    # ----------------------------------------------------------- workflows
    if "workflows" in phases and budget.ok():
        todo = {r: discovered.get(r, []) for r in all_repos if r not in wf_raw}
        log.info("workflows: %d repos to fetch", len(todo))
        keys = list(todo)
        for i in range(0, len(keys), 500):
            if not budget.ok():
                log.warning("time budget exhausted during workflows phase")
                break
            chunk = {k: todo[k] for k in keys[i : i + 500]}
            wf_raw.update(workflows.fetch_and_parse(gh, chunk))
            st.save("workflows_raw", wf_raw)
        meta["workflows_done"] = sum(1 for r in all_repos if r in wf_raw)
        st.save("meta", meta)

    wf = {r: workflows.summarize_repo(s) for r, s in wf_raw.items() if not s.get("missing")}

    # -------------------------------------------------------------- enrich
    if "enrich" in phases and budget.ok():
        todo = [r for r in all_repos if r not in repos or repos[r].get("missing")]
        log.info("enrich: %d repos", len(todo))
        for i in range(0, len(todo), 1000):
            if not budget.ok():
                break
            repos.update(enrich.enrich_repos(gh, todo[i : i + 1000]))
            st.save("repos", repos)
        logins = sorted({(repos.get(r) or {}).get("owner") or r.split("/")[0] for r in all_repos})
        todo_o = [l for l in logins if l not in owners or owners[l].get("missing")]
        log.info("enrich: %d owners", len(todo_o))
        for i in range(0, len(todo_o), 1000):
            if not budget.ok():
                break
            owners.update(enrich.enrich_owners(gh, todo_o[i : i + 1000]))
            st.save("owners", owners)
        meta["enrich_done"] = sum(1 for r in all_repos if r in repos)
        st.save("meta", meta)

    # ------------------------------------------------------------ contacts
    if "contacts" in phases and budget.ok() and not args.skip_contacts:
        todo = [r for r in all_repos if r not in contacts and not (repos.get(r) or {}).get("missing")]
        todo.sort(key=lambda r: _priority(r, repos, wf))
        log.info("contacts: %d repos", len(todo))
        for i in range(0, len(todo), 200):
            if not budget.ok(margin_s=240):
                log.warning("time budget exhausted during contacts phase (%d/%d done)", i, len(todo))
                break
            items = []
            for r in todo[i : i + 200]:
                items.append(
                    {
                        "repo": r,
                        "hr_paths": (wf.get(r) or {}).get("harden_runner_paths") or (wf.get(r) or {}).get("stepsecurity_paths") or [],
                        "pr_numbers": [p["number"] for p in (prs.get(r) or []) if p.get("merged_at")][:3],
                    }
                )
            contacts.update(contacts_mod.collect_contacts(gh, items, walk_history=not args.no_history_walk))
            st.save("contacts", contacts)
        meta["contacts_done"] = sum(1 for r in all_repos if r in contacts)
        st.save("meta", meta)

    # -------------------------------------------------------------- export
    if "export" in phases:
        repo_owner = {r: (repos.get(r) or {}).get("owner") or r.split("/")[0] for r in all_repos}
        people = contacts_mod.aggregate_people(contacts, repo_owner)
        by_acct = contacts_mod.account_contact_summary(people)
        scoped = set(all_repos)
        repo_rows = export.build_repo_rows(
            {r: repos.get(r, {"repo": r}) for r in all_repos},
            {r: v for r, v in wf.items() if r in scoped},
            {r: v for r, v in prs.items() if r in scoped},
            contacts,
            discovered,
        )
        acct_rows = export.build_account_rows(repo_rows, owners, by_acct)
        contact_rows = export.build_contact_rows(people)
        file_rows = export.build_file_rows(wf_raw)
        os.makedirs(args.out_dir, exist_ok=True)
        export.write_csv(os.path.join(args.out_dir, "accounts.csv"), acct_rows, export.ACCOUNT_COLUMNS)
        export.write_csv(os.path.join(args.out_dir, "repos.csv"), repo_rows, export.REPO_COLUMNS)
        export.write_csv(os.path.join(args.out_dir, "contacts.csv"), contact_rows, export.CONTACT_COLUMNS)
        export.write_csv(os.path.join(args.out_dir, "workflow_files.csv"), file_rows, export.FILE_COLUMNS)
        meta["exported_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        meta["requests_this_run"] = gh.requests_made
        st.save("meta", meta)
        export.write_summary(os.path.join(args.out_dir, "SUMMARY.md"), acct_rows, repo_rows, people, meta)

    remaining = [r for r in all_repos if r not in contacts] if not args.skip_contacts else []
    if remaining and "contacts" in phases:
        log.warning("%d repos still need the contacts phase; re-run to continue", len(remaining))
    log.info("done. requests: %d, buckets: %s", gh.requests_made, gh.bucket_status())
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", choices=["all"] + PHASES, default="all")
    p.add_argument("--state-dir", default="state")
    p.add_argument("--out-dir", default="data")
    p.add_argument("--time-budget-min", type=float, default=330)
    p.add_argument("--limit", type=int, default=0, help="cap candidate repos (smoke tests)")
    p.add_argument("--actions", default="", help="comma list of step-security action names to search; default all")
    p.add_argument("--rediscover", action="store_true", help="re-run discovery even if marked done")
    p.add_argument("--skip-prs", action="store_true")
    p.add_argument("--skip-contacts", action="store_true")
    p.add_argument("--no-history-walk", action="store_true", help="don't page file history to find the first commit")
    p.add_argument("--smoke", action="store_true", help="one page of discovery only; validates the pipeline end to end in minutes")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
