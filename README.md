# stepsec-audit

Crawls public GitHub for repositories that use [StepSecurity](https://www.stepsecurity.io/) actions (harden-runner and the rest of the `step-security` org) and exports account, repo, and contact CSVs for ABM targeting. The output lists organizations whose own engineers chose to run StepSecurity, with the people who made that choice, and separates them from the vendor's own accounts, its existing customers, and individuals.

## How to run

The crawler runs as a GitHub Actions workflow in this repo (the API calls it needs are not reachable from a Claude Code sandbox).

1. Add a repo secret **`CRAWLER_TOKEN`**: a fine-grained personal access token, public repositories, read-only. It carries the 5,000 requests/hour REST and GraphQL budgets. The crawler checks `/rate_limit` on start and fails fast with an explanation if the token has the 1,000/hour `GITHUB_TOKEN` budget.
2. Actions tab → **crawl** → **Run workflow**. Leave defaults for a full crawl.
3. Each run stops cleanly at `time_budget_min` (default 330), commits `data/` and `state/`, uploads `data/` as an artifact, and if work remains dispatches itself again (`chain` + 1, up to `max_chain`, default 4). Finished work is never repeated.
4. Download `data/*.csv` from the repo or the run's artifact.

Smoke test: set `extra_args` to `--smoke --limit 40`. Runs one page of discovery and every phase on ~40 repos in a few minutes, writing to `state_smoke/` and `data_smoke/` so it cannot disturb a full crawl's checkpoints.

Local run: `pip install -r requirements.txt && GITHUB_TOKEN=... python -m crawler.main`. Add `--allow-low-rate-limit` to bypass the token check when debugging; `--phase export` needs no token.

`verify` workflow: runs a script under `scripts/` inside Actions for ad-hoc API checks (used to confirm PR body wording before P0.2).

## Pipeline

Phases run in a funnel: each one filters before the next spends per-repo requests. All state is additive JSON under `state/`; a schema bump migrates in place.

| phase | what it does | requests |
|---|---|---|
| discover | Code search for `"step-security/<action>" path:.github/workflows` for every public repo in the `step-security` org, plus `action.yml` and the secure-repo comment marker. GitHub caps each query at 1,000 results, so buckets are bisected on file `size:` until each fits. PR search for `author:step-security-bot`, `author:app/stepsecurity-app`, and the secure-repo title, bisected on `created:`. PRs from `stepsecurity-int[bot]`, `step-security-bot-int` and `stepsecurity-advanced-*[bot]` are dropped at ingest. Results are cached per bucket. | code search 10/min, search 30/min |
| owners | Profile for every distinct owner (~3,700 logins, batch 50): type, name, website, public email, location, verified flag; for users also company and orgs. | GraphQL |
| classify | No requests. Drops from the work queue and records a reason in `state/filtered.json`: `denylist` (StepSecurity's accounts, sandboxes, `/stepsecurity\|step-security/i`), `individual` (user-owned; listed in `individuals.csv`), `owner_missing`, and, once metadata exists, `fork`, `archived`, `mirror`, `template`, `repo_missing`. | — |
| repos | Light metadata for survivors (batch 100): fork/archive flags, stars, dates, language, license, default branch, parent, owner. Then the second filter, a body backfill for secure-repo PRs that lack one, and public-member lists for orgs whose secure-repo requester is not the owner. | GraphQL, REST |
| workflows | Fetches each code-search hit via batched GraphQL and parses it: action, ref, SHA-pinned, harden-runner `egress-policy`, `disable-sudo`, `disable-telemetry`, `allowed-endpoints`, policy store / API key / self-hosted, `runs-on`. Repos discovered only through PRs get a second pass over up to 12 files from the `.github/workflows` tree listing (preferring `ci\|build\|release\|publish\|test\|codeql\|scorecard\|dependency`) so a live harden-runner is not missed. | GraphQL |
| score | No requests. Builds account rows, computes `score` (below), ranks, and picks the top N accounts (`--contacts-top-accounts`, default 400) with `score > 0`. | — |
| contacts | Top-N accounts only. Deep repo metadata (topics, languages, watchers, issues, release, funding), then the last 100 commits per repo → top human committers with commit emails; harden-runner file history → who introduced it and when; merger of the StepSecurity PR. Bots and StepSecurity staff excluded. | GraphQL |
| export | `accounts.csv`, `repos.csv`, `contacts.csv`, `individuals.csv`, `suppressed.csv`, `workflow_files.csv`, `SUMMARY.md` (funnel counts, per-phase wall time and requests per bucket, denylist removals, provenance distribution). | — |

### Vendor denylist and staff (`crawler/vendor.py`, `state/staff.json`)

Hard-coded accounts (`step-security`, `step-security-experiments`, `step-integration-tests`, `harden-runner-canary`, `actions-security-demo`, `varunsh-coder`, `ashishkurmi`, `sailikhith-stepsecurity`, `Raj-StepSecurity`, `Raj-Organization-1234`, `anurags-org-returns`, `vamshi-stepsecurity`, …) plus any login matching `/stepsecurity|step-security/i`, plus every owner the integration bots ever targeted. Staff: public members of the `step-security` org, `@stepsecurity.io` emails, sandbox owners, and any requester or PR author whose StepSecurity PRs land in five or more unrelated owners. Denylisted owners never appear in `accounts.csv`, `repos.csv` or `contacts.csv`; staff never appear as contacts; a staff-authored adoption commit marks the repo `vendor_initiated` and the PR merger gets the `org_side_contact` role.

### PR provenance (`crawler/prs.py`)

Each stored PR keeps `body` (2,000 chars), `requested_by` (parsed from `at the request of @login`, verified against live secure-repo PRs; 2022-era bodies lack it), `merge_latency_h`, and gets a `pr_class` at export time:

| pr_class | meaning |
|---|---|
| `app_installed` | `stepsecurity-app[bot]`; body says "as part of your enterprise subscription". An org admin installed the GitHub App. |
| `secure_repo_self` | `step-security-bot`; requester is the repo owner, a public org member, or a recent committer. |
| `secure_repo_vendor` | `step-security-bot`; requester is StepSecurity staff. |
| `secure_repo_third_party` | `step-security-bot`; requester is none of the above. |
| `secure_repo_unknown` | `step-security-bot`; body carries no requester (old PRs, or body not yet backfilled). |
| `human_pr` | any other human author (becomes a `human_pr_author`). |
| `revert` | title starts with `Revert "`; sets `reverted=true` on the repo and records the reverted PR number. |
| `internal` | integration/internal bots; the repo is excluded from every output. |

## Output

All new columns are nullable.

**`data/accounts.csv`** — one row per organization, ordered by `score`.

- Identity: `account`, `account_type`, `rank`, `account_url`, `account_name`, `company_hint`, `website`, `public_email`, `location`, `twitter`, `description`, `is_verified_org`, `public_repos`, `followers`, `user_orgs`, `account_created_at`
- `inferred_domain` + `domain_source` (owner website → owner email → committer/homepage domain matching the account name → majority corporate committer domain → repo homepage → weak single committer domain). Feed this to your enrichment provider.
- `score` = fit × `depth_score` × `recency_factor`. fit is 1 for an Organization with no `suppress_reason`, else 0. Drives contacts top-N and row order.
- `depth_score` 0–100: presence 10; breadth 2/repo up to 30; egress-`block` share up to 20 (discounted below three repos); `allowed-endpoints` set 7; policy store 10; `disable-sudo` 5; egress set by expression 5; harden-runner in a release/publish/deploy workflow 8; workflow coverage up to 5.
- `provenance`: `app_installed` > `secure_repo_self` > `human_pr` > `hand_written` (harden-runner live, no StepSecurity PRs) > `secure_repo_third_party` > `secure_repo_vendor` > `secure_repo_unknown` > `unknown`. `org_standard` and `inherited` are reserved for P1 and stay empty.
- `suppress_reason` (may hold several): `app_installed` (existing App customer), `policy_store` (paid feature in use), `individual`, `all_reverted` (revert PRs and nothing live). `foundation` and `demo_like` are reserved for P1.
- `breadth` (= `repos_with_harden_runner`), `rollout` (five or more adoptions inside any 30-day window), `recency_factor`, `latest_push`, `first_adoption`
- Where it runs: `path_hygiene_repos` (codeql, scorecard, dependency-review, security), `path_build_repos` (ci, build, test, lint), `path_release_repos` (release, publish, deploy, docker). Hygiene-only adoption is what secure-repo produces by default.
- Configuration depth: `egress_block_repos`, `egress_audit_repos`, `egress_unset_repos`, `egress_expr_repos`, `policy_store_repos`, `self_hosted_repos`, `disable_sudo_repos`, `disable_telemetry_repos`, `allowed_endpoints_max`, `avg_pinned_sha_ratio`, `workflows_total`, `workflows_with_stepsecurity`, `workflow_coverage_pct`, `other_stepsecurity_actions`
- PR provenance: `app_installed`, `secure_repo_self_prs`, `secure_repo_vendor_prs`, `secure_repo_third_party_prs`, `secure_repo_unknown_prs`, `human_prs`, `reverts`, `pr_median_latency_h`, `stepsecurity_prs`, `stepsecurity_prs_merged`, `stepsecurity_pr_first`
- Repos and people: `repos_using_stepsecurity`, `repos_bot_pr_only`, `repo_list`, `total_stars`, `max_stars`, `top_repo`, `languages`, `contacts_count`, `corporate_emails`, `corporate_email_domains`, `adopters`, `top_contacts` (`login|name|email|roles`)
- `legacy_tier`: the v1 tier label, kept for one release. `org_standard`, `inherited_repos`, `live_repos`, `latest_human_touch`, `foundation`: P1 placeholders, empty.

**`data/repos.csv`** — one row per repo. Repo metadata, the StepSecurity configuration, `path_class`, `egress_expr`, PR provenance (`pr_classes`, `pr_requesters`, `pr_human_authors`, `pr_median_latency_h`, `reverted`, `reverted_prs`, `app_installed`), adoption date and adopter, `discovery_sources` (`code_search`, `bot_pr`, `tree_scan`), and `filtered_reason` for forks, archived, mirror and template repos, which stay in this file but do not count toward any account. `owner_type` is populated from the owner profile or the code-search hit for every row.

**`data/contacts.csv`** — one row per person at a top-N account: `login`, `name`, `emails`, `corporate_emails`, `company`, `location`, `roles` (`harden_runner_adopter`, `merged_stepsecurity_pr`, `org_side_contact`, `top_committer`, `committer`, `repo_owner`), `accounts`, `repos`, `commits`, `last_commit`, `first_adoption`. StepSecurity staff are never listed.

**`data/individuals.csv`** — user-owned accounts, unenriched beyond the profile: `login`, `name`, `company`, `website`, `location`, `repos`, `repos_with_harden_runner`, `stepsecurity_prs`.

**`data/suppressed.csv`** — `account`, `suppress_reason`, `evidence`, one row per reason.

**`data/workflow_files.csv`** — one row per StepSecurity step found, for auditing the parser.

**`data/SUMMARY.md`** — funnel (candidates → after classify → after metadata → scored → contacts top-N), filtered reasons, provenance and suppression counts, per-phase wall time and requests per rate bucket, denylist removals, remaining work.

## Fidelity notes

- Code search covers default branches of repos GitHub has indexed (active in roughly the last year, files under 384 KB). Repos found only through PRs are read from their workflow tree instead.
- Repos that call a *reusable workflow* containing harden-runner do not show up in code search. The org that owns the reusable workflow does. Caller resolution is P1.
- `egress_unset` means the input was omitted; `egress_expr` means it is set by an expression. harden-runner's default has changed over versions, so neither is guessed.
- Commit emails are as committed. Roughly half of active developers use GitHub's noreply address; those are dropped. Corporate vs. freemail is a domain list, not a verification.
- The adopter is the author of the oldest commit touching the harden-runner workflow file. If harden-runner was added to a pre-existing file, that is the file's creator, not the person who added the step (blame-based attribution is P1). `adoption_date` prefers the merge date of a StepSecurity PR when one exists.
- "Org member" for `secure_repo_self` means *public* member; that is all the API exposes for organizations the token is not part of.
- Contacts are collected for the top N accounts only. Everything else in `accounts.csv` is complete; only the people columns are empty below the cut.

## Use restrictions

GitHub's Acceptable Use Policies prohibit using data from the API to send unsolicited email or to sell personal information. Use `accounts.csv` (organizations and domains) as the ABM account list and run contacts through a compliant B2B data provider. Treat `contacts.csv` as a champion/role map, not a mailing list. GDPR applies to EU-resident individuals. The crawler stores only public commit metadata, public profile fields and PR metadata.
