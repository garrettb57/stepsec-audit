# stepsec-audit

Crawls public GitHub for repositories that use [StepSecurity](https://www.stepsecurity.io/) actions (harden-runner and the rest of the `step-security` org) and exports account, repo, and contact CSVs for ABM targeting.

## How to run

The crawler runs as a GitHub Actions workflow in this repo (the API calls it needs are not reachable from a Claude Code sandbox).

1. Actions tab → **crawl** → **Run workflow**.
2. Leave defaults for a full crawl. Each run stops cleanly at `time_budget_min` (default 330), commits `data/` and `state/`, and uploads `data/` as an artifact. Re-run to resume; finished work is not repeated.
3. Download `data/*.csv` from the repo or the run's artifact.

Optional: add a repo secret `CRAWLER_TOKEN` holding a personal access token (fine-grained, public repositories, read-only is enough). It raises the REST limit from 1,000 to 5,000 requests/hour and the GraphQL budget from 1,000 to 5,000 points/hour. Without it the workflow uses `GITHUB_TOKEN`.

Smoke test: set `extra_args` to `--smoke --limit 40`. Runs one page of discovery and the full pipeline on ~40 repos in a few minutes.

Local run: `pip install -r requirements.txt && GITHUB_TOKEN=... python -m crawler.main`.

## Pipeline

| phase | what it does | rate bucket |
|---|---|---|
| discover | Code search for `"step-security/<action>" path:.github/workflows` for every public repo in the `step-security` org, plus `action.yml` and the secure-repo comment marker. GitHub caps each query at 1,000 results, so buckets are bisected on file `size:` until each fits. Also searches PRs opened by `step-security-bot` (the secure-repo app), bisected on `created:` dates. | code search 10/min, search 30/min |
| workflows | Pulls each hit file via batched GraphQL and parses it: action, ref, SHA-pinned or not, harden-runner `egress-policy`, `disable-sudo`, `disable-telemetry`, `allowed-endpoints`, policy-store / API-key / self-hosted (paid-tier indicators), `runs-on`. Counts total workflow files for coverage. | GraphQL |
| enrich | Repo metadata (stars, forks, language, topics, license, dates, security policy, funding) and owner profile (org name, website, public email, location, verified flag, member count; for users: company, orgs). | GraphQL |
| contacts | Last 100 commits on the default branch → top human committers with names and commit emails. File history of the harden-runner workflow → the person who introduced it and when. Merger of the secure-repo bot PR. Bots filtered. | GraphQL |
| export | `accounts.csv`, `repos.csv`, `contacts.csv`, `workflow_files.csv`, `SUMMARY.md`. | — |

Repos are processed in priority order (organizations first, harden-runner users first, then stars) so a partial run still covers the best targets.

## Output

**`data/accounts.csv`** — one row per GitHub owner. Key columns:

- `account`, `account_type`, `account_name`, `company_hint`, `website`, `public_email`, `location`, `twitter`, `is_verified_org`, `public_members`, `user_orgs`
- `inferred_domain` + `domain_source` (owner website → owner email → majority corporate committer domain → repo homepage). Feed this to your enrichment provider.
- `tier` + `tier_reason`:
  - `likely_customer` — policy store, API key, or self-hosted harden-runner (paid features). Treat as expansion or exclude.
  - `power_user` — egress `block` across ≥3 repos or ≥50% of workflows.
  - `adopter_block` — egress `block` in at least one repo.
  - `adopter_audit` — harden-runner in audit or default mode only.
  - `other_actions_only` — uses StepSecurity maintained actions but not harden-runner.
  - `bot_pr_only` — the secure-repo bot opened PRs, but harden-runner is not live on the default branch (tried it, did not keep it).
  - `individual` — user-owned account.
- Usage depth: `repos_with_harden_runner`, `egress_block_repos`, `egress_audit_repos`, `workflow_coverage_pct`, `avg_pinned_sha_ratio`, `disable_telemetry_repos`, `first_adoption`, `latest_push`, `total_stars`, `top_repo`, `repo_list`
- People: `contacts_count`, `corporate_emails`, `corporate_email_domains`, `adopters`, `top_contacts` (`login|name|email|roles`)

**`data/repos.csv`** — one row per repo with all metadata plus the StepSecurity configuration, adoption date and adopter, and discovery source.

**`data/contacts.csv`** — one row per person: `login`, `name`, `emails`, `corporate_emails`, `company`, `location`, `roles` (`harden_runner_adopter`, `merged_stepsecurity_pr`, `top_committer`, `repo_owner`), `accounts`, `repos`, `commits`, `first_adoption`.

**`data/workflow_files.csv`** — one row per StepSecurity step found, for auditing the parser.

## Fidelity notes

- Code search covers default branches of repos that GitHub has indexed (active in roughly the last year, files under 384 KB). Dormant repos are under-represented; they are also weak targets.
- Repos that call a *reusable workflow* containing harden-runner do not show up in code search. The org that owns the reusable workflow does.
- `egress_unset` means the input was omitted. harden-runner's default has changed over versions, so it is reported as unset rather than guessed.
- Commit emails are as committed. Roughly half of active developers use GitHub's noreply address; those are dropped. Corporate vs. freemail is a domain list, not a verification.
- The adopter is the author of the oldest commit touching the harden-runner workflow file. If harden-runner was added to a pre-existing file, that is the file's creator, not the person who added the step. `adoption_date` prefers the merge date of a secure-repo bot PR when one exists.
- `public_members` counts publicly visible org members only.

## Use restrictions

GitHub's Acceptable Use Policies prohibit using data from the API to send unsolicited email or to sell personal information. Use `accounts.csv` (organizations and domains) as the ABM account list and run contacts through a compliant B2B data provider. Treat `contacts.csv` as a champion/role map, not a mailing list. GDPR applies to EU-resident individuals.
