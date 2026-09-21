# stepsec-audit v2: provenance, funnel, speed

Implementation request for the `claude/github-stepsecurity-crawler-xhk7ie` branch of `garrettb57/stepsec-audit`. Read this whole file, then `README.md`, then `crawler/main.py`, before changing anything. Work in the priority order below. Ship P0 as one PR with passing tests before starting P1. Do not start P2 unless asked.

## Why

Goal: `data/accounts.csv` and `data/contacts.csv` should list organizations whose own engineers chose to run StepSecurity, with the people who made that choice. Two things currently defeat that.

1. **Enrichment starves.** `main.py` runs `workflows` for every candidate repo before `enrich` and `contacts`, and iterates `all_repos = sorted(set(discovered) | set(prs))`, i.e. alphabetically. The last full run (`state/meta.json`, 2026-09-21) parsed workflows for 10,594 repos, fetched repo metadata for 1,060, and collected contacts for 60, in 330 minutes and 1,958 requests. The workflows phase pulls blob text for ~36,000 files in batches of 25 before any filter is applied; that is where the hours went. Owner type is unknown for 3,345 of 3,736 accounts even though `state/discovered.json` already holds `owner_type` and `fork` for every code-search hit (20,784 org hits, 15,371 user hits); `export.py` only reads `owner_type` from the `repos` dict, so 9,538 rows export it empty.
2. **The PR signal is unclassified.** `state/prs.json` holds 12,577 PRs from three different bot identities with three different meanings, and `_slim_issue_item` drops the PR body that names the requester.
   - `step-security-bot` (6,770 PRs, 1,373 owners, since 2022-03): the secure-repo one-shot tool. 2,127 of these target StepSecurity's own accounts (`step-security`, `harden-runner-canary`, `varunsh-coder`, `ashishkurmi`, `step-security-experiments`). Merged ones have a median merge latency of 0.1 h; 3,688 of 4,569 merged within an hour, which means the maintainer ran the tool and merged it themselves.
   - `stepsecurity-app[bot]` (3,300 PRs, 198 owners, since 2025-03-24): the StepSecurity GitHub App. An org admin installed it. Median merge latency 11.6 h. 192 owners after removing StepSecurity's own. This is the strongest platform-engagement signal in the public record and is probably the existing-customer list (it includes coinbase, base, circlefin, coveo, PaddleHQ, chainguard-dev, NetApp, Checkmarx, hashgraph, aerospike, Contrast-Security-OSS, utilitywarehouse).
   - `stepsecurity-int[bot]` (2,393 PRs, 11 owners): 2,315 in `step-integration-tests`, the rest in StepSecurity employee sandboxes. Pure noise. It also leaks employee logins for the staff list.
   - 9 PRs are `Revert "[StepSecurity] Apply security best practices"` (contentful, coveo, chainguard-dev, bytedance, opentracing-contrib, others). That is a churn signal and it is currently invisible.

## Constraints (apply to every change)

- Keep the checkpointed `state/` design and resumability. New fields are additive; a rerun on existing state must not refetch what it already has. Write a one-time migration that adds new keys with `null` rather than invalidating `state/*.json`.
- One token, serial requests by default. Do not rotate tokens or run parallel jobs against the same token. A `--concurrency N` flag (default 1, max 3) is acceptable only with backoff on `Retry-After` and on 403 secondary-limit responses.
- Every new parser behavior gets a test in `tests/` with a fixture. `pytest` already runs in `crawl.yml`; keep it green.
- Every new CSV column is nullable and documented in `README.md`. Update the README's pipeline table and output section as part of the same PR.
- Keep the 330-minute budget and the 355-minute job timeout. Log per-phase wall time, request count per rate bucket, and rows processed into `meta` and `SUMMARY.md`.
- Do not fetch or store anything beyond what the pipeline already touches (public commit metadata, public profile fields, PR metadata). Keep the README's use-restrictions section.

## P0: must ship. Without these the output cannot be used.

### P0.1 Reorder into a funnel; filter before every per-repo call

New phase order in `main.py`: `discover` → `owners` → `classify` → `repos` → `workflows` → `score` → `contacts` → `export`.

