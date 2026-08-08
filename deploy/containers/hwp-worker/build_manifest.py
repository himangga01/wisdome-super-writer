#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
from pathlib import Path
from typing import Iterable


REQUIRED_ROLES = frozenset(
    {
        "converter", "qpdf", "wrapper", "config", "font", "fontconfig", "runtime",
        "library", "license", "lockfile", "build-metadata",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_role_path(value: str) -> tuple[str, Path]:
    try:
        role, raw_path = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("entry must be ROLE=ABSOLUTE_PATH") from exc
    if role not in REQUIRED_ROLES:
        raise argparse.ArgumentTypeError(f"unsupported material role: {role}")
    path = Path(raw_path)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("manifest material path must be absolute")
    return role, path


def _loaded_libraries(binary: Path) -> list[Path]:
    completed = subprocess.run(
        ["ldd", str(binary)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ldd failed for {binary}: {completed.stderr[:200]}")
    libraries: list[Path] = []
    for line in completed.stdout.splitlines():
        match = re.search(r"=>\s+(/\S+)", line)
        if match is None:
            match = re.match(r"\s*(/\S+)", line)
        if match is not None:
            libraries.append(Path(match.group(1)).resolve(strict=True))
    return libraries


def _directory_files(role: str, directory: Path) -> Iterable[tuple[str, Path]]:
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError(f"manifest directory is missing or unsafe: {directory}")
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_file() and not path.is_symlink():
            yield role, path


def build_manifest(
    *,
    entries: Iterable[tuple[str, Path]],
    rhwp_version: str,
    rhwp_commit: str,
    rust_version: str,
) -> dict:
    files: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    roles: set[str] = set()
    for role, path in entries:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RuntimeError(f"manifest material is missing: {path}") from exc
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1:
            raise RuntimeError(f"manifest material is not a non-empty regular file: {path}")
        normalized = path.resolve(strict=True).as_posix()
        if normalized in seen_paths:
            continue
        seen_paths.add(normalized)
        roles.add(role)
        files.append(
            {
                "role": role,
                "path": normalized,
                "sha256": _sha256(path),
                "byte_size": metadata.st_size,
            }
        )
    if not REQUIRED_ROLES.issubset(roles):
        missing = ", ".join(sorted(REQUIRED_ROLES - roles))
        raise RuntimeError(f"manifest is missing material roles: {missing}")
    return {
        "schema_version": "v1",
        "converter": {
            "name": "rhwp",
            "version": rhwp_version,
            "source_commit": rhwp_commit,
            "rust_version": rust_version,
            "cargo_locked": True,
        },
        "files": sorted(files, key=lambda item: str(item["path"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the immutable legacy HWP byte manifest")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rhwp-version", required=True)
    parser.add_argument("--rhwp-commit", required=True)
    parser.add_argument("--rust-version", required=True)
    parser.add_argument("--entry", action="append", default=[], type=_parse_role_path)
    parser.add_argument("--directory", action="append", default=[], type=_parse_role_path)
    parser.add_argument("--loaded-libraries-for", action="append", default=[], type=Path)
    arguments = parser.parse_args()

    entries = list(arguments.entry)
    for role, directory in arguments.directory:
        entries.extend(_directory_files(role, directory))
    for binary in arguments.loaded_libraries_for:
        entries.extend(("library", path) for path in _loaded_libraries(binary))
    manifest = build_manifest(
        entries=entries,
        rhwp_version=arguments.rhwp_version,
        rhwp_commit=arguments.rhwp_commit,
        rust_version=arguments.rust_version,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
