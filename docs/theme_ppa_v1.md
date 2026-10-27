# Theme PPA v1 research protocol

This protocol is a bounded research path. It never submits an Alpha. It preserves
the existing strict `quality_status` and records a separate `ppa_evaluation` in
each candidate's audited `result_snapshot`.

## Freeze before task creation

Use the account's current WQ platform theme page to verify the theme ID, start
and end time, region, universe, delay, data categories, allowed operators,
neutralization and other simulation settings, PPA correlation limit, quota, and
Power Pool access. A forum post is only a lead. Record the platform URL and
the time of the account eligibility check. Freeze one or two datasets and their
field IDs. Select them for coverage, update method, and an interpretable
economic mechanism. Each expression must use fields from only its named
dataset. The protocol validates the frozen list before the first WQ request.

The task configuration requires `research_protocol: theme_ppa_v1`,
`research_request_budget: 80`, `continuous_mode: false`,
`dataset_strategy: SPECIFIC`, and `target_datasets` equal to the manifest's
dataset IDs. `ppa_manifest` requires:

- `theme_id`, `verified_source: platform`, `platform_source_url` on
  `platform.worldquantbrain.com`, `starts_at`, `ends_at`, `region`, `universe`,
  `delay`, and `account_eligibility` with `verified_at`,
  `power_pool_access: true`, and at least 80 remaining requests.
- `datasets` mapping 1–2 dataset IDs to allowed field IDs,
  `dataset_categories`, `allowed_operators`, `max_fields`,
  `max_operator_count`, `settings`, `ppa_corr_max`, `turnover_max`, and
  `min_positions`. Freeze `eligible_classification_ids` and
  `required_pre_submit_checks` from the platform's current response.
  `settings` contains fixed `decay`, `neutralization`,
  `truncation`, and `test_period`; it must not override region, universe, or
  delay.
- Exactly 24 `discovery` cases in six `pair_id` groups. Each group has one
  `CONTROL` and three `TREATMENT` cases. `refinement` has at most 40 cases and
  `confirmation` at most 16. Each case has a unique `case_id`, `dataset_id`,
  and `expression`. Later phases also have a `parent_case_id`. Refinement
  cases are released only when the parent has complete metrics and beats its
  paired control on Sharpe and Fitness. Confirmation cases can use listed
  `confirmation_settings` overrides within the platform's theme constraints.

The request ledger reserves slots durably. Results, failures, uncertain
responses, and in-flight reservations all consume the 80-request limit;
duplicate requests are rejected before sending. Never automatically resend
an `UNKNOWN` request. Unused slots are acceptable.

## Candidate meaning

`PPA_READY` requires a WQ Power Pool eligible classification, the exact theme
ID, a passed `MATCHES_THEMES` check for that ID, explicit platform
`canSubmit: true` or every frozen pre-submit check passed, no failed or
pending checks, a Power Pool correlation below the frozen limit,
positive two-year and test/OS Sharpe, and valid turnover and position-count
guards. Any missing platform evidence yields `PPA_PENDING`; explicit
disqualification yields `PPA_REJECTED`. An inferred legacy `can_submit` flag
cannot make a candidate ready. A confirmed ready candidate still needs human
review before any future submission.

Read the task's candidate page and run research report for the request funnel,
phase counts, paired deltas, PPA counts, evidence, and unused budget. If the
run ends with no ready candidate, report the stop reason and the failed stage.
Do not equate WQ HTTP success, simulation count, or `OPTIMIZE` with PPA success.

Deployment requires a separately requested release under `AGENTS.md`. A
release must not start a new theme task automatically.