- `owners` first. ~3,700 logins at `enrich_owners` batch 50 is ~75 GraphQL calls. Run it before anything per-repo. Fall back to `discovered[repo][0]["owner_type"]` and `["fork"]` wherever `repos[...]` is missing (export, `_priority`, filters).
- `classify` (no API calls): drop from the work queue, but record in a side table with a reason:
  - owner in the denylist (P0.3) or matching `/stepsecurity|step-security/i`
  - `owner_type == "User"` (keep in `individuals.csv`; spend no more budget on them)
  - repo discovered only via `stepsecurity-int[bot]` or `step-security-bot-int` PRs
- `repos` (metadata) for survivors only. Split `REPO_FIELDS` in `enrich.py` into a light set for this pass (`isFork isArchived isMirror isTemplate isInOrganization stargazerCount pushedAt createdAt primaryLanguage defaultBranchRef owner parent homepageUrl description isSecurityPolicyEnabled`) and move `languages`, `repositoryTopics`, `watchers`, `openIssues`, `openPRs`, `fundingLinks`, `diskUsage`, `latestRelease` to a deep pass that runs only for top-N accounts. Raise the light batch to 100; `run_batched` already halves on 502.
- Second filter after metadata: drop `isFork`, `isArchived`, `isMirror`, `isTemplate`. Record reason.
- `workflows` for survivors only. Keep `fetch_and_parse` as is except P0.5.
- `score` (P0.4), then `contacts` for repos belonging to the top N accounts by score, N configurable (`--contacts-top-accounts`, default 400). Never run contacts for the full set.

Acceptance: a fresh full run completes owners for 100% of accounts, metadata and workflows for 100% of surviving repos, and contacts for the top N accounts, inside one 330-minute budget or one auto-chained pair of runs (P0.6). Report the funnel counts (discovered → after classify → after metadata filter → scored → contacts) in `SUMMARY.md`.

### P0.2 Classify every StepSecurity PR by provenance

In `discover.py`:

- Keep `body` in `_slim_issue_item` (the search API returns it; confirm on the first page and fall back to a GraphQL body fetch for top-N accounts if it does not). Store `body` truncated to 2,000 chars and a parsed `requested_by`.
- Parse the requester from the secure-repo body. Expected wording is `at the request of @login`; verify against five real PRs before hard-coding the regex and log any body where the parse fails.
- Add discovery queries: `is:pr author:app/stepsecurity-app`. Keep the existing author and title queries. Drop `author:app/stepsecurity-int`, `step-security-bot-int`, and `stepsecurity-advanced-*[bot]` results at ingest.
- Classify each PR into `pr_class`:
  - `internal`: authors above → exclude the repo from all outputs
  - `app_installed`: `stepsecurity-app[bot]`
  - `secure_repo_self`: `step-security-bot` and requester is the repo owner, an org member, or a committer in the repo's recent history
  - `secure_repo_vendor`: `step-security-bot` and requester is in the staff list (P0.3)
  - `secure_repo_third_party`: `step-security-bot` and requester is neither
  - `human_pr`: any other human author; author becomes a `human_pr_author` contact
  - `revert`: title starts with `Revert "` (also set `reverted=true` on the repo)
- Compute `merge_latency_h = merged_at - created_at` per merged PR.

In `export.py`, per repo: `pr_classes`, `pr_requesters`, `pr_median_latency_h`, `reverted`. Per account: `app_installed` (any `app_installed` PR), `secure_repo_self_prs`, `secure_repo_vendor_prs`, `human_prs`, `reverts`, `pr_median_latency_h`.

Acceptance: `stepsecurity-int[bot]` repos absent from every CSV; `app_installed` accounts present in `suppressed.csv` with reason `app_installed`; a fixture test for each `pr_class` and for the requester regex including a body that does not match.

### P0.3 Staff and self-noise denylist

New module `crawler/vendor.py`, persisted to `state/staff.json`:

