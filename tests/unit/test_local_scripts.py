from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import warnings
import zipfile
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="local PowerShell lifecycle scripts intentionally require Windows",
)


def _powershell(
    *arguments: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", *arguments],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _script_fixture(tmp_path: Path, *, environment: str = "development") -> tuple[Path, Path]:
    repository = tmp_path / "writer"
    humanizer = tmp_path / "ai-text-makes-likes-human"
    (repository / "src").mkdir(parents=True)
    (repository / ".venv" / "Scripts").mkdir(parents=True)
    humanizer.mkdir()
    for relative in ("pyproject.toml", "uv.lock", "src/manage.py"):
        (repository / relative).touch()
    (repository / ".env.local.example").write_text(
        "WISDOME_ENVIRONMENT=development\nWISDOME_RUNTIME_MODE=local\n",
        encoding="utf-8",
    )
    (repository / ".env.local").write_text(
        f"WISDOME_ENVIRONMENT={environment}\nWISDOME_RUNTIME_MODE=local\n"
        "DATA_GO_KR_SERVICE_KEY=do-not-print-this-secret\n",
        encoding="utf-8",
    )
    (repository / ".venv" / "Scripts" / "python.exe").touch()
    (humanizer / "package.json").write_text(
        json.dumps({"scripts": {"build": "tsc", "start": "node dist/server.js"}}),
        encoding="utf-8",
    )
    return repository, humanizer


def _write_uv_archive(path: Path, entries: list[tuple[str, bytes]]) -> bytes:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, payload in entries:
                archive.writestr(name, payload)
    return path.read_bytes()


def _write_uv_lock(
    path: Path,
    archive: Path,
    *,
    executable_sha256: str,
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "uv": {
                    "version": "0.12.7",
                    "windows_x64": {
                        "url": "https://github.com/astral-sh/uv/releases/download/0.12.7/uv-x86_64-pc-windows-msvc.zip",
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "executable_sha256": executable_sha256,
                    },
                },
                "python": {"version": "3.12"},
            }
        ),
        encoding="utf-8",
    )


def _provision_uv(
    repository: Path,
    humanizer: Path,
    archive: Path,
    lock: Path,
) -> subprocess.CompletedProcess[str]:
    return _powershell(
        "-File",
        str(REPOSITORY_ROOT / "scripts" / "setup-local.ps1"),
        "-RepositoryRoot",
        str(repository),
        "-HumanizerRoot",
        str(humanizer),
        "-ToolchainLockPath",
        str(lock),
        "-UvArchivePath",
        str(archive),
        "-ProvisionUvOnly",
    )


@WINDOWS_ONLY
def test_powershell_scripts_parse_and_contain_no_container_cli_invocation() -> None:
    files = (
        REPOSITORY_ROOT / "scripts" / "setup-local.ps1",
        REPOSITORY_ROOT / "scripts" / "start-local.ps1",
        REPOSITORY_ROOT / "scripts" / "local-toolchain.psm1",
        REPOSITORY_ROOT / "scripts" / "local-process-guard.psm1",
    )
    for script in files:
        result = _powershell(
            "-Command",
            f"[void][scriptblock]::Create((Get-Content -Raw -LiteralPath '{script}'))",
        )
        assert result.returncode == 0, result.stderr

    material = "\n".join(
        path.read_text("utf-8")
        for path in (*files, REPOSITORY_ROOT / ".github" / "workflows" / "quality.yml")
    )
    forbidden = "dock" + "er"
    assert forbidden not in material.casefold()


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("pwsh") is None,
    reason="requires portable PowerShell on a non-Windows host",
)
def test_start_script_explicitly_refuses_non_windows(tmp_path: Path) -> None:
    repository, humanizer = _script_fixture(tmp_path)

    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(REPOSITORY_ROOT / "scripts" / "start-local.ps1"),
            "-RepositoryRoot",
            str(repository),
            "-HumanizerRoot",
            str(humanizer),
            "-ValidateOnly",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "windows" in (result.stdout + result.stderr).casefold()


@WINDOWS_ONLY
def test_setup_preflight_validates_pinned_archive_checksum_without_side_effects(
    tmp_path: Path,
) -> None:
    repository, humanizer = _script_fixture(tmp_path)
    archive = tmp_path / "uv.zip"
    archive.write_bytes(b"not-the-pinned-archive")
    lock = tmp_path / "toolchain-lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "uv": {
                    "version": "0.12.7",
                    "windows_x64": {
                        "url": "https://github.com/astral-sh/uv/releases/download/0.12.7/uv-x86_64-pc-windows-msvc.zip",
                        "sha256": "0" * 64,
                        "executable_sha256": "0" * 64,
                    },
                },
                "python": {"version": "3.12"},
            }
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["DATA_GO_KR_SERVICE_KEY"] = "do-not-print-this-secret"

    result = _powershell(
        "-File",
        str(REPOSITORY_ROOT / "scripts" / "setup-local.ps1"),
        "-RepositoryRoot",
        str(repository),
        "-HumanizerRoot",
        str(humanizer),
        "-ToolchainLockPath",
        str(lock),
        "-UvArchivePath",
        str(archive),
        "-ValidateOnly",
        env=env,
    )

    assert result.returncode != 0
    assert "checksum" in (result.stdout + result.stderr).casefold()
    assert "do-not-print-this-secret" not in result.stdout + result.stderr
    assert hashlib.sha256(archive.read_bytes()).hexdigest() != "0" * 64
    assert not (repository / ".tools").exists()


