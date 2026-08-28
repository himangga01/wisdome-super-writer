#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$HumanizerRoot = '',
    [int]$HealthTimeoutSeconds = 45,
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$humanizerOrigin = 'http://127.0.0.1:3210'
$djangoOrigin = 'http://127.0.0.1:8000'
$scriptDirectory = Split-Path -Parent $PSCommandPath

if (-not [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
    [System.Runtime.InteropServices.OSPlatform]::Windows
)) {
    throw 'start-local.ps1 requires Windows.'
}
if ($HealthTimeoutSeconds -lt 1 -or $HealthTimeoutSeconds -gt 300) {
    throw 'HealthTimeoutSeconds must be between 1 and 300.'
}
Import-Module (Join-Path $scriptDirectory 'local-process-guard.psm1') -Force

function Resolve-SafeDirectory {
    param([string]$Path, [string]$Label)

    $full = [System.IO.Path]::GetFullPath($Path)
    $driveRoot = [System.IO.Path]::GetPathRoot($full).TrimEnd('\')
    if ($full.TrimEnd('\') -eq $driveRoot -or -not [System.IO.Directory]::Exists($full)) {
        throw "$Label must be an existing non-root directory."
    }
    $attributes = [System.IO.File]::GetAttributes($full)
    if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label cannot be a reparse point."
    }
    return $full.TrimEnd('\')
}

function Find-HumanizerDirectory {
    param([string]$Repository)

    $current = Get-Item -LiteralPath $Repository
    while ($null -ne $current.Parent) {
        $candidate = Join-Path $current.Parent.FullName 'ai-text-makes-likes-human'
        if ([System.IO.Directory]::Exists($candidate)) {
            return $candidate
        }
        $current = $current.Parent
    }
    throw 'The sibling humanizer project could not be found.'
}

function Assert-ExternalRegularFile {
    param([string]$Path, [string]$Label)

    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not [System.IO.File]::Exists($full)) {
        throw "$Label is missing."
    }
    $attributes = [System.IO.File]::GetAttributes($full)
    if (
        ($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        ($attributes -band [System.IO.FileAttributes]::Directory) -ne 0
    ) {
        throw "$Label cannot be a symlink or reparse point."
    }
    return $full
}

function Assert-HumanizerLayout {
    param([string]$Root)

    $packagePath = Assert-ExternalRegularFile -Path (Join-Path $Root 'package.json') -Label 'Humanizer package.json'
    $package = Get-Content -Raw -LiteralPath $packagePath -Encoding UTF8 | ConvertFrom-Json
    if ('start' -notin @($package.scripts.PSObject.Properties.Name)) {
        throw 'Humanizer package must define its start script.'
    }
}

function Import-LocalEnvironment {
    param([string]$Root, [string]$Path)

    $safePath = Assert-LocalRepositoryFile -RepositoryRoot $Root -Path $Path -Label '.env.local'
    foreach ($line in Get-Content -LiteralPath $safePath -Encoding UTF8) {
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

function Assert-LocalStopNotRequested {
    if ([WisdomeConsoleStopSignal]::StopRequested) {
        throw 'Local supervisor stop was requested.'
    }
}

function Invoke-LocalBoundedStep {
    param([scriptblock]$Action)

    Assert-LocalStopNotRequested
    $result = & $Action
    Assert-LocalStopNotRequested
    return $result
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
            return $payload -is [pscustomobject] -and $payload.status -eq $ExpectedStatus
        }
        return $true
    }
    catch {
        return $false
    }
}

function Wait-Health {
    param([string]$Url, [string]$ExpectedStatus = '', [int]$TimeoutSeconds)

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        Assert-LocalStopNotRequested
        $healthy = Test-Health -Url $Url -ExpectedStatus $ExpectedStatus
        Assert-LocalStopNotRequested
        if ($healthy) {
            return
        }
        if ([WisdomeConsoleStopSignal]::Wait(250)) {
            throw 'Local supervisor stop was requested.'
        }
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

if (-not $RepositoryRoot) {
    $RepositoryRoot = Split-Path -Parent $scriptDirectory
}
if (-not $HumanizerRoot) {
    $HumanizerRoot = Find-HumanizerDirectory -Repository $RepositoryRoot
}
$repository = Resolve-SafeDirectory -Path $RepositoryRoot -Label 'Repository root'
$humanizer = Resolve-SafeDirectory -Path $HumanizerRoot -Label 'Humanizer root'
[void](Assert-LocalRepositoryFile -RepositoryRoot $repository -Path (Join-Path $repository 'src\manage.py') -Label 'Django manage.py')
Assert-HumanizerLayout -Root $humanizer
Import-LocalEnvironment -Root $repository -Path (Join-Path $repository '.env.local')
if ($env:WISDOME_ENVIRONMENT -eq 'production') {
    throw 'start-local.ps1 refuses production mode.'
}
if ($env:WISDOME_ENVIRONMENT -ne 'development' -or $env:WISDOME_RUNTIME_MODE -ne 'local') {
    throw 'start-local.ps1 requires development environment and local runtime mode.'
}
$python = Assert-LocalRepositoryFile -RepositoryRoot $repository -Path (Join-Path $repository '.venv\Scripts\python.exe') -Label 'Project Python environment'

if ($ValidateOnly) {
    [pscustomobject]@{
        status = 'valid'
        humanizer = $humanizerOrigin
        django = $djangoOrigin
    } | ConvertTo-Json -Compress
    exit 0
}

$node = Get-Command 'node.exe' -ErrorAction Stop
$context = $null
$caughtFailure = $false
$stopSignalEnabled = $false
try {
    Enable-LocalStopSignal
    $stopSignalEnabled = $true
    $context = Enter-LocalSupervisor -RepositoryRoot $repository -DeferJob
    Assert-LocalStopNotRequested
    [void](Invoke-LocalBoundedStep { Initialize-LocalSupervisorJob -Context $context })

    $humanizerReady = Invoke-LocalBoundedStep {
        Test-Health -Url "$humanizerOrigin/api/health" -ExpectedStatus 'ready'
    }
    if ($humanizerReady) {
        [void](Invoke-LocalBoundedStep { Assert-LoopbackListener -Port 3210 })
    }
    else {
        $env:HOST = '127.0.0.1'
        $env:PORT = '3210'
        $env:CODEX_CHUNK_CONCURRENCY = '1'
        [void](Invoke-LocalBoundedStep {
            Start-LocalOwnedProcess -Context $context -Name 'humanizer' -FilePath $node.Source -Arguments @('dist\server.js') -WorkingDirectory $humanizer
        })
        [void](Invoke-LocalBoundedStep {
            Wait-Health -Url "$humanizerOrigin/api/health" -ExpectedStatus 'ready' -TimeoutSeconds $HealthTimeoutSeconds
        })
        [void](Invoke-LocalBoundedStep { Assert-LoopbackListener -Port 3210 })
    }

    $djangoLive = Invoke-LocalBoundedStep { Test-Health -Url "$djangoOrigin/health/live" }
    if ($djangoLive) {
        [void](Invoke-LocalBoundedStep { Assert-LoopbackListener -Port 8000 })
    }
    else {
        [void](Invoke-LocalBoundedStep {
            Start-LocalOwnedProcess -Context $context -Name 'django' -FilePath $python -Arguments @('src\manage.py', 'runserver', '127.0.0.1:8000', '--noreload') -WorkingDirectory $repository
        })
        [void](Invoke-LocalBoundedStep {
            Wait-Health -Url "$djangoOrigin/health/live" -TimeoutSeconds $HealthTimeoutSeconds
        })
        [void](Invoke-LocalBoundedStep { Assert-LoopbackListener -Port 8000 })
    }
    [void](Invoke-LocalBoundedStep {
        Wait-Health -Url "$djangoOrigin/health/ready" -TimeoutSeconds $HealthTimeoutSeconds
    })
    [void](Invoke-LocalBoundedStep { Set-LocalSupervisorRunning -Context $context })

    Write-Output "Preview: $djangoOrigin/local-articles/"
    Write-Output 'Collect: .\.venv\Scripts\python.exe src\manage.py collect_recent_housing --days 7 --humanize --write-articles'
    Write-Output "State: $($context.StatePath)"
    Write-Output 'Press Ctrl+C to close this instance job and stop only its owned process tree.'
    while (-not (Wait-LocalStopSignal -Milliseconds 1000)) {
        Assert-LocalStopNotRequested
        for ($index = 0; $index -lt $context.NativeProcesses.Count; $index += 1) {
            if ($context.NativeProcesses[$index].HasExited()) {
                throw "$($context.Processes[$index].name) exited; inspect its instance log paths."
            }
        }
        Assert-LocalStopNotRequested
    }
}
catch {
    $caughtFailure = $true
    if ($null -ne $context -and -not [WisdomeConsoleStopSignal]::StopRequested) {
        Set-LocalSupervisorTerminalStatus -Context $context -Status 'error' -ErrorCode 'START_FAILED'
    }
    throw
}
finally {
    try {
        if ($null -ne $context) {
            $closed = Exit-LocalSupervisor -Context $context
            if (-not $closed -and -not $caughtFailure) {
                throw 'The owned Windows job could not be closed cleanly.'
            }
        }
    }
    finally {
        if ($stopSignalEnabled) {
            Disable-LocalStopSignal
        }
    }
}