- Account denylist, hard-coded and extended at runtime: `step-security`, `step-security-experiments`, `step-integration-tests`, `harden-runner-canary`, `actions-security-demo`, `varunsh-coder`, `ashishkurmi`, `sailikhith-stepsecurity`, `Raj-StepSecurity`, `Raj-Organization-1234`, `anurags-org-returns`, `vamshi-stepsecurity`, plus any login matching `/stepsecurity|step-security/i`.
- Staff people: public members of the `step-security` org (`GET /orgs/step-security/public_members`), any committer or profile email at `stepsecurity.io`, any requester or PR author whose StepSecurity PRs land in five or more unrelated owners, and the owners of the denylisted sandboxes above.
- Apply everywhere: denylisted owners never appear as accounts; staff never appear as contacts; adoption commits authored by staff and PRs requested by staff set `provenance = vendor_initiated` and the merger/approver becomes the org-side contact instead.

Acceptance: zero rows for denylisted owners in `accounts.csv`, `repos.csv`, `contacts.csv`; `SUMMARY.md` reports how many repos and PRs the denylist removed.

### P0.4 Replace `_tier` with orthogonal fields and a score

Delete the tier ladder in `export._tier`. It conflates usage depth with account value and with paid-feature signals (`self_hosted` → `power_user`, `policy` → `likely_customer`). Emit instead:

- `provenance` (account-level, best available): `org_standard` > `app_installed` > `secure_repo_self` > `human_pr` > `hand_written` > `secure_repo_vendor` > `inherited` > `unknown`. `org_standard` and `inherited` come from P1.3; until then leave them unset.
- `depth_score` 0–100 from: egress `block` repos and `allowed_endpoints_max`, `policy_store_any`, `disable_sudo_any`, egress set by expression, harden-runner present in credential-bearing workflows, `workflow_coverage_pct`.
- `path_class` per repo from `harden_runner_paths` basenames: `hygiene` (codeql*, scorecard*, dependency-review*, security*), `build` (ci*, build*, test*, lint*), `release` (release*, publish*, deploy*, docker*). Account-level counts of each. In the last run: codeql.yml in 1,549 repos, dependency-review.yml 1,389, scorecard(s).yml 1,903, release.yml 1,097, publish.yml 249. Hygiene-only adoption is what secure-repo produces by default and scores low.
- `breadth` = `repos_with_harden_runner`; `rollout` = five or more repos with `adoption_date` inside any 30-day window.
- `recency` = `latest_push`; add `latest_human_touch` when P1.1 lands.
- `suppress_reason` (nullable, may hold several): `app_installed`, `policy_store`, `staff`, `foundation`, `individual`, `all_reverted`, `demo_like`.
- `score` = fit × depth × recency where fit is 1 for an Organization with no `suppress_reason`, 0 otherwise. Use `score` for `_priority`, for choosing contacts top-N, and for row order in `accounts.csv`.

Acceptance: deterministic score on a fixture set; `README.md` documents each field; the old `tier` column is removed (or kept as `legacy_tier` for one release, then dropped).

### P0.5 Fix false negatives for PR-discovered repos

`workflows.fetch_and_parse` only fetches paths that came from code search. Repos discovered only through PRs get `paths=[]`, so `uses_harden_runner=False` and they land in `bot_pr_only` ("tried it, did not keep it") even when harden-runner is live. Code search also skips repos inactive for about a year and files over 384 KB, so this affects real accounts.

Fix: when a repo has no code hits, fetch every `.yml`/`.yaml` blob in the `.github/workflows` tree that the query already lists (cap 12, prefer names matching `ci|build|release|publish|test|codeql|scorecard|dependency`) and parse them. Set `discovery_sources` accordingly.

Acceptance: a fixture repo with a merged bot PR and harden-runner live on `HEAD` parses as `uses_harden_runner=True`.

### P0.6 Runtime hygiene

- Require a PAT: fail fast with a clear error if the token has the 1,000/hour `GITHUB_TOKEN` budget (check `X-RateLimit-Limit` on the first response). Document `CRAWLER_TOKEN` as required in README and `crawl.yml`.
- Per-phase timing and request counts by bucket into `meta` and `SUMMARY.md`.
- Auto-chain: when a run ends with work remaining, dispatch the next run (`gh workflow run crawl.yml -f phase=all`) so a full crawl completes unattended. Add `actions: write` to the workflow permissions. Guard with a `max_chain` input (default 4) to stop runaway loops.
- Keep `--smoke --limit 40` working end to end, including the new phases.

