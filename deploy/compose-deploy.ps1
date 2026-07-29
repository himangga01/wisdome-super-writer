[CmdletBinding()]
param(
    [ValidateRange(1801, 7200)]
    [int]$StopTimeoutSeconds = 2400,

    [ValidateRange(60, 7200)]
    [int]$LegacyDrainTimeoutSeconds = 2400
)

$ErrorActionPreference = "Stop"

# 한국어: web/beat를 먼저 차단하고 구버전 worker로 legacy 큐를 비운 뒤 migration한다.
# English: Stop web/beat first, drain legacy queues with old workers, then migrate.
$WorkerServices = @(
    "worker-source-check",
    "worker-collect",
    "worker-evidence-fanout",
    "worker-extract",
    "worker-editorial",
    "worker-publish",
    "worker-publish-blogger",
    "worker-reconcile",
    "ocr-worker"
)
$IngressServices = @("web", "beat")
$ApplicationServices = @($IngressServices + $WorkerServices)
$LegacyQueues = @(
    "collect",
    "default",
    "extract.generic",
    "generate",
    "publish"
)
$LegacyPollSeconds = 5
$ComposeProjectName = "wisdome-super-writer"
$CollectionEgressNetworkName = $null

$ComposeServices = @(& docker compose config --services)
if ($LASTEXITCODE -ne 0) {
    throw "docker compose config failed"
}

$ConfiguredConsumers = @(
    $ComposeServices |
        Where-Object {
            $_ -eq "ocr-worker" -or
            $_ -like "worker-*"
        }
)
$UntrackedConsumers = @(
    $ConfiguredConsumers |
        Where-Object { $_ -notin $WorkerServices }
)
$MissingServices = @(
    $ApplicationServices |
        Where-Object { $_ -notin $ComposeServices }
)
if ($UntrackedConsumers.Count -gt 0 -or $MissingServices.Count -gt 0) {
    throw (
        "compose worker inventory changed; update deploy/compose-deploy.ps1 " +
        "(untracked: $($UntrackedConsumers -join ', '); " +
        "missing: $($MissingServices -join ', '))"
    )
}

$ExistingWorkerContainerIds = @(
    foreach ($Service in $WorkerServices) {
        $ContainerIds = @(& docker compose ps -a -q $Service)
        if ($LASTEXITCODE -ne 0) {
            throw "failed to inspect existing worker container for $Service"
        }
        $ContainerIds | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    }
)
$LegacyExtractWorkerContainerIds = @(
    & docker compose ps -a -q worker-extract |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
)
if ($LASTEXITCODE -ne 0) {
    throw "failed to inspect existing extract worker containers"
}

function Get-LegacyQueueLengths {
    $Lengths = @{}
    foreach ($QueueName in $LegacyQueues) {
        $RawLength = @(
            & docker compose exec -T redis sh -ec `
                'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" LLEN "$1"' `
                sh $QueueName
        )
        if ($LASTEXITCODE -ne 0) {
            throw "failed to inspect legacy Redis queue $QueueName"
        }
        $LengthText = [string]($RawLength | Select-Object -Last 1)
        [long]$Length = 0
        if (-not [long]::TryParse($LengthText.Trim(), [ref]$Length)) {
            throw "legacy Redis queue $QueueName returned a non-numeric length"
        }
        $Lengths[$QueueName] = $Length
    }
    return $Lengths
}

function Get-BrokerUnackedCount {
    $Counts = @()
    foreach ($Probe in @(
        @{ Command = "HLEN"; Key = "unacked" },
        @{ Command = "ZCARD"; Key = "unacked_index" }
    )) {
        $RawCount = @(
            & docker compose exec -T redis sh -ec `
                'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" "$1" "$2"' `
                sh $Probe.Command $Probe.Key
        )
        if ($LASTEXITCODE -ne 0) {
            throw "failed to inspect Redis broker unacked state"
        }
        $CountText = [string]($RawCount | Select-Object -Last 1)
        [long]$Count = 0
        if (-not [long]::TryParse($CountText.Trim(), [ref]$Count)) {
            throw "Redis broker unacked state returned a non-numeric count"
        }
        $Counts += $Count
    }
    return ($Counts | Measure-Object -Maximum).Maximum
}

