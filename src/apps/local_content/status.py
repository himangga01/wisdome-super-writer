"""Read-only local dependency and workflow-lock observations."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from pathlib import Path

import httpx

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_EXPECTED_HUMANIZER_ENDPOINT = "http://127.0.0.1:3210"
_MAX_GUARD_BYTES = 4096
_REPARSE_POINT = 0x400


def humanizer_status(base_url: str) -> dict[str, str]:
    """Report the required loopback humanizer without selecting a fallback."""

    if base_url != _EXPECTED_HUMANIZER_ENDPOINT:
        return {"status": "misconfigured", "endpoint": _EXPECTED_HUMANIZER_ENDPOINT}
    try:
        response = httpx.get(f"{base_url}/api/health", timeout=0.5)
        payload = response.json()
        ready = (
            response.status_code == 200
            and isinstance(payload, dict)
            and payload.get("status") == "ready"
        )
    except (httpx.HTTPError, ValueError, TypeError):
        ready = False
    return {
        "status": "ready" if ready else "unavailable",
        "endpoint": _EXPECTED_HUMANIZER_ENDPOINT,
    }


def local_runtime_status(
    *,
    humanizer_base_url: str,
    output_root: Path,
    state_root: Path,
) -> dict[str, dict[str, object]]:
    return {
        "humanizer": humanizer_status(humanizer_base_url),
        "workflow": workflow_lock_status(
            output_root=output_root,
            state_root=state_root,
        ),
    }


def workflow_lock_status(*, output_root: Path, state_root: Path) -> dict[str, object]:
    """Distinguish an actively held guard from its intentionally persistent file."""

    canonical_output = os.path.normcase(os.path.realpath(os.path.abspath(output_root)))
    guard_name = hashlib.sha256(canonical_output.encode("utf-8")).hexdigest() + ".guard"
    guard = Path(os.path.abspath(state_root)) / "locks" / guard_name
    try:
        metadata = os.lstat(guard)
    except FileNotFoundError:
        return {"status": "idle"}
    except OSError:
        return {"status": "unavailable"}
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)
    ):
        return {"status": "unavailable"}
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(guard, flags)
    except OSError:
        return {"status": "unavailable"}
    locked = False
    active = False
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            return {"status": "unavailable"}
        try:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            if exc.errno not in _contention_codes():
                return {"status": "unavailable"}
            active = True
        if not active:
            return {"status": "idle"}
        return _active_guard_metadata(descriptor)
    except OSError:
        return {"status": "unavailable"}
    finally:
        if locked:
            try:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(descriptor)
        except OSError:
            pass


def _active_guard_metadata(descriptor: int) -> dict[str, object]:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = os.read(descriptor, _MAX_GUARD_BYTES + 1)
        if not payload or len(payload) > _MAX_GUARD_BYTES:
            raise ValueError
        value = json.loads(payload.decode("utf-8", errors="strict"))
        if (
            not isinstance(value, dict)
            or set(value) != {"pid", "started_at", "workflow_id"}
            or type(value.get("pid")) is not int
            or value["pid"] < 1
            or not isinstance(value.get("started_at"), str)
            or not isinstance(value.get("workflow_id"), str)
            or not value["workflow_id"]
        ):
            raise ValueError
    except (OSError, UnicodeError, ValueError):
        return {"status": "active"}
    return {
        "status": "active",
        "workflowId": value["workflow_id"],
        "pid": value["pid"],
        "startedAt": value["started_at"],
    }


def _contention_codes() -> set[int]:
    values = {errno.EACCES, errno.EAGAIN}
    if hasattr(errno, "EDEADLK"):
        values.add(errno.EDEADLK)
    return values
