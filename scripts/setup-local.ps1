#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$HumanizerRoot = '',
    [string]$ToolchainLockPath = '',
    [string]$UvArchivePath = '',
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

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

function Get-ToolchainLock {
    param([string]$Path)

    Assert-RequiredFile -Path $Path -Label 'Toolchain lock'
    $lock = Get-Content -Raw -LiteralPath $Path -Encoding UTF8 | ConvertFrom-Json
    if ($lock.schema_version -ne 1 -or $lock.python.version -ne '3.12') {
        throw 'Toolchain lock schema or Python line is invalid.'
    }
    $version = [string]$lock.uv.version
    $url = [string]$lock.uv.windows_x64.url
    $sha256 = [string]$lock.uv.windows_x64.sha256
    $expectedUrl = "https://github.com/astral-sh/uv/releases/download/$version/uv-x86_64-pc-windows-msvc.zip"
    if (
        $version -notmatch '^\d+\.\d+\.\d+$' -or
        $url -cne $expectedUrl -or
        $sha256 -cnotmatch '^[0-9a-f]{64}$'
    ) {
        throw 'Toolchain lock contains an invalid official asset declaration.'
    }
    return [pscustomobject]@{
        Version = $version
        Url = $url
        Sha256 = $sha256
    }
}

function Assert-ArchiveChecksum {
    param([string]$Path, [string]$Expected)

    Assert-RequiredFile -Path $Path -Label 'uv archive'
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -cne $Expected) {
        throw 'uv archive checksum does not match the pinned toolchain lock.'
    }
}

function Assert-RepositoryLayout {
    param([string]$Root)

    foreach ($relative in @('pyproject.toml', 'uv.lock', 'src\manage.py', '.env.local.example')) {
        Assert-RequiredFile -Path (Join-Path $Root $relative) -Label "Repository marker $relative"
    }
}

function Assert-HumanizerLayout {
    param([string]$Root)

    $packagePath = Join-Path $Root 'package.json'
    Assert-RequiredFile -Path $packagePath -Label 'Humanizer package.json'
    $package = Get-Content -Raw -LiteralPath $packagePath -Encoding UTF8 | ConvertFrom-Json
    $scriptNames = @($package.scripts.PSObject.Properties.Name)
    if ('build' -notin $scriptNames -or 'start' -notin $scriptNames) {
        throw 'Humanizer package must define build and start scripts.'
    }
}

function Import-LocalEnvironment {
    param([string]$Path)

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

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )

    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "A required local setup command failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}

$scriptDirectory = Split-Path -Parent $PSCommandPath
if (-not $RepositoryRoot) {
    $RepositoryRoot = Split-Path -Parent $scriptDirectory
}
if (-not $HumanizerRoot) {
    $HumanizerRoot = Find-HumanizerDirectory -Repository $RepositoryRoot
}
if (-not $ToolchainLockPath) {
    $ToolchainLockPath = Join-Path $scriptDirectory 'toolchain-lock.json'
}
$repository = Resolve-SafeDirectory -Path $RepositoryRoot -Label 'Repository root'
$humanizer = Resolve-SafeDirectory -Path $HumanizerRoot -Label 'Humanizer root'
$lockPath = (Resolve-Path -LiteralPath $ToolchainLockPath -ErrorAction Stop).ProviderPath
Assert-RepositoryLayout -Root $repository
Assert-HumanizerLayout -Root $humanizer
$toolchain = Get-ToolchainLock -Path $lockPath

if ($UvArchivePath) {
    $providedArchive = (Resolve-Path -LiteralPath $UvArchivePath -ErrorAction Stop).ProviderPath
    Assert-ArchiveChecksum -Path $providedArchive -Expected $toolchain.Sha256
}

if ($ValidateOnly) {
    [pscustomobject]@{
        status = 'valid'
        python = '3.12'
        uv = $toolchain.Version
        humanizer = 'http://127.0.0.1:3210'
    } | ConvertTo-Json -Compress
    exit 0
}

