#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$HumanizerRoot = '',
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$humanizerOrigin = 'http://127.0.0.1:3210'
$djangoOrigin = 'http://127.0.0.1:8000'

function Resolve-SafeDirectory {
    param([string]$Path, [string]$Label)

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath
    $root = [System.IO.Path]::GetPathRoot($resolved).TrimEnd('\')
    if ($resolved.TrimEnd('\') -eq $root) {
        throw "$Label cannot be a drive root."
    }
    return $resolved
}

function Find-HumanizerDirectory {
    param([string]$Repository)

    $current = Get-Item -LiteralPath $Repository
    while ($null -ne $current.Parent) {
        $candidate = Join-Path $current.Parent.FullName 'ai-text-makes-likes-human'
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            return $candidate
        }
        $current = $current.Parent
    }
    throw 'The sibling humanizer project could not be found.'
}

function Assert-RequiredFile {
    param([string]$Path, [string]$Label)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is missing."
    }
}

function Import-LocalEnvironment {
    param([string]$Path)

    Assert-RequiredFile -Path $Path -Label '.env.local'
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) {
            continue
        }
        $equals = $trimmed.IndexOf('=')
        if ($equals -le 0) {
            throw 'The local environment file contains an invalid assignment.'
        }
        $key = $trimmed.Substring(0, $equals)
        $value = $trimmed.Substring($equals + 1)
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            throw 'The local environment file contains an invalid assignment.'
        }
        if ($value.Length -ge 2 -and $value[0] -eq $value[$value.Length - 1] -and $value[0] -in @("'", '"')) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($key, $value, 'Process')
    }
}

function Assert-HumanizerLayout {
    param([string]$Root)

    $packagePath = Join-Path $Root 'package.json'
    Assert-RequiredFile -Path $packagePath -Label 'Humanizer package.json'
    $package = Get-Content -Raw -LiteralPath $packagePath -Encoding UTF8 | ConvertFrom-Json
    $scriptNames = @($package.scripts.PSObject.Properties.Name)
    if ('start' -notin $scriptNames) {
        throw 'Humanizer package must define its start script.'
    }
}

function Test-Health {
    param([string]$Url, [string]$ExpectedStatus = '')

    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -ne 200) {
            return $false
        }
        if ($ExpectedStatus) {
            $payload = $response.Content | ConvertFrom-Json
            return $payload.status -eq $ExpectedStatus
        }
        return $true
    }
    catch {
        return $false
    }
}

function Wait-Health {
    param(
        [string]$Url,
        [string]$ExpectedStatus = '',
        [int]$TimeoutSeconds = 45
    )

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        if (Test-Health -Url $Url -ExpectedStatus $ExpectedStatus) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out waiting for local health endpoint $Url."
}

function Assert-LoopbackListener {
    param([int]$Port)

    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop)
    if ($listeners.Count -eq 0 -or @($listeners | Where-Object { $_.LocalAddress -ne '127.0.0.1' }).Count -gt 0) {
        throw "Port $Port is not bound exclusively to literal loopback."
    }
}

function New-OwnedRecord {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Name,
        [string]$StandardOutput,
        [string]$StandardError
    )

    return [pscustomobject]@{
        name = $Name
        pid = $Process.Id
        startedAt = $Process.StartTime.ToUniversalTime().ToString('o')
        stdout = $StandardOutput
        stderr = $StandardError
    }
}

function Save-OwnedState {
    param(
        [string]$Path,
        [string]$Repository,
        [System.Collections.ArrayList]$Records,
        [bool]$Active
    )

    $state = [ordered]@{
        schemaVersion = 1
        repositoryRoot = $Repository
        active = $Active
        updatedAt = [DateTimeOffset]::UtcNow.ToString('o')
        processes = @($Records)
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Add-OwnedDescendants {
    param([System.Collections.ArrayList]$Records)

    $known = @{}
    foreach ($record in $Records) {
        $known[[int]$record.pid] = $true
    }
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($candidate in Get-CimInstance Win32_Process) {
            if ($known.ContainsKey([int]$candidate.ProcessId) -or -not $known.ContainsKey([int]$candidate.ParentProcessId)) {
                continue
            }
            $process = Get-Process -Id $candidate.ProcessId -ErrorAction SilentlyContinue
            if ($null -eq $process) {
                continue
            }
            [void]$Records.Add((New-OwnedRecord -Process $process -Name 'owned-child' -StandardOutput '' -StandardError ''))
            $known[[int]$candidate.ProcessId] = $true
            $changed = $true
        }
    }
}

function Stop-OwnedProcesses {
    param([System.Collections.ArrayList]$Records)

    $ordered = @($Records) | Sort-Object -Property pid -Descending
    foreach ($record in $ordered) {
        $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            continue
        }
        $actualStart = $process.StartTime.ToUniversalTime().ToString('o')
        if ($actualStart -cne $record.startedAt) {
            continue
        }
        Stop-Process -Id $record.pid -ErrorAction SilentlyContinue
    }
}

