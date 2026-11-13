# Isolated PPA selector experiment

This development experiment compares two rankings of the same 80 frozen candidates. Each candidate gets at most one new WQ simulation request. The primary result is the pre-registered top 20 for each policy; top 40 is a sensitivity check. It does not change task 14, production code, production database, or Alpha submission state.

## Before any simulation

1. Pass `python scripts/preflight.py` and Ubuntu and Windows CI on the exact commit used to prepare the manifest. A later code change requires a new manifest.
2. Confirm the current theme is Nov'26 GLB/TOP3000/Delay 1, still open before `2026-11-22T00:00:00Z`, and that this account has Power Pool access and a usable simulation quota. Save the observation in a private JSON file outside the repository. Do not put credentials in that file.
3. Run `prepare` to read the local AIAC API. It must find 80 unique validated candidates and at least eight candidates exclusive to each policy's top 20. The private manifest contains expressions and parent evidence; never commit or paste it.
4. Run `status`. `SOURCE_REQUEST_PENDING` means the production run still has `RESERVED`, `REQUESTED`, or `UNKNOWN` requests. The existing run 28 has two `UNKNOWN` as of 2026-11-13. Reconcile them in the production process at their original locations; this experiment does not control task 14. Once its feedback evidence hash changes, prepare a **new** manifest. `OTHER_WQ_WORK_ACTIVE`, `SOURCE_RUN_CHANGED`, `PLATFORM_UNVERIFIED`, and `THEME_EXPIRED` also prevent sending.

The platform evidence file format is:

```json
{
  "theme_id": "9B3qVP4",
  "verified_at": "2026-11-13T12:00:00Z",
  "source_url": "https://platform.worldquantbrain.com/",
  "power_pool_access": true,
  "quota_mode": "NO_FIXED_LIMIT",
  "quota_source": "account_owner"
}
```

The existing PPA contract accepts either a confirmed balance of at least 80 or the account owner's prior `NO_FIXED_LIMIT` report. The latter is an owner assertion, not a platform API rule; the 80-request experiment cap still applies. Record the current authenticated account check and the preserved owner assertion separately. The check must be less than 24 hours old. If access or theme eligibility cannot be confirmed, stop without sending.

## Independent lab database

Use the repository's evaluation dependency file and a dedicated Python environment. Set `AIAC_EVAL_DB_PASSWORD` to a generated private value in the current shell, then start only `compose.eval.yaml`:

```powershell
docker compose -f compose.eval.yaml up -d db
$env:AIAC_EVAL_LAB = '1'
$env:POSTGRES_SERVER = '127.0.0.1'
$env:POSTGRES_PORT = '55434'
$env:POSTGRES_DB = 'aiac_eval_ppa'
$env:POSTGRES_USER = 'aiac_eval'
$env:POSTGRES_PASSWORD = $env:AIAC_EVAL_DB_PASSWORD
python -m alembic -c backend/alembic.ini upgrade head
```

The lab has its own named Postgres volume and loopback port. It has no API, worker, scheduler, or Redis. Do not point it at the production database. Set `BRAIN_EMAIL` and `BRAIN_PASSWORD` explicitly in the private operator shell only for `run`. The CLI refuses missing lab markers or the production DB name/port. Do not copy a production `.env` file into this worktree.

## Commands

```powershell
python scripts/ppa_eval_experiment.py prepare --output C:\private\ppa-eval\manifest.json
python scripts/ppa_eval_experiment.py status --manifest C:\private\ppa-eval\manifest.json --platform-evidence C:\private\ppa-eval\platform.json
python scripts/ppa_eval_experiment.py run --manifest C:\private\ppa-eval\manifest.json --platform-evidence C:\private\ppa-eval\platform.json --max-steps 16
python scripts/ppa_eval_experiment.py report --manifest C:\private\ppa-eval\manifest.json --output C:\private\ppa-eval\report.json
```

`prepare` and `status` make GET requests only to the loopback production API. `run` rechecks production and platform gates before each serial POST. It uses AIAC's persistent request ledger and single request adapter with POST retry disabled. A common candidate is sent once, regardless of its place in the two policy rankings. A 429 occupies one of the 80 slots and pauses until its recorded cooldown. An uncertain request stays in the lab ledger and is polled only at its original simulation location; no new POST follows until it resolves. If source checks fail, the lab run pauses. Do not remove the private run lock while a process is active.

`report` reads the lab database. The private report includes manifest hash, lab run, candidate IDs, source parent IDs, WQ Alpha IDs, persisted request and quality states, and aggregate comparison. `PASS` is the persisted AIAC quality status, not HTTP success or a Sharpe threshold. The report includes a parent cluster bootstrap interval for count differences; it is descriptive for this one frozen batch.

The lineage report keeps both `platform_ppa_status` and final `ppa_status`. AIAC initially marks an otherwise platform-ready Alpha as `PPA_PENDING` until its separate robustness confirmation is complete. This experiment does not run that confirmation workflow, so its final `PPA_READY` count may remain zero by design. Do not relabel `platform_ppa_status=PPA_READY` as final `PPA_READY`; use the former only as additional diagnostic evidence.

Interpret a comparison only after all 80 requests have terminal states, `UNKNOWN=0`, and at least 76 have `RESULT`. If hard outcomes are both zero, only a positive pre-registered `effective_improvement` difference supports a directional proxy observation. A single batch cannot establish a stable advantage. If the theme expires or eligibility remains unverified, report the experiment as unexecuted.
