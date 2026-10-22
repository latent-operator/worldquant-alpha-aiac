# AIAC local baseline

This baseline uses PostgreSQL and Redis in Docker. The API, Vite UI, and Celery
worker run on the host. It does not start Celery Beat or an autonomous mining loop.
The live local instance uses the ignored `compose.local.yaml`, project
`aiac-local`, PostgreSQL port `55433`, Redis port `56579`, API port `8003`, and UI
port `5174`.

## Rebuild on a fresh machine

1. Copy `compose.local.example.yaml` to ignored `compose.local.yaml`, and
   `.env.example` to ignored `.env`. In `.env`, set the same `POSTGRES_PASSWORD`
   used by Compose, `POSTGRES_USER=aiac_local`, `POSTGRES_SERVER=127.0.0.1`,
   `POSTGRES_PORT=55433`, `POSTGRES_DB=alpha_gpt_local`, and
   `REDIS_URL=redis://127.0.0.1:56579/0`. Set one stable, private
   `CREDENTIALS_SECRET` for both API and worker. An existing database with
   encrypted credentials requires its original `CREDENTIALS_SECRET`.
2. Start only the dependencies:

   ```powershell
   docker compose -p aiac-local -f compose.local.yaml up -d db redis
   ```

3. With Python 3.11, create a virtual environment, install `requirements.txt`,
   and apply migrations from the repository root:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe -m alembic -c backend/alembic.ini upgrade head
   ```

4. Run each host service in its own terminal:

   ```powershell
   # Terminal 1, repository root
   .\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8003
   # Terminal 2, repository root
   cd frontend; npm ci; npm run dev -- --host 127.0.0.1 --port 5174
   # Terminal 3, repository root
   .\.venv\Scripts\python.exe -m celery -A backend.celery_app:celery_app worker --loglevel=info --pool=solo --concurrency=1
   ```

   Run the Celery command from the repository root. Its startup can resume
   pending database backed runs. Inspect pending work before starting it on an
   existing database; smoke checks do not need a new worker.

## Existing local instance and validation

The existing PostgreSQL container uses an **anonymous Docker volume**. Keep
its ignored `compose.local.yaml` and its volume mapping. The example file's
named volume is for fresh installations. Do not replace the live Compose file
or run `docker compose down -v` while preserving this database.

Before changing processes, save `git status`, running commands, the Docker
volume ID, `alembic_version`, key table counts, and a `pg_dump -Fc` file.
Validate the dump by restoring into a separate temporary database. Keep the
same ignored `.env` across API and worker restarts.

Offline checks from the repository root:

```powershell
python scripts/check_repo_hygiene.py
$env:POSTGRES_PORT='1'; $env:REDIS_URL='redis://127.0.0.1:1/0'
$env:BRAIN_EMAIL=''; $env:BRAIN_PASSWORD=''; $env:OPENAI_API_KEY=''; $env:CREDENTIALS_SECRET=''
.\.venv\Scripts\python.exe -m pytest backend/tests/unit -q --disable-warnings
cd frontend; npm run lint; npm run build
```

The PR workflow runs those checks and applies all migrations to a fresh
temporary PostgreSQL service. It has no BRAIN or LLM credentials and does not
start the worker. To smoke check the live instance, request
`http://127.0.0.1:8003/health` and `http://127.0.0.1:5174/`, check database
revision `0002_llm_wait_checkpoint`, and compare table counts with the saved
snapshot. Counts may increase while an already running mining task writes;
they must not fall. A busy Windows `--pool=solo` worker may not answer Celery
remote control ping until its task finishes, so inspect its process, Redis
connection, and database progress as well.

## Recovery

Stop only the process being replaced. If an API or UI restart fails, restart it
with the prior command and the preserved `.env`; leave the database containers
and active worker alone. Validate a database dump in a separate database before
considering any live restore. A live database restore requires a separate
maintenance decision because it would overwrite work completed since backup.

The historical public Git record contained `keys.txt`. Review established that
this particular file lists dataset identifiers, not API credentials. The
release gate checks the exact reviewed blob; see `security-baseline.md`.

## Versioned software operations

The legacy commands above describe the existing host setup. The release path
keeps the same PostgreSQL/Redis containers and anonymous volume, then starts
the API, one Celery worker, and a built static UI through
`scripts/manage-local.ps1`. The UI remains on `127.0.0.1:5174`; its `/api`
and SSE requests proxy to `127.0.0.1:8003`. All three services read the same
ignored `.env` without copying it into a Git worktree or image. Release logs,
PID records, database backups, task snapshots, and the release journal live
under `../../runtime/aiac-ops`, outside the repository.

1. Run `.\.venv\Scripts\python.exe scripts/preflight.py` after every code change.
   It runs offline backend and frontend tests, lint, build, and current-tree
   credential hygiene. CI repeats this on Ubuntu and Windows; Ubuntu also
   applies migrations twice to an isolated PostgreSQL service. CI has no WQ or
   LLM credentials and starts no mining worker.
2. The historical `keys.txt` requires no key rotation based on its reviewed
   contents. The local GitHub account can read the public upstream but cannot
   push there. Create a writable personal fork, merge via
   PR with required `offline-ubuntu` and `offline-windows` checks, and record
   the successful **master push** Actions run ID. See `security-baseline.md`.
3. Keep the checked-out worktree clean at that exact SHA. The first release
   also needs `-BootstrapPreviousSource`, pointing at the preserved, built
   pre-operations snapshot. Subsequent releases read the previous version
   from `../../runtime/aiac-ops/release-*.json`.
4. Run `scripts/deploy-local.ps1 -Commit <40-character SHA> -CiRun <run ID>
   -BootstrapPreviousSource <absolute snapshot path>` for the first release;
   omit the bootstrap parameter later. The command refuses dirty code,
   a changed historical `keys.txt` blob, missing CI, a different DB volume, unmanaged
   services, or an unavailable rollback source. It stages code and UI before
   pausing active tasks, waits for an idle worker, verifies a database dump by
   restoring it into a separate database, then starts the new services and
   resumes only the original active run IDs. A failed deployment restores
   previous code when the schema revision is unchanged. It never restores a
   live database automatically.
5. After the first successful deployment, register the periodic local
   supervisor with `scripts/install-autostart.ps1`. It reads
   `../../runtime/aiac-ops/current.json`, starts only that release, and skips
   startup while `maintenance.lock` exists. Inspect status with
   `scripts/manage-local.ps1 -Action Status`, the `/health` and
   `/__ui_health` endpoints, and the release journal. The old loop remains
   paused.

The first deployment must be explicitly checked after the prior manual
runtime: the previous source is the preserved worktree snapshot, not merely
the older clean Git commit. If the desktop blocks local process control, do not
use another command to bypass it; leave the prepared release unstarted and
report the blocker.
