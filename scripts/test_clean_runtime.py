"""Run bounded offline contracts against the built native runtime, never live WQ."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--env-file',type=Path)
    args=p.parse_args()
    env=dict(os.environ)
    if args.env_file:
        from dotenv import dotenv_values
        values=dotenv_values(args.env_file)
        env.update({k:v for k,v in values.items() if k.startswith('POSTGRES_') and v is not None})
    env.update(POSTGRES_DB='aiac_bounded_test',BRAIN_EMAIL='',BRAIN_PASSWORD='',OPENAI_API_KEY='offline-test-placeholder',
               CREDENTIALS_SECRET='',OPENAI_BASE_URL='http://127.0.0.1:1',REDIS_URL='redis://127.0.0.1:1/0',
               AIAC_BOUNDED_TEST_SOURCE=str(args.source.resolve()))
    return subprocess.run([sys.executable,'-m','unittest','clean_runtime.test_bounded','clean_runtime.test_database','-v'],env=env).returncode


if __name__=='__main__': raise SystemExit(main())
