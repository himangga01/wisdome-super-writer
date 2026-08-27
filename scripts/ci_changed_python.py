"""Run Ruff on branch-changed Python plus the complete local-content package."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _git(*arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout


def _usable_base(requested: str | None) -> str:
    candidates = [
        requested,
        f"origin/{os.environ['GITHUB_BASE_REF']}" if os.environ.get("GITHUB_BASE_REF") else None,
        os.environ.get("GITHUB_EVENT_BEFORE"),
        "HEAD^",
        "HEAD",
    ]
    for candidate in candidates:
        if not candidate or set(candidate) == {"0"}:
            continue
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{candidate}^{{commit}}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return candidate
    raise RuntimeError("No usable merge base is available for the changed-file quality gate.")


def selected_python_files(base: str | None) -> tuple[str, ...]:
    merge_base = _git("merge-base", "HEAD", _usable_base(base)).strip()
    changed = _git(
        "diff",
        "--name-only",
        "--diff-filter=ACMR",
        merge_base,
        "HEAD",
        "--",
    ).splitlines()
    local_content = _git(
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        "src/apps/local_content",
    ).splitlines()
    selected = {
        value.replace("\\", "/")
        for value in (*changed, *local_content)
        if value.endswith(".py") and Path(value).is_file()
    }
    return tuple(sorted(selected))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--list", action="store_true")
    arguments = parser.parse_args()
    selected = selected_python_files(arguments.base)
    if arguments.list:
        print("\n".join(selected))
        return 0
    if not selected:
        print("No Python files selected for Ruff.")
        return 0
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", *selected],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
