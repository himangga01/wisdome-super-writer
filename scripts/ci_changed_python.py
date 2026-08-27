"""Run Ruff on branch-changed Python plus the complete local-content package."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


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


def _commit(reference: str | None) -> str | None:
    if not reference or set(reference) == {"0"}:
        return None
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{reference}^{{commit}}"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _default_branch_commit(default_branch: str | None) -> str | None:
    if not default_branch:
        return None
    return _commit(f"origin/{default_branch}")


def _trusted_base(
    *,
    event: str | None,
    base: str | None,
    before: str | None,
    default_branch: str | None,
    ref_name: str | None,
) -> str:
    event = event or "explicit"
    if event in {"explicit", "pull_request"}:
        resolved = _commit(base)
        if resolved:
            return resolved
        raise RuntimeError("No trustworthy pull-request or explicit base commit is available.")
    if event == "push":
        resolved_before = _commit(before)
        if resolved_before:
            return resolved_before
        if before and set(before) != {"0"}:
            raise RuntimeError("No trustworthy push-before commit is available in this checkout.")
        default_commit = _default_branch_commit(default_branch)
        if default_commit and ref_name != default_branch:
            return default_commit
        if default_commit and default_commit != _commit("HEAD"):
            return default_commit
        if ref_name == default_branch:
            return EMPTY_TREE
        raise RuntimeError("No trustworthy fetched default branch is available for the new branch.")
    if event == "workflow_dispatch":
        default_commit = _default_branch_commit(default_branch)
        if default_commit:
            return default_commit
        raise RuntimeError("No trustworthy fetched default branch is available for dispatch.")
    raise RuntimeError(f"No trustworthy base policy exists for event {event!r}.")


def selected_python_files(
    *,
    event: str | None,
    base: str | None,
    before: str | None,
    default_branch: str | None,
    ref_name: str | None,
) -> tuple[str, ...]:
    trusted = _trusted_base(
        event=event,
        base=base,
        before=before,
        default_branch=default_branch,
        ref_name=ref_name,
    )
    diff_base = (
        trusted
        if trusted == EMPTY_TREE
        else _git("merge-base", "HEAD", trusted).strip()
    )
    if not diff_base:
        raise RuntimeError("No trustworthy merge base could be computed.")
    changed = _git(
        "diff",
        "--name-only",
        "--diff-filter=ACMRT",
        diff_base,
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
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME"))
    parser.add_argument("--base", default=os.environ.get("GITHUB_BASE_SHA"))
    parser.add_argument("--before", default=os.environ.get("GITHUB_EVENT_BEFORE"))
    parser.add_argument("--default-branch", default=os.environ.get("GITHUB_DEFAULT_BRANCH"))
    parser.add_argument("--ref-name", default=os.environ.get("GITHUB_REF_NAME"))
    parser.add_argument("--list", action="store_true")
    arguments = parser.parse_args()
    try:
        selected = selected_python_files(
            event=arguments.event,
            base=arguments.base,
            before=arguments.before,
            default_branch=arguments.default_branch,
            ref_name=arguments.ref_name,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
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
