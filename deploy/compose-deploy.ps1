[CmdletBinding()]
param(
    [ValidateRange(10, 3600)]
    [int]$StopTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"

# 한국어: beat와 모든 Celery worker를 멈춘 상태에서만 migration을 실행한다.
# English: Run migrations only after beat and every Celery worker are stopped.
$QuiescedServices = @(
    "beat",
    "worker-collect",
    "worker-extract",
    "worker-editorial",
    "worker-publish",
    "worker-publish-blogger",
    "worker-reconcile",
    "ocr-worker"
)

$ComposeServices = @(& docker compose config --services)
if ($LASTEXITCODE -ne 0) {
    throw "docker compose config failed"
}

$ConfiguredConsumers = @(
    $ComposeServices |
        Where-Object {
            $_ -eq "beat" -or
            $_ -eq "ocr-worker" -or
            $_ -like "worker-*"
        }
)
$UntrackedConsumers = @(
    $ConfiguredConsumers |
        Where-Object { $_ -notin $QuiescedServices }
)
$MissingServices = @(
    $QuiescedServices |
        Where-Object { $_ -notin $ComposeServices }
)
if ($UntrackedConsumers.Count -gt 0 -or $MissingServices.Count -gt 0) {
    throw (
        "compose worker inventory changed; update deploy/compose-deploy.ps1 " +
        "(untracked: $($UntrackedConsumers -join ', '); " +
        "missing: $($MissingServices -join ', '))"
    )
}

& docker compose build
if ($LASTEXITCODE -ne 0) {
    throw "docker compose build failed"
}

& docker compose stop --timeout $StopTimeoutSeconds @QuiescedServices
if ($LASTEXITCODE -ne 0) {
    throw "failed to stop beat and workers"
}

& docker compose up -d postgres redis minio minio-bootstrap
if ($LASTEXITCODE -ne 0) {
    throw "failed to start infrastructure services"
}

& docker compose up -d --wait --wait-timeout 120 postgres
if ($LASTEXITCODE -ne 0) {
    throw "postgres did not become healthy before migration"
}

& docker compose up --no-deps --force-recreate `
    --abort-on-container-exit --exit-code-from migrate migrate
if ($LASTEXITCODE -ne 0) {
    throw "migration failed; beat and workers remain stopped"
}

& docker compose up -d web @QuiescedServices
if ($LASTEXITCODE -ne 0) {
    throw "migration succeeded but application restart failed"
}
