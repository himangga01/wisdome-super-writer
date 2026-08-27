from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _powershell(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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


def test_powershell_scripts_parse_and_contain_no_container_cli_invocation() -> None:
    files = (
        REPOSITORY_ROOT / "scripts" / "setup-local.ps1",
        REPOSITORY_ROOT / "scripts" / "start-local.ps1",
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
    python_step = next(step for step in steps if step.get("uses", "").startswith("actions/setup-python@"))
    assert str(python_step["with"]["python-version"]) == "3.12"
    commands = "\n".join(str(step.get("run", "")) for step in steps)
    assert "uv sync --frozen --extra dev" in commands
    assert "scripts/ci_changed_python.py" in commands
    assert "makemigrations --check --dry-run" in commands
    assert "test_local_housing_workflow.py" in commands
    assert "collect_recent_housing" not in commands


def test_ci_changed_python_gate_selects_diff_plus_all_local_content(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=repository, check=True)
    (repository / "src" / "apps" / "local_content").mkdir(parents=True)
    (repository / "src" / "apps" / "local_content" / "always.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    (repository / "legacy.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
    (repository / "changed.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "changed.py"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "change"], cwd=repository, check=True)

    result = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "ci_changed_python.py"),
            "--base",
            "HEAD~1",
            "--list",
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["changed.py", "src/apps/local_content/always.py"]
    assert "legacy.py" not in result.stdout.splitlines()


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