@WINDOWS_ONLY
def test_setup_preflight_resolves_default_lock_from_script_directory(tmp_path: Path) -> None:
    repository, humanizer = _script_fixture(tmp_path)

    result = _powershell(
        "-File",
        str(REPOSITORY_ROOT / "scripts" / "setup-local.ps1"),
        "-RepositoryRoot",
        str(repository),
        "-HumanizerRoot",
        str(humanizer),
        "-ValidateOnly",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert '"uv":"0.12.7"' in result.stdout
    assert not (repository / ".tools").exists()


@WINDOWS_ONLY
def test_setup_provisions_only_closed_checksum_pinned_uv_archive(tmp_path: Path) -> None:
    repository, humanizer = _script_fixture(tmp_path)
    archive = tmp_path / "uv.zip"
    uv_payload = b"pinned uv executable"
    _write_uv_archive(
        archive,
        [("uv.exe", uv_payload), ("uvw.exe", b"uvw"), ("uvx.exe", b"uvx")],
    )
    lock = tmp_path / "toolchain-lock.json"
    _write_uv_lock(lock, archive, executable_sha256=hashlib.sha256(uv_payload).hexdigest())

    result = _provision_uv(repository, humanizer, archive, lock)

    assert result.returncode == 0, result.stdout + result.stderr
    published = repository / ".tools" / "uv" / "0.12.7"
    assert (published / "uv.exe").read_bytes() == uv_payload
    assert {path.name for path in published.iterdir()} == {"uv.exe", "uvw.exe", "uvx.exe"}
    assert not list((repository / ".tools").glob(".setup-*"))


@WINDOWS_ONLY
@pytest.mark.parametrize(
    "invalid_entries",
    [
        [("../outside.exe", b"bad")],
        [("uv.exe", b"uv"), ("UV.EXE", b"duplicate")],
        [("notes.txt", b"extra")],
        [("con", b"device")],
        [("uv\x01.exe", b"control")],
    ],
    ids=("traversal", "case-duplicate", "extra", "device", "control"),
)
def test_setup_rejects_non_closed_or_noncanonical_uv_archive(
    tmp_path: Path,
    invalid_entries: list[tuple[str, bytes]],
) -> None:
    repository, humanizer = _script_fixture(tmp_path)
    archive = tmp_path / "uv.zip"
    entries = [("uv.exe", b"uv"), ("uvw.exe", b"uvw"), ("uvx.exe", b"uvx")]
    entries.extend(invalid_entries)
    _write_uv_archive(archive, entries)
    lock = tmp_path / "toolchain-lock.json"
    _write_uv_lock(lock, archive, executable_sha256=hashlib.sha256(b"uv").hexdigest())

    result = _provision_uv(repository, humanizer, archive, lock)

    assert result.returncode != 0
    assert "archive" in (result.stdout + result.stderr).casefold()
    assert not (repository / ".tools" / "uv" / "0.12.7").exists()
    assert not (repository / "outside.exe").exists()


@WINDOWS_ONLY
def test_setup_rejects_extracted_uv_executable_hash_mismatch(tmp_path: Path) -> None:
    repository, humanizer = _script_fixture(tmp_path)
    archive = tmp_path / "uv.zip"
    _write_uv_archive(
        archive,
        [("uv.exe", b"uv"), ("uvw.exe", b"uvw"), ("uvx.exe", b"uvx")],
    )
    lock = tmp_path / "toolchain-lock.json"
    _write_uv_lock(lock, archive, executable_sha256="0" * 64)

    result = _provision_uv(repository, humanizer, archive, lock)

    assert result.returncode != 0
    assert "executable" in (result.stdout + result.stderr).casefold()
    assert not (repository / ".tools" / "uv" / "0.12.7").exists()


@WINDOWS_ONLY
def test_setup_rejects_reparse_tool_root_before_archive_write(tmp_path: Path) -> None:
    repository, humanizer = _script_fixture(tmp_path)
    outside = tmp_path / "outside-tools"
    outside.mkdir()
    junction = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(repository / ".tools"), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if junction.returncode != 0:
        pytest.skip("junction creation is unavailable")
    archive = tmp_path / "uv.zip"
    _write_uv_archive(
        archive,
        [("uv.exe", b"uv"), ("uvw.exe", b"uvw"), ("uvx.exe", b"uvx")],
    )
    lock = tmp_path / "toolchain-lock.json"
    _write_uv_lock(lock, archive, executable_sha256=hashlib.sha256(b"uv").hexdigest())

    result = _provision_uv(repository, humanizer, archive, lock)

    assert result.returncode != 0
    assert "reparse" in (result.stdout + result.stderr).casefold()
    assert not (outside / "uv").exists()


@WINDOWS_ONLY
def test_setup_initializes_env_atomically_and_preserves_existing_file(tmp_path: Path) -> None:
    repository, humanizer = _script_fixture(tmp_path)
    env_path = repository / ".env.local"
    env_path.unlink()
    arguments = (
        "-File",
        str(REPOSITORY_ROOT / "scripts" / "setup-local.ps1"),
        "-RepositoryRoot",
        str(repository),
        "-HumanizerRoot",
        str(humanizer),
        "-InitializeEnvironmentOnly",
    )

    created = _powershell(*arguments)
    assert created.returncode == 0, created.stdout + created.stderr
    assert env_path.read_text("utf-8") == (repository / ".env.local.example").read_text("utf-8")
    env_path.write_text("SENTINEL=preserve\n", encoding="utf-8")

    preserved = _powershell(*arguments)

    assert preserved.returncode == 0, preserved.stdout + preserved.stderr
    assert env_path.read_text("utf-8") == "SENTINEL=preserve\n"


@WINDOWS_ONLY
@pytest.mark.parametrize("phase", ["write", "flush", "close"])
def test_setup_env_post_create_fault_fails_and_removes_only_owned_partial(
    tmp_path: Path,
    phase: str,
) -> None:
    repository, humanizer = _script_fixture(tmp_path)
    env_path = repository / ".env.local"
    env_path.unlink()
    arguments = (
        "-File",
        str(REPOSITORY_ROOT / "scripts" / "setup-local.ps1"),
        "-RepositoryRoot",
        str(repository),
        "-HumanizerRoot",
        str(humanizer),
        "-InitializeEnvironmentOnly",
        "-EnvironmentFaultPhase",
        phase,
    )

    failed = _powershell(*arguments)

    assert failed.returncode != 0
    assert phase in (failed.stdout + failed.stderr).casefold()
    assert not env_path.exists()
    env_path.write_text("SENTINEL=preexisting\n", encoding="utf-8")

    collision = _powershell(*arguments)

    assert collision.returncode == 0, collision.stdout + collision.stderr
    assert env_path.read_text("utf-8") == "SENTINEL=preexisting\n"


@WINDOWS_ONLY
def test_verified_uv_handle_denies_concurrent_write_until_exact_process_exits(
    tmp_path: Path,
) -> None:
    repository, _humanizer = _script_fixture(tmp_path)
    executable_root = repository / ".tools" / "verified"
    executable_root.mkdir(parents=True)
    executable = executable_root / "uv.exe"
    shutil.copy2(os.environ["COMSPEC"], executable)
    expected = hashlib.sha256(executable.read_bytes()).hexdigest()
    module = REPOSITORY_ROOT / "scripts" / "local-toolchain.psm1"
    probe = tmp_path / "verified-uv.ps1"
    probe.write_text(
        f"Import-Module '{module}' -Force\n"
        f"$result=Invoke-VerifiedUv -RepositoryRoot '{repository}' -Path '{executable}' "
        f"-ExpectedSha256 '{expected}' -Arguments @('/d','/c','ping 127.0.0.1 -n 3 > nul') "
        f"-WorkingDirectory '{repository}'\n"
        "$result | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
        cwd=REPOSITORY_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.4)
    assert process.poll() is None

    with pytest.raises(PermissionError):
        executable.write_bytes(b"replacement")

    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stdout + stderr
    payload = json.loads(stdout.strip().splitlines()[-1])
    assert payload["ExitCode"] == 0
    assert hashlib.sha256(executable.read_bytes()).hexdigest() == expected


@WINDOWS_ONLY
def test_setup_rejects_archive_larger_than_compressed_bound(tmp_path: Path) -> None:
    repository, humanizer = _script_fixture(tmp_path)
    archive = tmp_path / "oversized.zip"
    _write_uv_archive(
        archive,
        [("uv.exe", b"uv"), ("uvw.exe", b"uvw"), ("uvx.exe", b"uvx")],
    )
    with archive.open("ab") as stream:
        stream.truncate(33 * 1024 * 1024)
    lock = tmp_path / "toolchain-lock.json"
    _write_uv_lock(lock, archive, executable_sha256=hashlib.sha256(b"uv").hexdigest())

    result = _provision_uv(repository, humanizer, archive, lock)

    assert result.returncode != 0
    assert "compressed archive" in (result.stdout + result.stderr).casefold()


@WINDOWS_ONLY
def test_setup_rejects_entry_with_oversized_expanded_metadata(tmp_path: Path) -> None:
    repository, humanizer = _script_fixture(tmp_path)
    archive = tmp_path / "expanded.zip"
    oversized = b"0" * (49 * 1024 * 1024)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("uv.exe", oversized)
        bundle.writestr("uvw.exe", b"uvw")
        bundle.writestr("uvx.exe", b"uvx")
    lock = tmp_path / "toolchain-lock.json"
    _write_uv_lock(lock, archive, executable_sha256=hashlib.sha256(oversized).hexdigest())

    result = _provision_uv(repository, humanizer, archive, lock)

    assert result.returncode != 0
    assert "expanded" in (result.stdout + result.stderr).casefold()


@WINDOWS_ONLY
def test_bounded_stream_copy_fails_when_source_exceeds_counter_limit(tmp_path: Path) -> None:
    module = REPOSITORY_ROOT / "scripts" / "local-toolchain.psm1"
    result = _powershell(
        "-Command",
        f"Import-Module '{module}' -Force; "
        "$source=[IO.MemoryStream]::new([byte[]](0..255) * 8); "
        "$target=[IO.MemoryStream]::new(); "
        "Copy-BoundedStream -Source $source -Destination $target -MaximumBytes 1024",
    )

    assert result.returncode != 0
    assert "limit" in (result.stdout + result.stderr).casefold()


@WINDOWS_ONLY
def test_start_preflight_rejects_reparse_virtual_environment(tmp_path: Path) -> None:
    repository, humanizer = _script_fixture(tmp_path)
    shutil.rmtree(repository / ".venv")
    outside = tmp_path / "outside-venv"
    (outside / "Scripts").mkdir(parents=True)
    (outside / "Scripts" / "python.exe").touch()
    junction = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(repository / ".venv"), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if junction.returncode != 0:
        pytest.skip("junction creation is unavailable")

    result = _powershell(
        "-File",
        str(REPOSITORY_ROOT / "scripts" / "start-local.ps1"),
        "-RepositoryRoot",
        str(repository),
        "-HumanizerRoot",
        str(humanizer),
        "-ValidateOnly",
    )

    assert result.returncode != 0
    assert "reparse" in (result.stdout + result.stderr).casefold()


@WINDOWS_ONLY
def test_job_object_closure_kills_direct_process_and_descendant(tmp_path: Path) -> None:
    repository, _humanizer = _script_fixture(tmp_path)
    child_pid_path = tmp_path / "child.pid"
    root_script = tmp_path / "root.ps1"
    root_script.write_text(
        "$child = Start-Process powershell.exe "
        "-ArgumentList @('-NoProfile','-Command','Start-Sleep -Seconds 60') "
        "-WindowStyle Hidden -PassThru\n"
        f"Set-Content -LiteralPath '{child_pid_path}' -Value $child.Id\n"
        "Start-Sleep -Seconds 60\n",
        encoding="utf-8",
    )
    probe = tmp_path / "probe.ps1"
    module = REPOSITORY_ROOT / "scripts" / "local-process-guard.psm1"
    probe.write_text(
        f"Import-Module '{module}' -Force\n"
        f"$context = Enter-LocalSupervisor -RepositoryRoot '{repository}'\n"
        "$process = Start-LocalOwnedProcess -Context $context -Name 'probe' "
        f"-FilePath 'powershell.exe' -Arguments @('-NoProfile','-File','{root_script}')\n"
        f"$deadline = [DateTimeOffset]::UtcNow.AddSeconds(10)\n"
        f"while (-not (Test-Path -LiteralPath '{child_pid_path}')) {{\n"
        "  if ([DateTimeOffset]::UtcNow -ge $deadline) { throw 'child timeout' }\n"
        "  Start-Sleep -Milliseconds 50\n"
        "}\n"
        f"$childPid = [int](Get-Content -LiteralPath '{child_pid_path}')\n"
        "$rootPid = $process.Id\n"
        "$closed = Exit-LocalSupervisor -Context $context\n"
        "$state = Get-Content -Raw -LiteralPath $context.StatePath | ConvertFrom-Json\n"
        "[pscustomobject]@{closed=$closed;rootPid=$rootPid;childPid=$childPid;"
        "rootAlive=[bool](Get-Process -Id $rootPid -ErrorAction SilentlyContinue);"
        "childAlive=[bool](Get-Process -Id $childPid -ErrorAction SilentlyContinue);"
        "nativeClosed=$context.NativeProcesses[0].Closed;"
        "state=$state} | ConvertTo-Json -Depth 8 -Compress\n",
        encoding="utf-8",
    )

    result = _powershell("-File", str(probe))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["closed"] is True
    assert payload["rootAlive"] is False
    assert payload["childAlive"] is False
    assert payload["nativeClosed"] is True
    assert payload["state"]["active"] is False
    assert payload["state"]["status"] == "stopped"
    assert len(payload["state"]["processes"]) == 1
    assert payload["state"]["processes"][0]["pid"] == payload["rootPid"]


@WINDOWS_ONLY
def test_suspended_assignment_failure_terminates_by_handle_before_child_executes(
    tmp_path: Path,
) -> None:
    repository, _humanizer = _script_fixture(tmp_path)
    marker = tmp_path / "must-not-run.txt"
    child = tmp_path / "child.ps1"
    child.write_text(f"Set-Content -LiteralPath '{marker}' -Value ran\n", encoding="utf-8")
    module = REPOSITORY_ROOT / "scripts" / "local-process-guard.psm1"
    result = _powershell(
        "-Command",
        "$ErrorActionPreference='Stop'; "
        f"Import-Module '{module}' -Force; "
        f"$c=Enter-LocalSupervisor -RepositoryRoot '{repository}'; "
        "try { Start-LocalOwnedProcess -Context $c -Name 'probe' -FilePath 'powershell.exe' "
        f"-Arguments @('-NoProfile','-File','{child}') -SimulateAssignmentFailure }} "
        "finally { [void](Exit-LocalSupervisor -Context $c) }",
    )

    assert result.returncode != 0
    normalized_error = re.sub(r"\s+", " ", result.stdout + result.stderr).casefold()
    assert "injected assignment" in normalized_error
    assert "invalidoperationexception" in normalized_error
    assert not marker.exists()


@WINDOWS_ONLY
def test_owned_process_redirects_are_confined_and_created_new(tmp_path: Path) -> None:
    repository, _humanizer = _script_fixture(tmp_path)
    outside = tmp_path / "outside.log"
    module = REPOSITORY_ROOT / "scripts" / "local-process-guard.psm1"
    escaped = _powershell(
        "-Command",
        "$ErrorActionPreference='Stop'; "
        f"Import-Module '{module}' -Force; "
        f"$c=Enter-LocalSupervisor -RepositoryRoot '{repository}'; "
        "try { Start-LocalOwnedProcess -Context $c -Name 'probe' -FilePath 'cmd.exe' "
        f"-Arguments @('/d','/c','echo no') -StandardOutput '{outside}' }} "
        "finally { [void](Exit-LocalSupervisor -Context $c) }",
    )
    assert escaped.returncode != 0
    assert "log root" in (escaped.stdout + escaped.stderr).casefold()
    assert not outside.exists()

    collision_probe = tmp_path / "collision.ps1"
    collision_probe.write_text(
        f"Import-Module '{module}' -Force\n"
        f"$c=Enter-LocalSupervisor -RepositoryRoot '{repository}'\n"
        "$log=Join-Path $c.LogRoot 'probe.stdout.log'\n"
        "Set-Content -LiteralPath $log -Value SENTINEL\n"
        "try {\n"
        "  Start-LocalOwnedProcess -Context $c -Name 'probe' -FilePath 'cmd.exe' "
        "-Arguments @('/d','/c','echo replace') -StandardOutput $log\n"
        "}\nfinally {\n"
        "  $value=Get-Content -Raw -LiteralPath $log\n"
        "  [void](Exit-LocalSupervisor -Context $c)\n"
        "  Write-Output $value\n"
        "}\n",
        encoding="utf-8",
    )
    collision = _powershell("-File", str(collision_probe))
    assert collision.returncode != 0
    assert "sentinel" in (collision.stdout + collision.stderr).casefold()


@WINDOWS_ONLY
def test_physical_repository_alias_uses_same_global_mutex(tmp_path: Path) -> None:
    repository, _humanizer = _script_fixture(tmp_path)
    module = REPOSITORY_ROOT / "scripts" / "local-process-guard.psm1"
    holder = tmp_path / "physical-holder.ps1"
    holder.write_text(
        f"Import-Module '{module}' -Force\n"
        f"$c=Enter-LocalSupervisor -RepositoryRoot '{repository}'\n"
        "Write-Output ($c.MutexName + '|' + $c.PhysicalRepositoryRoot)\n"
        "Start-Sleep -Seconds 3\n"
        "[void](Exit-LocalSupervisor -Context $c)\n",
        encoding="utf-8",
    )
    holder_process = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(holder)],
        cwd=REPOSITORY_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder_process.stdout is not None
    identity = holder_process.stdout.readline().strip()
    assert identity.startswith("Global\\WisdomeWriter-")
    alias = "\\\\?\\" + str(repository)

    contender = _powershell(
        "-Command",
        f"Import-Module '{module}' -Force; Enter-LocalSupervisor -RepositoryRoot '{alias}'",
    )

    assert contender.returncode != 0
    assert "already active" in (contender.stdout + contender.stderr).casefold()
    stdout, stderr = holder_process.communicate(timeout=10)
    assert holder_process.returncode == 0, stdout + stderr