## P1: should ship. Better contacts and inheritance detection; moderate cost.

### P1.1 Find the adopter with blame, not file creation

`contacts.py` takes the author of the oldest commit on the file, which is the file's creator whenever harden-runner was added to an existing `ci.yml` (README admits this), and `_oldest_commit` pages up to six serial requests per file to get there.

Replace with GraphQL `Commit.blame(path:)` on the harden-runner step's lines (the `- name:`/`uses:` line and the `with:` block), batched per repo: adoption commit = oldest non-bot commit across those lines; `block_configurer` = latest non-bot commit on the `egress-policy` and `allowed-endpoints` lines; `latest_human_touch` = its date. Skip the `uses:` line's own commit when it is a Dependabot or Renovate SHA bump. Fall back to the current method when blame fails. If a merged StepSecurity PR exists, adoption date = its `merged_at`, adopter roles = `bot_pr_requester` and `bot_pr_merger`.

### P1.2 Contact roles, exclusions, still-there

- Roles: keep `top_committer`, `merged_stepsecurity_pr` (rename `bot_pr_merger`), add `bot_pr_requester`, `human_pr_author`, `block_configurer`, `codeowner` (one batched blob fetch of `CODEOWNERS` / `.github/CODEOWNERS`; take owners for `.github/workflows` or `*`), `adopter` (from P1.1). Emit `evidence_url` per role (commit or PR URL).
- Exclusions: extend `BOT_LOGIN_RE` with `^svc-`, `-bot$`, `automation`, `^copilot$` (a `Copilot` author appears in the data), staff (P0.3), and drive-by contributors (no commits in the org outside the adoption PR).
- `still_there`: latest commit in any of the account's repos within 180 days and the latest commit email domain matches the account domain (or the profile `company` matches). When the profile company or newest email domain points to a different company, write the person to `champions_moved.csv` with both accounts.
- Email classes in `domains.py`: `corporate` only when the domain matches the account's inferred domain; otherwise `other_corporate`; add `academic` for `.edu` and `.ac.*`. Keep freemail and ISP lists. Never drop personal emails; put them in `personal_emails` for provider matching.
- Resolve profile `company` values beginning with `@` to a GitHub org and its `websiteUrl`.

### P1.3 Inheritance and org-standard detection

- In `parse_workflow`, add a normalized hash of the harden-runner step block (strip SHA refs, version comments, whitespace, and `allowed-endpoints` values) and of the whole file. In export, cluster by step hash; a cluster spanning ten or more repos across three or more owners marks those repos `inherited_template=true` unless the owner has other evidence (`secure_repo_self`, `human_pr`, block mode). Exact blob-SHA clusters in `state/discovered.json` top out at 17 repos, so normalization is required.
- In `parse_workflow`, record `is_reusable` (`on` contains `workflow_call`) and `calls` (job-level `uses: owner/repo/.github/workflows/*.yml@ref`). When harden-runner sits in a reusable workflow, credit its owner and add a capped discovery query for callers (`"owner/repo/.github/workflows/<name>" path:.github/workflows`). `lfreleng-actions` (92 repos) and `onap` (64) are the test case: onap should resolve as a caller, not 64 decisions.
- Org standard: for each surviving org, one GraphQL fetch of the `.github` repo's `workflow-templates/` and `.github/workflows/` trees; harden-runner in either sets `org_standard=true` and `provenance=org_standard`.
- Foundations list in `crawler/vendor.py`: `apache`, `ruby`, `python`, `kubernetes`, `kubernetes-sigs`, `cncf`, `onap`, `lfreleng-actions`, `lf-*`, `openssf`, `eclipse`, `nodejs`, `opentelemetry`, plus owner description containing `foundation` or `non-profit` → `suppress_reason=foundation`.
- Repo purpose: name or description matching `demo|example|sample|template|starter|boilerplate|tutorial|playground|sandbox|test-` → `demo_like=true`. `aerospike-examples` (72 repos) is the test case.