function Wait-LegacyQueueDrain {
    param(
        [Parameter(Mandatory = $true)]
        [int]$TimeoutSeconds,

        [Parameter(Mandatory = $true)]
        [string]$Phase
    )

    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ($true) {
        $Lengths = Get-LegacyQueueLengths
        [long]$Total = 0
        foreach ($Length in $Lengths.Values) {
            $Total += [long]$Length
        }
        if ($Total -eq 0) {
            Write-Host "Legacy Celery queues drained during $Phase."
            return $true
        }
        $Summary = @(
            foreach ($QueueName in $LegacyQueues) {
                "$QueueName=$($Lengths[$QueueName])"
            }
        ) -join ", "
        Write-Host "Waiting for legacy Celery queues during $Phase ($Summary)."
        if ((Get-Date) -ge $Deadline) {
            return $false
        }
        Start-Sleep -Seconds $LegacyPollSeconds
    }
}

function Start-ExistingWorkers {
    if ($ExistingWorkerContainerIds.Count -eq 0) {
        return
    }
    $null = & docker container start @ExistingWorkerContainerIds
    if ($LASTEXITCODE -ne 0) {
        throw "failed to start existing worker containers for legacy queue drain"
    }
}

function Stop-ExistingWorkers {
    if ($ExistingWorkerContainerIds.Count -eq 0) {
        return
    }
    $null = & docker container stop --timeout $StopTimeoutSeconds @ExistingWorkerContainerIds
    if ($LASTEXITCODE -ne 0) {
        throw "failed to stop existing worker containers"
    }
}

