# Crawl summary (2026-09-21 15:03 UTC)

- Repos discovered: **8256** (code search) + **5691** (bot PRs); exported **6061**
- Denylist removed: 730 repos, 2561 PRs; staff logins known: 13
- Accounts: **1503** (Organization: 1503)
- Accounts with an inferred domain: 1162 (77%)
- Accounts with at least one corporate committer email: 329 (21%)
- People rows: 3621; with any email: 2808; with corporate email: 1833

## Funnel

| stage | count |
|---|---|
| candidates | 11344 |
| internal_prs_dropped | 0 |
| after_classify | 5352 |
| after_metadata | 5211 |
| scored_accounts | 1503 |
| scored_accounts_positive | 1039 |
| contacts_top_accounts | 400 |

Filtered by reason: archived=198, denylist=725, fork=584, individual=4553, template=73

## Provenance

| provenance | accounts |
|---|---|
| hand_written | 701 |
| secure_repo_self | 267 |
| secure_repo_third_party | 241 |
| unknown | 131 |
| app_installed | 111 |
| secure_repo_vendor | 38 |
| human_pr | 10 |
| secure_repo_unknown | 4 |

Suppressed: {'app_installed': 111, 'policy_store': 8, 'all_reverted': 3}; score > 0: 1039; rollout accounts: 66

## Tiers (legacy)

| tier | accounts |
|---|---|
| adopter_audit | 852 |
| bot_pr_only | 273 |
| power_user | 210 |
| other_actions_only | 109 |
| adopter_block | 51 |
| likely_customer | 8 |

## Phases (cumulative across runs)

| phase | wall (min) | last run (min) | requests by bucket | rows |
|---|---|---|---|---|
| discover | 6.2 | 6.2 | search=140 | 13947 |
| owners | 0.0 | 0.0 | graphql=1 | 4 |
| classify | 0.0 | 0.0 | - | 5992 |
| repos | 2.7 | 2.7 | core=545, graphql=20 | 183 |
| workflows | 5.9 | 5.9 | graphql=74 | 912 |
| score | 0.0 | 0.0 | - | 1503 |
| contacts | 23.8 | 23.8 | graphql=528 | 2262 |

Requests this run by bucket: {'core': 547, 'search': 140, 'graphql': 623}; rate limits seen: {'core': 5000, 'search': 30, 'graphql': 5000, 'integration_manifest': 5000, 'source_import': 100, 'code_scanning_autofix': 10, 'actions_runner_registration': 10000, 'scim': 15000, 'dependency_snapshots': 100, 'dependency_sbom': 100, 'audit_log': 1750, 'audit_log_streaming': 15, 'code_search': 10, 'copilot_usage_records': 1750, 'enterprise_token_inventory': 1750}
Remaining work: none

## Phase state

- discover_code_done: True
- discovered_repos: 8256
- discover_prs_done: True
- pr_repos: 5691
- workflows_done: 5211
- enrich_done: 11189
- contacts_done: 2281
- exported_at: 2026-09-21T15:03:54Z
- requests_this_run: 1310
- schema: 2
- rate_limits: {'core': 5000, 'search': 30, 'graphql': 5000, 'integration_manifest': 5000, 'source_import': 100, 'code_scanning_autofix': 10, 'actions_runner_registration': 10000, 'scim': 15000, 'dependency_snapshots': 100, 'dependency_sbom': 100, 'audit_log': 1750, 'audit_log_streaming': 15, 'code_search': 10, 'copilot_usage_records': 1750, 'enterprise_token_inventory': 1750}
- workflows_reset_for_tree_scan: 2456
- run_started_at: 2026-09-21T14:25:17Z
- staff_logins: 13
- owners_done: 3755
- owners_total: 3755
- pr_bodies_backfilled: 649
- repos_done: 5211
- repos_total: 5211
- workflows_total: 5211
- contacts_total: 2281
- denylist_repos_removed: 730
- denylist_prs_removed: 2561
- requests_by_bucket_this_run: {'core': 547, 'search': 140, 'graphql': 623}