### P1.4 Live-usage check for top-N accounts only

REST `GET /repos/{o}/{r}/actions/workflows` gives `state` per workflow (`active`, `disabled_inactivity`, `disabled_manually`); `GET .../actions/workflows/{id}/runs?per_page=1` gives the latest run. Two requests per repo, restricted to repos of the top-N accounts. Emit `hr_workflow_state`, `hr_last_run_at`, and account-level `live_repos`. A file on `HEAD` in a disabled workflow is not live usage.

## P2: later, or skip. Slow or marginal for the list.

- Per-line edit history of the harden-runner config beyond blame.
- Verifying that the harden-runner step executed inside recent runs (jobs API per run).
- Full contacts for every repo. Never; top-N only.
- Sharding the workflows phase across parallel jobs. Measure secondary rate limits first; default stays serial.
- Third-party firmographic enrichment (Clay, RocketReach, Similarweb). Downstream of the crawler, keyed on `inferred_domain`, `name`, `company`, `location`.
- Heavy metadata (`languages`, `topics`, `funding`, `latestRelease`, `watchers`) for accounts outside top-N.

## Output schema

`accounts.csv`: add `provenance`, `depth_score`, `score`, `breadth`, `rollout`, `org_standard`, `app_installed`, `secure_repo_self_prs`, `secure_repo_vendor_prs`, `human_prs`, `reverts`, `pr_median_latency_h`, `path_hygiene_repos`, `path_build_repos`, `path_release_repos`, `inherited_repos`, `live_repos`, `latest_human_touch`, `foundation`, `suppress_reason`. Remove `tier` and `tier_reason` (or keep as `legacy_*` for one release).

`repos.csv`: add `pr_classes`, `pr_requesters`, `pr_median_latency_h`, `reverted`, `path_class`, `step_hash`, `inherited_template`, `is_reusable`, `calls`, `hr_workflow_state`, `hr_last_run_at`, `adopter_source` (`blame` | `file_history` | `bot_pr`), `filtered_reason` (for rows dropped by classify).

`contacts.csv`: add `roles` values above, `evidence_urls`, `still_there`, `email_class`, `personal_emails`, `is_staff` (always false in this file; staff are excluded).

New files: `suppressed.csv` (account, suppress_reason, evidence), `champions_moved.csv` (login, name, from_account, to_company_or_domain, evidence), `individuals.csv` (user-owned accounts, unenriched).

## Acceptance checklist for the P0 PR

- [ ] Fresh full run: owners 100%, metadata and workflows 100% of survivors, contacts for top-N, within one budget or one auto-chained pair.
- [ ] `SUMMARY.md` shows funnel counts, per-phase wall time, requests per bucket, denylist removals.
- [ ] No denylisted owner or staff login in any CSV; `stepsecurity-int[bot]` repos absent.
- [ ] `app_installed` accounts in `suppressed.csv`; `revert` PRs set `reverted`.
- [ ] `owner_type` populated for every row in `repos.csv` and `accounts.csv`.
- [ ] Fixture tests for: PR classifier (each class, latency), requester regex (match and no match), path class, PR-only repo with live harden-runner, score determinism.
- [ ] Rerun on the migrated existing `state/` repeats no completed work.
- [ ] README pipeline table, output section, and required-token note updated.

## Verified facts to rely on (from the codebase and `state/` on 2026-09-21)

- `main.py` phase order and alphabetical iteration: lines 105–168.
- `export.py` reads `owner_type` from `repos` only; `discovered.json` hits carry `owner_type` and `fork`.
- `_slim_issue_item` keeps `author` but drops `body`; PR authors and counts as listed under "Why".
- `workflows.fetch_and_parse` fetches only code-search paths; batch 25, cap 12 files.
- `contacts.py` batch 10, 100 recent commits plus two file histories per repo, serial `_oldest_commit` paging.
- `enrich.py` `REPO_FIELDS` includes seven connections per repo; batch 25.
- `domains.infer_account_domain` precedence is already website → email → name-matched committer domain → ≥2 committer emails → homepage → weak single email. Most first-pass domain errors came from `owners` never running for those accounts, not from the precedence.
