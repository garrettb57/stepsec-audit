"""CLI entry point. A funnel: every phase filters before the next spends
per-repo requests. Phases checkpoint to state/ so a run can stop at the
time budget and resume.

    discover -> owners -> classify -> repos -> workflows -> score -> contacts -> export

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
from . import discover, enrich, export, vendor, workflows
from . import prs as prs_mod
from .gh import GitHub

log = logging.getLogger("crawler")

PHASES = ["discover", "owners", "classify", "repos", "workflows", "score", "contacts", "export"]
SCHEMA = 2


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


class Phase:
    """Context manager recording wall time and per-bucket requests into meta."""

    def __init__(self, name: str, gh: GitHub, meta: dict, st: State):
        self.name, self.gh, self.meta, self.st = name, gh, meta, st
        self.rows = 0

    def __enter__(self):
        self.t0 = time.time()
        self.req0 = dict(self.gh.requests_by_bucket)
        log.info("== phase %s", self.name)
        return self

    def __exit__(self, *exc):
        wall = round(time.time() - self.t0, 1)
        delta = {b: n - self.req0.get(b, 0) for b, n in self.gh.requests_by_bucket.items() if n - self.req0.get(b, 0)}
        prev = self.meta.setdefault("phases", {}).get(self.name, {})
        reqs = dict(prev.get("requests", {}))
        for b, n in delta.items():
            reqs[b] = reqs.get(b, 0) + n
        self.meta["phases"][self.name] = {
            "wall_s": round(prev.get("wall_s", 0) + wall, 1),
            "last_run_wall_s": wall,
            "requests": reqs,
            "rows": prev.get("rows", 0) + self.rows,
        }
        self.st.save("meta", self.meta)
        log.info("== phase %s done in %ss, requests %s, rows %d", self.name, wall, delta, self.rows)
        return False


def migrate(meta: dict, st: State) -> None:
    """One-time, additive schema bumps. Never invalidates fetched data."""
    if meta.get("schema", 1) < 2:
        # new app-bot discovery query; cached buckets replay from disk at no cost
        meta["discover_prs_done"] = False
        meta["schema"] = 2
        log.info("migration: schema 1 -> 2 (PR discovery will add the app query)")
        st.save("meta", meta)


def migrate_workflows(wf_raw: dict) -> int:
    """v1 recorded PR-only repos (no code-search paths) with files=[] even when
    the repo has workflow files. Drop those records so the tree-scan second pass
    (P0.5) fetches them. v2 records carry `source`; records with no workflow
    files at all stay. Idempotent."""
    drop = [
        r for r, v in wf_raw.items()
        if "source" not in v and not v.get("missing") and not v.get("files") and (v.get("workflows_total") or 0) > 0
    ]
    for r in drop:
        del wf_raw[r]
    return len(drop)


def migrate_repos(repos: dict) -> int:
    """v1 fetched light and deep fields together; mark them so the deep pass
    does not refetch. Idempotent."""
    n = 0
    for v in repos.values():
        if not v.get("missing") and "deep" not in v and "languages" in v:
            v["deep"] = True
            n += 1
    return n


class Ctx:
    def __init__(self, st: State):
        self.st = st
        self.meta = st.load("meta", {})
        self.discovered = st.load("discovered", {})
        self.prs, self.dropped_internal, self.sandbox_owners = prs_mod.migrate_prs(st.load("prs", {}))
        self.wf_raw = st.load("workflows_raw", {})
        self.repos = st.load("repos", {})
        self.owners = st.load("owners", {})
        self.contacts = st.load("contacts", {})
        self.org_members = st.load("org_members", {})
        self.filtered: dict[str, str] = {}
        self.scores: dict[str, dict] = {}
        self.staff = vendor.Staff()

    def owner_of(self, repo: str) -> str:
        return (self.repos.get(repo) or {}).get("owner") or repo.split("/")[0]

    def owner_type(self, repo: str) -> str | None:
        """owners.json -> discovered hit -> repos.json."""
        o = self.owners.get(self.owner_of(repo)) or {}
        if o.get("type"):
            return o["type"]
        hits = self.discovered.get(repo) or []
        if hits and hits[0].get("owner_type"):
            return hits[0]["owner_type"]
        return (self.repos.get(repo) or {}).get("owner_type")

    def candidates(self, limit: int = 0) -> list[str]:
        allr = sorted(set(self.discovered) | set(self.prs))
        if limit:
            allr.sort(key=lambda r: (0 if self.owner_type(r) == "Organization" else 1, r.lower()))
            allr = allr[:limit]
        return allr


def _metadata_reason(m: dict) -> str | None:
    if not m:
        return None
    if m.get("missing"):
        return "repo_missing"
    for key, reason in (("is_fork", "fork"), ("is_archived", "archived"), ("is_mirror", "mirror"), ("is_template", "template")):
        if m.get(key):
            return reason
    return None


def _count_values(d: dict) -> dict:
    out: dict[str, int] = {}
    for v in d.values():
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def _merge_prs(existing: dict, fresh: dict) -> dict:
    """Fresh search results win, but bodies already backfilled are kept."""
    out = dict(existing)
    for repo, plist in fresh.items():
        have = {p["number"]: p for p in existing.get(repo, [])}
        merged = {p["number"]: p for p in existing.get(repo, [])}
        for p in plist:
            old = have.get(p["number"])
            if old and old.get("body") is not None and p.get("body") is None:
                p["body"], p["requested_by"] = old["body"], old.get("requested_by")
            merged[p["number"]] = p
        out[repo] = sorted(merged.values(), key=lambda p: p.get("created_at") or "")
    return out


def run(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    gh = GitHub(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "")
    if args.smoke and args.state_dir == "state":
        # never let a smoke run overwrite the full crawl's checkpoints
        args.state_dir, args.out_dir = "state_smoke", "data_smoke"
        log.info("smoke: using %s and %s", args.state_dir, args.out_dir)
    st = State(args.state_dir)
    budget = Budget(args.time_budget_min)
    phases = PHASES if args.phase == "all" else [args.phase]
    ctx = Ctx(st)
    meta = ctx.meta
    migrate(meta, st)
    needs_api = bool(set(phases) & {"discover", "owners", "repos", "workflows", "contacts"})
    if needs_api and not args.allow_low_rate_limit:
        meta["rate_limits"] = gh.check_budget()
    marker = os.path.join(os.path.dirname(os.path.abspath(args.state_dir)), ".crawl_remaining")
    if os.path.exists(marker):
        os.remove(marker)
    if ctx.dropped_internal:
        log.info("migration: dropped %d internal-bot PRs", ctx.dropped_internal)
        st.save("prs", ctx.prs)
        meta["internal_prs_dropped"] = meta.get("internal_prs_dropped", 0) + ctx.dropped_internal
        st.save("meta", meta)  # persist even if the run stops before the first phase completes
    n = migrate_workflows(ctx.wf_raw)
    if n:
        log.info("migration: %d PR-only workflow records reset for tree scan", n)
        st.save("workflows_raw", ctx.wf_raw)
        meta["workflows_reset_for_tree_scan"] = meta.get("workflows_reset_for_tree_scan", 0) + n
    n = migrate_repos(ctx.repos)
    if n:
        log.info("migration: %d v1 repo records marked deep", n)
        st.save("repos", ctx.repos)
    meta["run_started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ------------------------------------------------------------ discover
    if "discover" in phases and budget.ok():
        with Phase("discover", gh, meta, st) as ph:
            actions = [a.strip() for a in (args.actions or "").split(",") if a.strip()] or None
            if not meta.get("discover_code_done") or args.rediscover or args.smoke:
                ctx.discovered = discover.discover_code(gh, args.state_dir, actions, smoke=args.smoke)
                st.save("discovered", ctx.discovered)
                meta["discover_code_done"] = not args.smoke
                meta["discovered_repos"] = len(ctx.discovered)
            if (not meta.get("discover_prs_done") or args.rediscover or args.smoke) and not args.skip_prs:
                fresh = discover.discover_prs(gh, args.state_dir, smoke=args.smoke)
                ctx.prs = _merge_prs(ctx.prs, fresh) if not args.smoke else fresh
                st.save("prs", ctx.prs)
                meta["discover_prs_done"] = not args.smoke
                meta["pr_repos"] = len(ctx.prs)
            ph.rows = len(ctx.discovered) + len(ctx.prs)

    ctx.staff = vendor.build_staff(gh, ctx.prs, st.load("staff", None), ctx.sandbox_owners)
    st.save("staff", ctx.staff.to_json())
    meta["staff_logins"] = len(ctx.staff.logins)

    candidates = ctx.candidates(args.limit)
    meta["funnel"] = {"candidates": len(candidates), "internal_prs_dropped": meta.get("internal_prs_dropped", 0)}
    log.info("%d candidate repos", len(candidates))

    # -------------------------------------------------------------- owners
    if "owners" in phases and budget.ok():
        with Phase("owners", gh, meta, st) as ph:
            logins = sorted({ctx.owner_of(r) for r in candidates})
            todo = [l for l in logins if l not in ctx.owners or ctx.owners[l].get("missing")]
            log.info("owners: %d logins, %d to fetch", len(logins), len(todo))
            for i in range(0, len(todo), 500):
                if not budget.ok():
                    break
                chunk = todo[i : i + 500]
                ctx.owners.update(enrich.enrich_owners(gh, chunk))
                st.save("owners", ctx.owners)
                ph.rows += len(chunk)
            meta["owners_done"] = sum(1 for l in logins if l in ctx.owners and not ctx.owners[l].get("missing"))
            meta["owners_total"] = len(logins)

    # ------------------------------------------------------------ classify
    survivors = list(candidates)
    with Phase("classify", gh, meta, st) as ph:
        survivors = []
        for r in candidates:
            owner = ctx.owner_of(r)
            otype = ctx.owner_type(r)
            if ctx.staff.is_denylisted(owner):
                ctx.filtered[r] = "denylist"
            elif otype == "User":
                ctx.filtered[r] = "individual"
            elif otype is None and (ctx.owners.get(owner) or {}).get("missing"):
                ctx.filtered[r] = "owner_missing"
            else:
                reason = _metadata_reason(ctx.repos.get(r) or {})
                if reason:
                    ctx.filtered[r] = reason
                else:
                    survivors.append(r)
        st.save("filtered", ctx.filtered)
        ph.rows = len(ctx.filtered)
        meta["funnel"]["after_classify"] = len(survivors)
        meta["funnel"]["filtered_reasons"] = _count_values(ctx.filtered)
        log.info("classify: %d survivors, %d filtered %s", len(survivors), len(ctx.filtered), meta["funnel"]["filtered_reasons"])

    # --------------------------------------------------------------- repos
    if "repos" in phases and budget.ok():
        with Phase("repos", gh, meta, st) as ph:
            todo = [r for r in survivors if r not in ctx.repos or ctx.repos[r].get("missing")]
            log.info("repos: %d survivors, %d to fetch (light)", len(survivors), len(todo))
            for i in range(0, len(todo), 1000):
                if not budget.ok():
                    break
                chunk = todo[i : i + 1000]
                ctx.repos.update(enrich.enrich_repos(gh, chunk))
                st.save("repos", ctx.repos)
                ph.rows += len(chunk)
            for r in list(survivors):
                reason = _metadata_reason(ctx.repos.get(r) or {})
                if reason:
                    ctx.filtered[r] = reason
                    survivors.remove(r)
            st.save("filtered", ctx.filtered)
            meta["funnel"]["after_metadata"] = len(survivors)
            meta["funnel"]["filtered_reasons"] = _count_values(ctx.filtered)

            if budget.ok():
                n = prs_mod.backfill_bodies(gh, ctx.prs, set(survivors))
                if n:
                    st.save("prs", ctx.prs)
                    ctx.staff = vendor.build_staff(None, ctx.prs, ctx.staff.to_json(), ctx.sandbox_owners)
                    st.save("staff", ctx.staff.to_json())
                meta["pr_bodies_backfilled"] = meta.get("pr_bodies_backfilled", 0) + n
            if budget.ok():
                need = sorted({
                    ctx.owner_of(r)
                    for r in survivors
                    if ctx.owner_type(r) == "Organization"
                    for p in ctx.prs.get(r, [])
                    if p.get("author") == vendor.SECURE_REPO_BOT
                    and p.get("requested_by")
                    and p["requested_by"].lower() != ctx.owner_of(r).lower()
                    and not ctx.staff.is_staff(p["requested_by"])
                })
                ctx.org_members = enrich.public_members(gh, need, ctx.org_members)
                st.save("org_members", ctx.org_members)
            meta["repos_done"] = sum(1 for r in survivors if r in ctx.repos and not ctx.repos[r].get("missing"))
            meta["repos_total"] = len(survivors)

    # ----------------------------------------------------------- workflows
    if "workflows" in phases and budget.ok():
        with Phase("workflows", gh, meta, st) as ph:
            todo = {r: ctx.discovered.get(r, []) for r in survivors if r not in ctx.wf_raw}
            log.info("workflows: %d survivors, %d to fetch", len(survivors), len(todo))
            keys = list(todo)
            for i in range(0, len(keys), 500):
                if not budget.ok():
                    log.warning("time budget exhausted during workflows phase")
                    break
                chunk = {k: todo[k] for k in keys[i : i + 500]}
                ctx.wf_raw.update(workflows.fetch_and_parse(gh, chunk))
                st.save("workflows_raw", ctx.wf_raw)
                ph.rows += len(chunk)
            meta["workflows_done"] = sum(1 for r in survivors if r in ctx.wf_raw)
            meta["workflows_total"] = len(survivors)

    wf = {r: workflows.summarize_repo(s) for r, s in ctx.wf_raw.items() if not s.get("missing")}

    # --------------------------------------------------------------- score
    top_accounts: list[str] = []
    with Phase("score", gh, meta, st) as ph:
        _, acct_rows = _build_rows(ctx, candidates, wf)
        ctx.scores = {a["account"]: {"rank": a["rank"], "score": a["score"], "depth_score": a["depth_score"],
                                     "provenance": a["provenance"], "suppress_reason": a["suppress_reason"]} for a in acct_rows}
        st.save("scores", ctx.scores)
        top_accounts = [a["account"] for a in acct_rows if (a.get("score") or 0) > 0][: args.contacts_top_accounts]
        ph.rows = len(acct_rows)
        meta["funnel"]["scored_accounts"] = len(acct_rows)
        meta["funnel"]["scored_accounts_positive"] = sum(1 for a in acct_rows if (a.get("score") or 0) > 0)
        meta["funnel"]["contacts_top_accounts"] = len(top_accounts)

    # ------------------------------------------------------------ contacts
    top_set = set(top_accounts)
    top_repos = [r for r in survivors if ctx.owner_of(r) in top_set]
    if "contacts" in phases and budget.ok() and not args.skip_contacts:
        with Phase("contacts", gh, meta, st) as ph:
            enrich.enrich_repos_deep(gh, ctx.repos, top_repos)
            st.save("repos", ctx.repos)
            todo = [r for r in top_repos if r not in ctx.contacts and not (ctx.repos.get(r) or {}).get("missing")]
            rank = {a: i for i, a in enumerate(top_accounts)}
            todo.sort(key=lambda r: (rank.get(ctx.owner_of(r), 10**9), 0 if (wf.get(r) or {}).get("uses_harden_runner") else 1, -((ctx.repos.get(r) or {}).get("stars") or 0)))
            log.info("contacts: %d repos across %d accounts, %d to fetch", len(top_repos), len(top_accounts), len(todo))
            for i in range(0, len(todo), 200):
                if not budget.ok(margin_s=240):
                    log.warning("time budget exhausted during contacts phase (%d/%d done)", i, len(todo))
                    break
                items = [
                    {
                        "repo": r,
                        "hr_paths": (wf.get(r) or {}).get("harden_runner_paths") or (wf.get(r) or {}).get("stepsecurity_paths") or [],
                        "pr_numbers": [p["number"] for p in (ctx.prs.get(r) or []) if p.get("merged_at")][:3],
                    }
                    for r in todo[i : i + 200]
                ]
                ctx.contacts.update(contacts_mod.collect_contacts(gh, items, walk_history=not args.no_history_walk, staff=ctx.staff))
                st.save("contacts", ctx.contacts)
                ph.rows += len(items)
            meta["contacts_done"] = sum(1 for r in top_repos if r in ctx.contacts)
            meta["contacts_total"] = len(top_repos)

    # -------------------------------------------------------------- export
    if "export" in phases:
        with Phase("export", gh, meta, st) as ph:
            repo_rows, acct_rows = _build_rows(ctx, candidates, wf)
            repo_owner = {r: ctx.owner_of(r) for r in candidates}
            people = contacts_mod.aggregate_people(ctx.contacts, repo_owner, ctx.staff)
            meta["denylist_repos_removed"] = repo_rows.denylisted + sum(1 for v in ctx.filtered.values() if v == "denylist")
            meta["denylist_prs_removed"] = sum(len(v) for r, v in ctx.prs.items() if ctx.staff.is_denylisted(r.split("/")[0]))
            contact_rows = export.build_contact_rows(people)
            file_rows = export.build_file_rows(ctx.wf_raw)
            indiv = [r for r in candidates if ctx.filtered.get(r) == "individual"]
            indiv_rows = export.build_individual_rows(indiv, ctx.owners, wf, ctx.prs)
            supp_rows = export.build_suppressed_rows(acct_rows)
            os.makedirs(args.out_dir, exist_ok=True)
            export.write_csv(os.path.join(args.out_dir, "accounts.csv"), acct_rows, export.ACCOUNT_COLUMNS)
            export.write_csv(os.path.join(args.out_dir, "repos.csv"), repo_rows, export.REPO_COLUMNS)
            export.write_csv(os.path.join(args.out_dir, "contacts.csv"), contact_rows, export.CONTACT_COLUMNS)
            export.write_csv(os.path.join(args.out_dir, "workflow_files.csv"), file_rows, export.FILE_COLUMNS)
            export.write_csv(os.path.join(args.out_dir, "individuals.csv"), indiv_rows, export.INDIVIDUAL_COLUMNS)
            export.write_csv(os.path.join(args.out_dir, "suppressed.csv"), supp_rows, export.SUPPRESSED_COLUMNS)
            meta["exported_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            meta["requests_this_run"] = gh.requests_made
            meta["requests_by_bucket_this_run"] = dict(gh.requests_by_bucket)
            meta["rate_limits"] = dict(gh.limits)
            ph.rows = len(acct_rows) + len(repo_rows) + len(contact_rows)
            st.save("meta", meta)
            export.write_summary(os.path.join(args.out_dir, "SUMMARY.md"), acct_rows, repo_rows, people, meta)

    remaining = _remaining_work(ctx, survivors, top_repos, args)
    meta["remaining_work"] = remaining
    st.save("meta", meta)
    if remaining:
        log.warning("work remaining: %s; re-run to continue", remaining)
        # crawl.yml reads this after committing state and dispatches the next run
        with open(marker, "w") as f:
            json.dump(remaining, f)
    log.info("done. requests: %d by bucket %s", gh.requests_made, gh.requests_by_bucket)
    return 0


def _build_rows(ctx: Ctx, candidates: list[str], wf: dict):
    """Repo and account rows from whatever state exists. No API calls."""
    hidden = {"denylist", "individual", "owner_missing"}
    exportable = [r for r in candidates if ctx.filtered.get(r) not in hidden]
    scoped = set(exportable)
    repo_owner = {r: ctx.owner_of(r) for r in candidates}
    people = contacts_mod.aggregate_people(ctx.contacts, repo_owner, ctx.staff)
    by_acct = contacts_mod.account_contact_summary(people)
    repo_rows = export.build_repo_rows(
        {r: ctx.repos.get(r, {"repo": r}) for r in exportable},
        {r: v for r, v in wf.items() if r in scoped},
        {r: v for r, v in ctx.prs.items() if r in scoped},
        ctx.contacts,
        ctx.discovered,
        ctx.staff,
        ctx.org_members,
        owner_types={r: ctx.owner_type(r) for r in candidates},
        filtered=ctx.filtered,
        wf_sources={r: v.get("source") for r, v in ctx.wf_raw.items()},
    )
    acct_rows = export.build_account_rows(repo_rows, ctx.owners, by_acct)
    return repo_rows, acct_rows


def _remaining_work(ctx: Ctx, survivors: list[str], top_repos: list[str], args) -> dict:
    rem: dict[str, int | bool] = {}
    if args.phase != "all" or args.smoke:
        return rem
    if not ctx.meta.get("discover_code_done"):
        rem["discover_code"] = True
    if not ctx.meta.get("discover_prs_done") and not args.skip_prs:
        rem["discover_prs"] = True
    n = sum(1 for r in survivors if r not in ctx.repos or ctx.repos[r].get("missing"))
    if n:
        rem["repos"] = n
    n = sum(1 for r in survivors if r not in ctx.wf_raw)
    if n:
        rem["workflows"] = n
    if not args.skip_contacts:
        n = sum(1 for r in top_repos if r not in ctx.contacts and not (ctx.repos.get(r) or {}).get("missing"))
        if n:
            rem["contacts"] = n
    return rem


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", choices=["all"] + PHASES, default="all")
    p.add_argument("--state-dir", default="state")
    p.add_argument("--out-dir", default="data")
    p.add_argument("--time-budget-min", type=float, default=330)
    p.add_argument("--limit", type=int, default=0, help="cap candidate repos (smoke tests); organizations first")
    p.add_argument("--actions", default="", help="comma list of step-security action names to search; default all")
    p.add_argument("--contacts-top-accounts", type=int, default=400, help="run contacts and deep metadata for the top N accounts by score")
    p.add_argument("--rediscover", action="store_true", help="re-run discovery even if marked done")
    p.add_argument("--skip-prs", action="store_true")
    p.add_argument("--skip-contacts", action="store_true")
    p.add_argument("--no-history-walk", action="store_true", help="don't page file history to find the first commit")
    p.add_argument("--smoke", action="store_true", help="one page of discovery only; validates the pipeline end to end in minutes")
    p.add_argument("--allow-low-rate-limit", action="store_true", help="skip the 5,000/hour token check (local debugging only)")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