$scriptDirectory = Split-Path -Parent $PSCommandPath
if (-not $RepositoryRoot) {
    $RepositoryRoot = Split-Path -Parent $scriptDirectory
}
if (-not $HumanizerRoot) {
    $HumanizerRoot = Find-HumanizerDirectory -Repository $RepositoryRoot
}
$repository = Resolve-SafeDirectory -Path $RepositoryRoot -Label 'Repository root'
$humanizer = Resolve-SafeDirectory -Path $HumanizerRoot -Label 'Humanizer root'
Assert-RequiredFile -Path (Join-Path $repository 'src\manage.py') -Label 'Django manage.py'
Assert-HumanizerLayout -Root $humanizer
Import-LocalEnvironment -Path (Join-Path $repository '.env.local')
if ($env:WISDOME_ENVIRONMENT -eq 'production') {
    throw 'start-local.ps1 refuses production mode.'
}
if ($env:WISDOME_ENVIRONMENT -ne 'development' -or $env:WISDOME_RUNTIME_MODE -ne 'local') {
    throw 'start-local.ps1 requires development environment and local runtime mode.'
}
$python = Join-Path $repository '.venv\Scripts\python.exe'
Assert-RequiredFile -Path $python -Label 'Project Python environment'

if ($ValidateOnly) {
    [pscustomobject]@{
        status = 'valid'
        humanizer = $humanizerOrigin
        django = $djangoOrigin
    } | ConvertTo-Json -Compress
    exit 0
}

$npm = Get-Command 'npm.cmd' -ErrorAction Stop
$stateRoot = Join-Path $repository '.local\state'
$logRoot = Join-Path $stateRoot 'logs'
[void](New-Item -ItemType Directory -Force -Path $logRoot)
$statePath = Join-Path $stateRoot 'start-local-owned.json'
$owned = New-Object System.Collections.ArrayList

try {
    if (Test-Health -Url "$humanizerOrigin/api/health" -ExpectedStatus 'ready') {
        Assert-LoopbackListener -Port 3210
    }
    else {
        $env:HOST = '127.0.0.1'
        $env:PORT = '3210'
        $env:CODEX_CHUNK_CONCURRENCY = '1'
        $humanizerOut = Join-Path $logRoot 'humanizer.stdout.log'
        $humanizerErr = Join-Path $logRoot 'humanizer.stderr.log'
        $humanizerProcess = Start-Process -FilePath $npm.Source -ArgumentList @('start') -WorkingDirectory $humanizer -PassThru -WindowStyle Hidden -RedirectStandardOutput $humanizerOut -RedirectStandardError $humanizerErr
        [void]$owned.Add((New-OwnedRecord -Process $humanizerProcess -Name 'humanizer' -StandardOutput $humanizerOut -StandardError $humanizerErr))
        Save-OwnedState -Path $statePath -Repository $repository -Records $owned -Active $true
        Wait-Health -Url "$humanizerOrigin/api/health" -ExpectedStatus 'ready'
        Assert-LoopbackListener -Port 3210
        Add-OwnedDescendants -Records $owned
        Save-OwnedState -Path $statePath -Repository $repository -Records $owned -Active $true
    }

    if (Test-Health -Url "$djangoOrigin/health/live") {
        Assert-LoopbackListener -Port 8000
    }
    else {
        $djangoOut = Join-Path $logRoot 'django.stdout.log'
        $djangoErr = Join-Path $logRoot 'django.stderr.log'
        $djangoProcess = Start-Process -FilePath $python -ArgumentList @('src\manage.py', 'runserver', '127.0.0.1:8000', '--noreload') -WorkingDirectory $repository -PassThru -WindowStyle Hidden -RedirectStandardOutput $djangoOut -RedirectStandardError $djangoErr
        [void]$owned.Add((New-OwnedRecord -Process $djangoProcess -Name 'django' -StandardOutput $djangoOut -StandardError $djangoErr))
        Save-OwnedState -Path $statePath -Repository $repository -Records $owned -Active $true
        Wait-Health -Url "$djangoOrigin/health/live"
        Wait-Health -Url "$djangoOrigin/health/ready"
        Assert-LoopbackListener -Port 8000
        Add-OwnedDescendants -Records $owned
        Save-OwnedState -Path $statePath -Repository $repository -Records $owned -Active $true
    }

    Write-Output "Preview: $djangoOrigin/local-articles/"
    Write-Output 'Collect: .\.venv\Scripts\python.exe src\manage.py collect_recent_housing --days 7 --humanize --write-articles'
    Write-Output 'Press Ctrl+C to stop only the processes started by this script.'
    while ($true) {
        Start-Sleep -Seconds 1
        foreach ($record in $owned) {
            if ($record.name -ne 'owned-child' -and $null -eq (Get-Process -Id $record.pid -ErrorAction SilentlyContinue)) {
                throw "$($record.name) exited; inspect its recorded log paths."
            }
        }
    }
}
finally {
    try {
        Add-OwnedDescendants -Records $owned
    }
    catch {
        # Continue with the exact owned PIDs already recorded.
    }
    Stop-OwnedProcesses -Records $owned
    if (Test-Path -LiteralPath $statePath) {
        Save-OwnedState -Path $statePath -Repository $repository -Records $owned -Active $false
    }
}
