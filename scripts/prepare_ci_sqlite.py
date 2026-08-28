"""Reserve one new SQLite file so CI cannot reuse mutable local state."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / ".local" / "state" / "db.sqlite3"


def prepare_fresh_sqlite(database: Path) -> None:
    target = Path(database).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(target, flags, 0o600)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    arguments = parser.parse_args()
    try:
        prepare_fresh_sqlite(arguments.database)
    except FileExistsError:
        print("CI SQLite database already exists; refusing mutable state reuse.")
        return 2
    print("Fresh CI SQLite database reserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
