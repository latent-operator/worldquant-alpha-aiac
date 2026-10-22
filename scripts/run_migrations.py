"""Apply committed Alembic migrations with the original private runtime env."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from alembic import command
from alembic.config import Config

from scripts.launch_runtime import load_runtime_env


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    load_runtime_env(args.env_file.resolve())
    os.chdir(source)
    sys.path.insert(0, str(source))
    command.upgrade(Config(str(source / "backend" / "alembic.ini")), "head")


if __name__ == "__main__":
    main()