$node = Get-Command 'node.exe' -ErrorAction Stop
$npm = Get-Command 'npm.cmd' -ErrorAction Stop
$codex = Get-Command 'codex.cmd' -ErrorAction SilentlyContinue
if ($null -eq $codex) {
    $codex = Get-Command 'codex.exe' -ErrorAction Stop
}
$previousErrorPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $codex.Source login status 1>$null 2>$null
$codexExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorPreference
if ($codexExitCode -ne 0) {
    throw 'Codex login is required by the sibling humanizer.'
}
$nodeVersionOutput = @(& $node.Source --version)
if ($LASTEXITCODE -ne 0 -or $nodeVersionOutput.Count -ne 1) {
    throw 'Node validation failed.'
}
$nodeVersion = [version]([string]$nodeVersionOutput[0]).TrimStart('v')
if ($nodeVersion -lt [version]'24.19.0') {
    throw 'Node 24.19.0 or newer is required by the sibling humanizer.'
}
& $npm.Source --version *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'npm validation failed.'
}
Assert-RequiredFile -Path (Join-Path $humanizer 'package-lock.json') -Label 'Humanizer package lock'
Assert-RequiredFile -Path (Join-Path $humanizer 'dist\server.js') -Label 'Built humanizer server'

$toolsRoot = Join-Path $repository '.tools'
$downloadRoot = Join-Path $toolsRoot 'downloads'
$uvRoot = Join-Path $toolsRoot (Join-Path 'uv' $toolchain.Version)
foreach ($path in @($toolsRoot, $downloadRoot, $uvRoot)) {
    [void](New-Item -ItemType Directory -Force -Path $path)
}
$archive = if ($UvArchivePath) {
    $providedArchive
}
else {
    Join-Path $downloadRoot "uv-$($toolchain.Version)-x86_64-pc-windows-msvc.zip"
}
if (-not $UvArchivePath -and -not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    Invoke-WebRequest -Uri $toolchain.Url -OutFile $archive -UseBasicParsing
}
Assert-ArchiveChecksum -Path $archive -Expected $toolchain.Sha256
Expand-Archive -LiteralPath $archive -DestinationPath $uvRoot -Force
$uv = Join-Path $uvRoot 'uv.exe'
Assert-RequiredFile -Path $uv -Label 'Pinned uv executable'
$uvVersion = [string](& $uv --version)
$uvVersionPattern = '^uv ' + [regex]::Escape($toolchain.Version) + '(?:\s|$)'
if ($LASTEXITCODE -ne 0 -or $uvVersion -notmatch $uvVersionPattern) {
    throw 'Extracted uv executable does not match the pinned version.'
}

$python = $null
$launcher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
if ($null -ne $launcher) {
    $previousErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $candidate = @(& $launcher.Source -3.12 -c 'import sys; print(sys.executable)' 2>$null)
    $launcherExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorPreference
    if ($launcherExitCode -eq 0 -and $candidate.Count -gt 0) {
        $python = [string]$candidate[-1]
    }
}
if ($null -eq $python) {
    Invoke-Checked -FilePath $uv -Arguments @('python', 'install', '3.12') -WorkingDirectory $repository
    $python = '3.12'
}

Invoke-Checked -FilePath $uv -Arguments @('sync', '--frozen', '--extra', 'dev', '--python', $python) -WorkingDirectory $repository
$venvPython = Join-Path $repository '.venv\Scripts\python.exe'
Assert-RequiredFile -Path $venvPython -Label 'Project Python environment'

$envPath = Join-Path $repository '.env.local'
if (-not (Test-Path -LiteralPath $envPath)) {
    Copy-Item -LiteralPath (Join-Path $repository '.env.local.example') -Destination $envPath
}
Import-LocalEnvironment -Path $envPath
if ($env:WISDOME_ENVIRONMENT -ne 'development' -or $env:WISDOME_RUNTIME_MODE -ne 'local') {
    throw 'Local setup requires development environment and local runtime mode.'
}

Invoke-Checked -FilePath $venvPython -Arguments @('src\manage.py', 'migrate', '--noinput') -WorkingDirectory $repository
Invoke-Checked -FilePath $venvPython -Arguments @('src\manage.py', 'seed_source_registry') -WorkingDirectory $repository
Invoke-Checked -FilePath $venvPython -Arguments @('src\manage.py', 'verify_source_registry_snapshots') -WorkingDirectory $repository
Invoke-Checked -FilePath $venvPython -Arguments @('src\manage.py', 'check') -WorkingDirectory $repository

Write-Output 'Local setup completed. No secret values were printed.'