function Get-CollectionEgressNetworkName {
    $NetworkNames = @(
        & docker network ls `
            --filter "label=com.docker.compose.project=$ComposeProjectName" `
            --filter "label=com.docker.compose.network=collection-egress" `
            --format "{{.Name}}"
    )
    if ($LASTEXITCODE -ne 0) {
        throw "failed to inspect the collection-egress network"
    }
    $NetworkNames = @(
        $NetworkNames |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($NetworkNames.Count -ne 1) {
        throw "expected exactly one collection-egress network for legacy drain"
    }
    return $NetworkNames[0]
}

function Connect-LegacyExtractEgress {
    if ($LegacyExtractWorkerContainerIds.Count -eq 0) {
        return
    }
    $script:CollectionEgressNetworkName = (
        Get-CollectionEgressNetworkName
    )
    foreach ($ContainerId in $LegacyExtractWorkerContainerIds) {
        $AttachedNetworks = @(
            & docker inspect `
                --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' `
                $ContainerId
        )
        if ($LASTEXITCODE -ne 0) {
            throw "failed to inspect legacy extract worker networks"
        }
        if ($CollectionEgressNetworkName -notin $AttachedNetworks) {
            $null = & docker network connect `
                $CollectionEgressNetworkName $ContainerId
            if ($LASTEXITCODE -ne 0) {
                throw "failed to attach legacy extract worker to collection egress"
            }
        }
    }
}

function Disconnect-LegacyExtractEgress {
    if (
        $LegacyExtractWorkerContainerIds.Count -eq 0 -or
        [string]::IsNullOrWhiteSpace($CollectionEgressNetworkName)
    ) {
        return
    }
    foreach ($ContainerId in $LegacyExtractWorkerContainerIds) {
        $AttachedNetworks = @(
            & docker inspect `
                --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' `
                $ContainerId
        )
        if ($LASTEXITCODE -ne 0) {
            throw "failed to inspect stopped legacy extract worker networks"
        }
        if ($CollectionEgressNetworkName -in $AttachedNetworks) {
            $null = & docker network disconnect `
                $CollectionEgressNetworkName $ContainerId
            if ($LASTEXITCODE -ne 0) {
                throw "failed to remove temporary legacy extract egress"
            }
        }
    }
    $script:CollectionEgressNetworkName = $null
}

try {
    Connect-LegacyExtractEgress

    & docker compose stop --timeout 120 @IngressServices
    if ($LASTEXITCODE -ne 0) {
        throw "failed to stop web and beat before legacy queue drain"
    }

    & docker compose build
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose build failed"
    }

    & docker compose up -d postgres redis minio minio-bootstrap
    if ($LASTEXITCODE -ne 0) {
        throw "failed to start infrastructure services"
    }

    & docker compose up -d --wait --wait-timeout 120 postgres redis minio
    if ($LASTEXITCODE -ne 0) {
        throw "infrastructure services did not become healthy before legacy queue drain"
    }

    Start-ExistingWorkers
    if (-not (Wait-LegacyQueueDrain `
        -TimeoutSeconds $LegacyDrainTimeoutSeconds `
        -Phase "initial drain")) {
        Stop-ExistingWorkers
        throw (
            "legacy queue drain timed out before migration; " +
            "web, beat, and existing workers remain stopped"
        )
    }

    Stop-ExistingWorkers
    $PostStopLengths = Get-LegacyQueueLengths
    [long]$PostStopTotal = 0
    foreach ($Length in $PostStopLengths.Values) {
        $PostStopTotal += [long]$Length
    }
    $PostStopUnacked = Get-BrokerUnackedCount
    if ($PostStopUnacked -gt 0) {
        throw (
            "Redis still contains unacknowledged Celery deliveries after worker stop; " +
            "migration was not started and web, beat, and existing workers remain stopped"
        )
    }
    if ($PostStopTotal -gt 0) {
        Write-Host (
            "Legacy messages reappeared after graceful worker stop; " +
            "restarting the same worker containers for one bounded re-drain."
        )
        Start-ExistingWorkers
        if (-not (Wait-LegacyQueueDrain `
            -TimeoutSeconds $LegacyDrainTimeoutSeconds `
            -Phase "post-stop re-drain")) {
            Stop-ExistingWorkers
            throw (
                "legacy queue re-drain timed out before migration; " +
                "web, beat, and existing workers remain stopped"
            )
        }
        Stop-ExistingWorkers
        $PostRedrainUnacked = Get-BrokerUnackedCount
        if ($PostRedrainUnacked -gt 0) {
            throw (
                "Redis still contains unacknowledged Celery deliveries after re-drain; " +
                "migration was not started and web, beat, and existing workers remain stopped"
            )
        }
    }

    $FinalLegacyLengths = Get-LegacyQueueLengths
    [long]$FinalLegacyTotal = 0
    foreach ($Length in $FinalLegacyLengths.Values) {
        $FinalLegacyTotal += [long]$Length
    }
    if ($FinalLegacyTotal -gt 0) {
        throw (
            "legacy queues are not empty after the final worker stop; " +
            "migration was not started and web, beat, and existing workers remain stopped"
        )
    }
} finally {
    Disconnect-LegacyExtractEgress
}

& docker compose up -d --wait --wait-timeout 120 postgres
if ($LASTEXITCODE -ne 0) {
    throw "postgres did not become healthy before migration"
}

& docker compose up --no-deps --force-recreate `
    --abort-on-container-exit --exit-code-from migrate migrate
if ($LASTEXITCODE -ne 0) {
    throw "migration failed; web, beat, and workers remain stopped"
}

# 현재 코드와 일치하는 승인된 v3 소스 snapshot이 없으면 수집 worker를 시작하지 않는다.
# English: Do not start collection workers without approved v3 source snapshots matching this build.
& docker compose run --rm --no-deps migrate `
    python src/manage.py verify_source_registry_snapshots --require-approved-mvp
if ($LASTEXITCODE -ne 0) {
    throw (
        "source registry snapshot verification failed; " +
        "web, beat, and workers remain stopped"
    )
}

& docker compose up -d @ApplicationServices
if ($LASTEXITCODE -ne 0) {
    throw "migration succeeded but application restart failed"
}