@WINDOWS_ONLY
def test_stop_signal_latched_before_job_association_closes_later_job(tmp_path: Path) -> None:
    repository, _humanizer = _script_fixture(tmp_path)
    module = REPOSITORY_ROOT / "scripts" / "local-process-guard.psm1"
    result = _powershell(
        "-Command",
        "$ErrorActionPreference='Stop'; "
        f"Import-Module '{module}' -Force; Enable-LocalStopSignal; "
        f"$c=Enter-LocalSupervisor -RepositoryRoot '{repository}' -DeferJob; "
        "Initialize-LocalStopSignalContext -Context $c; "
        "[WisdomeConsoleStopSignal]::TriggerForTest(); "
        "Initialize-LocalSupervisorJob -Context $c; "
        "$done=Complete-LocalSupervisorAfterSignal -Context $c; "
        "$state=Get-Content -Raw $c.StatePath | ConvertFrom-Json; Disable-LocalStopSignal; "
        "[pscustomobject]@{done=$done;state=$state} | ConvertTo-Json -Depth 5 -Compress",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["done"] is True
    assert payload["state"]["active"] is False


@WINDOWS_ONLY
def test_initial_state_publish_failure_releases_mutex_for_next_supervisor(tmp_path: Path) -> None:
    repository, _humanizer = _script_fixture(tmp_path)
    module = REPOSITORY_ROOT / "scripts" / "local-process-guard.psm1"
    result = _powershell(
        "-Command",
        f"Import-Module '{module}' -Force; "
        f"try {{ Enter-LocalSupervisor -RepositoryRoot '{repository}' "
        "-SimulateInitialStateFailure } catch { $first=$_.Exception.Message }; "
        f"$c=Enter-LocalSupervisor -RepositoryRoot '{repository}'; "
        "$id=$c.InstanceId; [void](Exit-LocalSupervisor -Context $c); "
        "Write-Output ($first + '|' + $id)",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    first, instance_id = result.stdout.strip().splitlines()[-1].split("|", 1)
    assert "injected initial state failure" in first.casefold()
    assert len(instance_id) == 32


@WINDOWS_ONLY
def test_supervisor_mutex_rejects_concurrent_instance_and_preserves_unique_ledgers(
    tmp_path: Path,
) -> None:
    repository, _humanizer = _script_fixture(tmp_path)
    module = REPOSITORY_ROOT / "scripts" / "local-process-guard.psm1"
    holder = tmp_path / "holder.ps1"
    holder.write_text(
        f"Import-Module '{module}' -Force\n"
        f"$context = Enter-LocalSupervisor -RepositoryRoot '{repository}'\n"
        "Write-Output ($context.InstanceId + '|' + $context.StatePath)\n"
        "Start-Sleep -Seconds 3\n"
        "[void](Exit-LocalSupervisor -Context $context)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(holder)],
        cwd=REPOSITORY_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    first_line = process.stdout.readline().strip()
    assert "|" in first_line
    first_id, first_state = first_line.split("|", 1)
    contender = _powershell(
        "-Command",
        f"Import-Module '{module}' -Force; "
        f"Enter-LocalSupervisor -RepositoryRoot '{repository}' | Out-Null",
    )
    assert contender.returncode != 0
    assert "already active" in (contender.stdout + contender.stderr).casefold()
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stdout + stderr
    first_payload = json.loads(Path(first_state).read_text("utf-8-sig"))
    assert first_payload["instanceId"] == first_id

    sequential = _powershell(
        "-Command",
        f"Import-Module '{module}' -Force; "
        f"$c=Enter-LocalSupervisor -RepositoryRoot '{repository}'; "
        "$id=$c.InstanceId; [void](Exit-LocalSupervisor -Context $c); Write-Output $id",
    )

    assert sequential.returncode == 0, sequential.stdout + sequential.stderr
    assert sequential.stdout.strip().splitlines()[-1] != first_id
    assert json.loads(Path(first_state).read_text("utf-8-sig"))["instanceId"] == first_id


@WINDOWS_ONLY
def test_supervisor_close_failure_keeps_instance_ledger_active_error(tmp_path: Path) -> None:
    repository, _humanizer = _script_fixture(tmp_path)
    module = REPOSITORY_ROOT / "scripts" / "local-process-guard.psm1"
    result = _powershell(
        "-Command",
        f"Import-Module '{module}' -Force; "
        f"$c=Enter-LocalSupervisor -RepositoryRoot '{repository}'; "
        "$closed=Exit-LocalSupervisor -Context $c -CloseHandle { param($handle) $false }; "
        "$state=Get-Content -Raw -LiteralPath $c.StatePath | ConvertFrom-Json; "
        "[pscustomobject]@{closed=$closed;state=$state} | ConvertTo-Json -Depth 6 -Compress",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["closed"] is False
    assert payload["state"]["active"] is True
    assert payload["state"]["status"] == "cleanup_error"


@WINDOWS_ONLY
def test_console_control_handler_converts_interrupt_to_supervisor_stop_signal() -> None:
    module = REPOSITORY_ROOT / "scripts" / "local-process-guard.psm1"
    result = _powershell(
        "-Command",
        f"Import-Module '{module}' -Force; "
        "Enable-LocalStopSignal; "
        "[WisdomeConsoleStopSignal]::TriggerForTest(); "
        "$observed=Wait-LocalStopSignal -Milliseconds 1000; "
        "Disable-LocalStopSignal; Write-Output $observed",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "True"


@WINDOWS_ONLY
def test_console_handler_closes_job_and_commits_inactive_ledger_without_pipeline(
    tmp_path: Path,
) -> None:
    repository, _humanizer = _script_fixture(tmp_path)
    module = REPOSITORY_ROOT / "scripts" / "local-process-guard.psm1"
    result = _powershell(
        "-Command",
        f"Import-Module '{module}' -Force; "
        f"$c=Enter-LocalSupervisor -RepositoryRoot '{repository}'; "
        "$p=Start-LocalOwnedProcess -Context $c -Name 'probe' -FilePath 'powershell.exe' "
        "-Arguments @('-NoProfile','-Command','Start-Sleep -Seconds 60'); "
        "Enable-LocalStopSignal; Update-LocalStopSignalContext -Context $c; "
        "[WisdomeConsoleStopSignal]::TriggerForTest(); "
        "$observed=Wait-LocalStopSignal -Milliseconds 5000; "
        "$completed=Complete-LocalSupervisorAfterSignal -Context $c; "
        "$state=Get-Content -Raw -LiteralPath $c.StatePath | ConvertFrom-Json; "
        "$alive=[bool](Get-Process -Id $p.Id -ErrorAction SilentlyContinue); "
        "Disable-LocalStopSignal; "
        "[pscustomobject]@{observed=$observed;completed=$completed;alive=$alive;state=$state} "
        "| ConvertTo-Json -Depth 6 -Compress",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["observed"] is True
    assert payload["completed"] is True
    assert payload["alive"] is False
    assert payload["state"]["active"] is False
    assert payload["state"]["status"] == "stopped"


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    ready_status = 200

    def do_GET(self) -> None:  # noqa: N802
        if self.server.server_port == 3210 and self.path == "/api/health":
            status, body = 200, b'{"status":"ready"}'
        elif self.server.server_port == 8000 and self.path == "/health/live":
            status, body = 200, b'{"status":"ok"}'
        elif self.server.server_port == 8000 and self.path == "/health/ready":
            status, body = self.ready_status, b'{"status":"unavailable"}'
        else:
            status, body = 404, b"{}"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


@WINDOWS_ONLY
def test_start_requires_preexisting_django_readiness_and_never_stops_it(tmp_path: Path) -> None:
    repository, humanizer = _script_fixture(tmp_path)
    servers: list[http.server.ThreadingHTTPServer] = []
    threads: list[threading.Thread] = []
    _HealthHandler.ready_status = 503
    try:
        for port in (3210, 8000):
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _HealthHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            server.daemon_threads = True
            server.timeout = 0.2
            thread.start()
            servers.append(server)
            threads.append(thread)

        result = _powershell(
            "-File",
            str(REPOSITORY_ROOT / "scripts" / "start-local.ps1"),
            "-RepositoryRoot",
            str(repository),
            "-HumanizerRoot",
            str(humanizer),
            "-HealthTimeoutSeconds",
            "1",
        )

        assert result.returncode != 0
        assert "ready" in (result.stdout + result.stderr).casefold()
        assert urllib.request.urlopen("http://127.0.0.1:3210/api/health").status == 200
        assert urllib.request.urlopen("http://127.0.0.1:8000/health/live").status == 200
        states = list((repository / ".local" / "state" / "start-local").glob("*/state.json"))
        assert len(states) == 1
        state = json.loads(states[0].read_text("utf-8-sig"))
        assert state["processes"] == []
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)
        _HealthHandler.ready_status = 200


@WINDOWS_ONLY
def test_start_preflight_loads_local_env_and_refuses_production(tmp_path: Path) -> None:
    repository, humanizer = _script_fixture(tmp_path, environment="production")

    result = _powershell(
        "-File",
        str(REPOSITORY_ROOT / "scripts" / "start-local.ps1"),
        "-RepositoryRoot",
        str(repository),
        "-HumanizerRoot",
        str(humanizer),
        "-ValidateOnly",
    )

    assert result.returncode != 0
    assert "production" in (result.stdout + result.stderr).casefold()
    assert "do-not-print-this-secret" not in result.stdout + result.stderr
    assert not (repository / ".local" / "state" / "start-local-owned.json").exists()


@WINDOWS_ONLY
def test_start_preflight_reports_exact_loopback_endpoints_without_starting(
    tmp_path: Path,
) -> None:
    repository, humanizer = _script_fixture(tmp_path)

    result = _powershell(
        "-File",
        str(REPOSITORY_ROOT / "scripts" / "start-local.ps1"),
        "-RepositoryRoot",
        str(repository),
        "-HumanizerRoot",
        str(humanizer),
        "-ValidateOnly",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "http://127.0.0.1:3210" in result.stdout
    assert "http://127.0.0.1:8000" in result.stdout
    assert "do-not-print-this-secret" not in result.stdout + result.stderr
    assert not (repository / ".local" / "state" / "start-local-owned.json").exists()


def test_ci_configuration_has_portable_locked_local_gates() -> None:
    workflow = yaml.safe_load(
        (REPOSITORY_ROOT / ".github" / "workflows" / "quality.yml").read_text("utf-8")
    )
    job = workflow["jobs"]["local-quality"]
    assert set(job["strategy"]["matrix"]["os"]) == {"windows-latest", "ubuntu-latest"}
    steps = job["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    python_step = next(
        step for step in steps if step.get("uses", "").startswith("actions/setup-python@")
    )
    assert str(python_step["with"]["python-version"]) == "3.12"
    commands = "\n".join(str(step.get("run", "")) for step in steps)
    assert "${{" not in commands
    assert "uv sync --frozen --extra dev" in commands
    assert "scripts/ci_changed_python.py" in commands
    assert "makemigrations --check --dry-run" in commands
    assert "test_local_housing_workflow.py" in commands
    assert "collect_recent_housing" not in commands
    expected_tests = {
        "tests/unit/test_queue_configuration.py",
        "tests/unit/test_local_runtime.py",
        "tests/unit/test_local_content_dates.py",
        "tests/unit/test_local_content_http.py",
        "tests/unit/test_applyhome_public_html.py",
        "tests/unit/test_lh_public_html.py",
        "tests/unit/test_local_content_selection.py",
        "tests/unit/test_local_content_rendering.py",
        "tests/unit/test_local_content_images.py",
        "tests/unit/test_local_content_humanizer.py",
        "tests/unit/test_local_content_bundles.py",
        "tests/unit/test_local_content_preview.py",
        "tests/unit/test_local_scripts.py",
        "tests/integration/test_local_housing_workflow.py",
    }
    configured_tests = {
        token for token in commands.split() if token.startswith("tests/") and token.endswith(".py")
    }
    assert configured_tests == expected_tests
    ruff_step = next(
        step for step in steps if step.get("name") == "Ruff changed files and local content"
    )
    assert set(ruff_step["env"]) == {
        "GITHUB_BASE_SHA",
        "GITHUB_DEFAULT_BRANCH",
        "GITHUB_EVENT_BEFORE",
        "GITHUB_EVENT_NAME",
        "GITHUB_REF_NAME",
    }


def _initialize_git_repository(repository: Path) -> None:
    repository.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=repository, check=True)


def _ci_selector(
    repository: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "ci_changed_python.py"),
            *arguments,
            "--list",
        ],
        cwd=repository,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _ci_selector_module():
    path = REPOSITORY_ROOT / "scripts" / "ci_changed_python.py"
    spec = importlib.util.spec_from_file_location("ci_changed_python_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ci_changed_python_gate_selects_multi_commit_diff_and_type_changes(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _initialize_git_repository(repository)
    (repository / "src" / "apps" / "local_content").mkdir(parents=True)
    (repository / "src" / "apps" / "local_content" / "always.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    (repository / "legacy.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
    (repository / "changed.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "changed.py"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "change one"], cwd=repository, check=True)
    (repository / "second.py").write_text("VALUE = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "second.py"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "change two"], cwd=repository, check=True)

    result = _ci_selector(repository, "--event", "push", "--before", "HEAD~2")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "changed.py",
        "second.py",
        "src/apps/local_content/always.py",
    ]
    assert "legacy.py" not in result.stdout.splitlines()


def test_ci_changed_python_gate_includes_type_changed_python_file(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _initialize_git_repository(repository)
    link_blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repository,
        input="target.py",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"120000,{link_blob},typed.py"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "commit", "-qm", "symlink mode"], cwd=repository, check=True)
    (repository / "typed.py").write_text("VALUE = 1\n", encoding="utf-8")
    file_blob = subprocess.run(
        ["git", "hash-object", "-w", "typed.py"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-index", "--cacheinfo", f"100644,{file_blob},typed.py"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "commit", "-qm", "regular mode"], cwd=repository, check=True)
    status = subprocess.run(
        ["git", "diff", "--name-status", "--diff-filter=T", "HEAD~1", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert status == "T\ttyped.py"

    result = _ci_selector(repository, "--event", "push", "--before", "HEAD~1")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["typed.py"]


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_ci_changed_python_gate_uses_fetched_default_branch_for_zero_or_dispatch(
    tmp_path: Path,
    event: str,
) -> None:
    repository = tmp_path / "repository"
    _initialize_git_repository(repository)
    (repository / "base.py").write_text("BASE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", base_sha],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "switch", "-qc", "feature"], cwd=repository, check=True)
    (repository / "feature.py").write_text("FEATURE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "feature"], cwd=repository, check=True)

    arguments = [
        "--event",
        event,
        "--default-branch",
        "main",
        "--ref-name",
        "feature",
    ]
    if event == "push":
        arguments.extend(("--before", "0" * 40))
    result = _ci_selector(repository, *arguments)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["feature.py"]


def test_ci_changed_python_gate_uses_empty_tree_for_first_default_branch_push(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _initialize_git_repository(repository)
    (repository / "first.py").write_text("FIRST = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=repository, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", head], cwd=repository, check=True
    )

    result = _ci_selector(
        repository,
        "--event",
        "push",
        "--before",
        "0" * 40,
        "--default-branch",
        "main",
        "--ref-name",
        "main",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["first.py"]


def test_ci_changed_python_nul_decoder_preserves_unicode_and_embedded_newline() -> None:
    module = _ci_selector_module()
    names = ("한글 공고 '자료'.py", "line\nbreak.py")
    payload = b"\0".join(os.fsencode(name) for name in names) + b"\0"

    assert module._decode_git_paths(payload) == names


def test_ci_changed_python_gate_selects_korean_space_and_quote_filename(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _initialize_git_repository(repository)
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
    unusual = "한국 공고 '검증'.py"
    (repository / unusual).write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "unusual"], cwd=repository, check=True)

    result = _ci_selector(repository, "--event", "push", "--before", "HEAD~1")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == unusual


@pytest.mark.skipif(sys.platform == "win32", reason="Windows filenames cannot contain newlines")
def test_ci_changed_python_gate_does_not_omit_valid_newline_filename(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _initialize_git_repository(repository)
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
    unusual = "line\nbreak.py"
    (repository / unusual).write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "newline"], cwd=repository, check=True)
    module = _ci_selector_module()

    selected = module.selected_python_files(
        event="push",
        base=None,
        before="HEAD~1",
        default_branch=None,
        ref_name=None,
    )

    assert selected == (unusual,)


def test_ci_selector_reads_malicious_but_valid_ref_only_from_environment(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _initialize_git_repository(repository)
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", base], cwd=repository, check=True
    )
    malicious_ref = "feature/$(echo-owned)"
    subprocess.run(["git", "switch", "-qc", malicious_ref], cwd=repository, check=True)
    (repository / "safe.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "feature"], cwd=repository, check=True)
    marker = repository / "echo-owned"
    env = dict(os.environ)
    env.update(
        {
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_EVENT_BEFORE": "0" * 40,
            "GITHUB_DEFAULT_BRANCH": "main",
            "GITHUB_REF_NAME": malicious_ref,
            "GITHUB_BASE_SHA": "",
        }
    )

    result = _ci_selector(repository, env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "safe.py"
    assert not marker.exists()


def test_ci_changed_python_gate_fails_closed_for_unresolved_pr_base(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _initialize_git_repository(repository)
    (repository / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)

    result = _ci_selector(
        repository,
        "--event",
        "pull_request",
        "--base",
        "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    )

    assert result.returncode != 0
    assert "trustworthy" in result.stderr.casefold()


def test_ci_changed_python_gate_fails_closed_when_before_is_missing_in_shallow_clone(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _initialize_git_repository(source)
    (source / "first.py").write_text("FIRST = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=source, check=True)
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
    ).stdout.strip()
    (source / "second.py").write_text("SECOND = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=source, check=True)
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", source.as_uri(), str(shallow)],
        check=True,
    )

    result = _ci_selector(shallow, "--event", "push", "--before", before)

    assert result.returncode != 0
    assert "trustworthy" in result.stderr.casefold()


def test_local_tool_and_runtime_artifacts_are_ignored_by_git() -> None:
    for relative in (
        ".tools/uv/example/uv.exe",
        ".local/state/start-local-owned.json",
        ".env.local",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", relative],
            cwd=REPOSITORY_ROOT,
            check=False,
        )
        assert result.returncode == 0, relative
