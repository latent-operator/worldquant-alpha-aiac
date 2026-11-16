"""Migrate a restored legacy archive into an empty, pristine upstream database.

No task, run, session, queue or request ledger enters the active database.
The full restored archives remain authoritative for historical lineage.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

UPSTREAM_COMMIT = "8ffda431d08703b7f6ae13c8287806fd76eb63e0"
TABLES = frozenset({
    "alphas", "alpha_failures", "alpha_pnl", "system_configs", "wqb_credentials",
    "llm_providers", "mcp_servers", "mcp_tools", "datasets", "datafields", "operators",
    "operator_blacklist", "operator_prefs", "regions", "universes", "neutralizations",
    "pyramid_multipliers", "templates", "template_variables", "knowledge_entries",
    "rl_states", "rl_actions",
})
LEGACY_REFS = {"task_id", "run_id", "trace_step_id"}


def git_output(source: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()


def validate_source(source: Path) -> None:
    if git_output(source, "rev-parse", "HEAD") != UPSTREAM_COMMIT:
        raise ValueError("Unexpected upstream commit")
    if git_output(source, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Modified upstream source")


def project_row(table: str, source: dict, columns: set[str]) -> tuple[dict, dict | None]:
    if table not in TABLES:
        raise ValueError(f"{table}: archive only")
    row = {key: value for key, value in source.items() if key in columns}
    refs = {key: source[key] for key in sorted(LEGACY_REFS)
            if key in source and source[key] is not None}
    for key in LEGACY_REFS & row.keys():
        row[key] = None
    lineage = {"table": table, "id": source["id"], **refs} if refs else None
    return row, lineage


async def migrate(report_path: Path) -> None:
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.engine import make_url
    from backend.database import SQLAlchemyBase, engine
    import backend.models  # noqa: F401
    from backend.config import settings

    target_url = make_url(settings.SQLALCHEMY_DATABASE_URI)
    if target_url.database != "aiac_clean":
        raise ValueError("Destination must be the isolated aiac_clean database")
    archive = create_async_engine(target_url.set(database="archive_production"))
    counts, lineage = {}, []
    try:
        async with engine.begin() as target:
            await target.run_sync(SQLAlchemyBase.metadata.create_all)
            for table in SQLAlchemyBase.metadata.sorted_tables:
                if (await target.scalar(select(__import__('sqlalchemy').func.count()).select_from(table))):
                    raise ValueError("Destination is not empty")
            async with archive.connect() as source:
                await source.execute(text("SET TRANSACTION READ ONLY"))
                for table in SQLAlchemyBase.metadata.sorted_tables:
                    if table.name not in TABLES:
                        continue
                    order = ', '.join(f'"{col.name}"' for col in table.primary_key.columns)
                    rows = (await source.execute(text(f'SELECT * FROM "{table.name}" ORDER BY {order}'))).mappings().all()
                    converted = []
                    for original in rows:
                        row, refs = project_row(table.name, dict(original), set(table.columns.keys()))
                        converted.append(row)
                        if refs:
                            lineage.append(refs)
                    for offset in range(0, len(converted), 100):
                        await target.execute(table.insert(), converted[offset:offset + 100])
                    counts[table.name] = len(converted)
                    if "id" in table.columns and converted:
                        await target.execute(text(
                            f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                            f"(SELECT max(id) FROM \"{table.name}\"), true)"))
                    stored = (await target.execute(select(table).order_by(*table.primary_key.columns))).mappings().all()
                    if len(stored) != len(converted):
                        raise ValueError("Migration count mismatch")
                    for expected, actual in zip(converted, stored):
                        if any(actual[key] != value for key, value in expected.items()):
                            raise ValueError("Migration value mismatch")
                await source.rollback()
            # Write the private lineage before committing; errors roll back the import.
            report_path.write_text(json.dumps({"upstream_commit": UPSTREAM_COMMIT,
                "counts": counts, "legacy_lineage": lineage,
                "archive": "archive_production", "tasks_imported": 0,
                "research_hold": True}, default=str, indent=2), encoding="utf-8")
    finally:
        await archive.dispose()
        await engine.dispose()
    print(json.dumps({"counts": counts, "lineage_rows": len(lineage), "tasks_imported": 0}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    validate_source(args.source)
    from dotenv import load_dotenv
    load_dotenv(args.env_file, override=True)
    os.chdir(args.source)
    sys.path.insert(0, str(args.source.resolve()))
    asyncio.run(migrate(args.report))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # DB exceptions can contain bound credential values; do not print them.
        print(f"Clean migration failed ({type(exc).__name__}); transaction rolled back.", file=sys.stderr)
        raise SystemExit(1) from None
