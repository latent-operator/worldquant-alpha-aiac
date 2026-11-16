"""Offline PostgreSQL request/recovery checks; never touches the active database."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env-file',type=Path)
    args=p.parse_args(); env=dict(os.environ)
    if args.env_file:
        from dotenv import dotenv_values
        env.update({k:v for k,v in dotenv_values(args.env_file).items() if k.startswith('POSTGRES_') and v is not None})
    env.update(POSTGRES_DB='aiac_bounded_test',BRAIN_EMAIL='',BRAIN_PASSWORD='',OPENAI_API_KEY='offline-test-placeholder',
               CREDENTIALS_SECRET='',OPENAI_BASE_URL='http://127.0.0.1:1/v1',REDIS_URL='redis://127.0.0.1:1/0',
               AIAC_BOUNDED_TEST_SOURCE=str(Path(__file__).resolve().parents[1]))
    raise SystemExit(subprocess.run([sys.executable,'-m','unittest','discover','-s','backend/tests/bounded','-v'],env=env).returncode)
