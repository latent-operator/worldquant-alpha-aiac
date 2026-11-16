# Local tool contract

The production API is resolved from `G:\安装\claude\runtime\aiac-clean\current.json`. The known installation uses API port 8003 and UI port 5174; verify rather than assume.

Resolve the helper exclusively as `<current.json.recipe_source>/scripts/aiac_research.py`; the generated application source does not contain this helper. Use `current.json.python_executable` as the interpreter. For the older deployment pointer without that property, use `<current.json.source>/.venv/Scripts/python.exe` (the original installation's environment). Inspect the resolved helper's `--help` before first use. If it is absent or its contract differs, report the mismatch rather than guessing an API or using legacy custom research endpoints.

Implemented command interface (shown relative to recipe_source; substitute the resolved absolute helper and interpreter paths when invoking):

```text
python scripts/aiac_research.py --url http://127.0.0.1:8003 health
python scripts/aiac_research.py --url http://127.0.0.1:8003 create --manifest PRIVATE_JSON_PATH
python scripts/aiac_research.py --url http://127.0.0.1:8003 start --task-id TASK_ID
python scripts/aiac_research.py --url http://127.0.0.1:8003 report --run-id RUN_ID
python scripts/aiac_research.py --url http://127.0.0.1:8003 resume --task-id TASK_ID
python scripts/aiac_research.py --url http://127.0.0.1:8003 pause --task-id TASK_ID
python scripts/aiac_research.py --url http://127.0.0.1:8003 stop --task-id TASK_ID
```

Resume retains the same fresh eligible run, under current user instruction. Only NOT_REQUESTED/ACCEPTED/RESULT positions are eligible; REQUESTED/UNKNOWN/REJECTED block recovery. It is not a way to replay legacy requests. Start creates execution; reading health/report does not. Pause/stop require the owner's action request or an applicable existing execution stop condition.

## Frozen-case manifest

The manifest is the original task-create JSON body, not a separate orchestration format:

```json
{
  "name": "Research hypothesis and frozen contrasts",
  "region": "GLB",
  "universe": "TOP3000",
  "dataset_strategy": "SPECIFIC",
  "target_datasets": ["VERIFIED_DATASET_ID"],
  "agent_mode": "AUTONOMOUS",
  "max_iterations": 1,
  "daily_goal": 1,
  "config": {
    "execution_mode": "bounded",
    "simulation_mode": "single",
    "hypothesis": "Economic mechanism and expected direction",
    "fields": ["VERIFIED_FIELD_ID"],
    "max_create_requests": 4,
    "neutralization": "SUBINDUSTRY",
    "truncation": 0.08,
    "candidates": [{"id": "A", "expression": "VALIDATED_EXPRESSION", "decay": 4}]
  }
}
```

Uppercase placeholders must be replaced with verified, validated research inputs. This example is not executable evidence. The caller verifies live field metadata for the selected dataset, region, delay and universe, and retains that evidence. The backend validates the declared one or two field IDs and expression syntax against that declaration; it does not independently verify the actual field catalog. Supply all frozen candidates before create. First release supports only single simulation, serially; no automatic candidate expansion. The backend allows 1..16 candidates within max_create_requests. Use the agreed research budget, not the maximum merely because it is allowed.

Health reports worker_online, worker_state, workers, queue_length, historical_exclusions and simulation_mode. It combines Celery ping with a running worker heartbeat no older than 120 seconds: worker_state is idle_or_ready, busy_recent_heartbeat or offline_or_unknown. A missed ping alone does not prove the worker is offline. If worker_online is false, report execution readiness as unverified and inspect the persisted state rather than bypassing the worker. Confirm historical exclusions are loaded.

The report provides observed_at, recipe_commit, requests by state, quality_PASS, PPA_READY, cases and alphas. Each Alpha provides metrics (including raw_wq with test-period values when returned, failures and missing evidence), is_metrics, settings and checks. Preserve missing values. PPA_READY is currently 0/unproven; do not infer it from completed execution.

Manifest schema, hard limits and state transitions belong to the backend/CLI. Never treat a prompt or unrecognized config key as an enforced budget. Preserve the exact accepted manifest and receipt. Report reading must not alter task state.

Deployment and fixes use the existing exact-commit CI/audited CD procedure. This Skill does not release software or enable an unverified worker.
