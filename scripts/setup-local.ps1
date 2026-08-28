#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$HumanizerRoot = '',
    [string]$ToolchainLockPath = '',
    [string]$UvArchivePath = '',
    [ValidateSet('', 'write', 'flush', 'close', 'move-collision', 'substitution', 'in-place')]
    [string]$EnvironmentFaultPhase = '',
    [switch]$ValidateOnly,
    [switch]$ProvisionUvOnly,
    [switch]$InitializeEnvironmentOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$scriptDirectory = Split-Path -Parent $PSCommandPath
$reparseFlag = [System.IO.FileAttributes]::ReparsePoint
$allowedUvEntries = @('uv.exe', 'uvw.exe', 'uvx.exe')
$maximumArchiveBytes = 32 * 1024 * 1024
$maximumEntryCompressedBytes = 32 * 1024 * 1024
$maximumEntryExpandedBytes = 48 * 1024 * 1024
$maximumAggregateExpandedBytes = 48 * 1024 * 1024

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.Net.Http
Import-Module (Join-Path $scriptDirectory 'local-toolchain.psm1') -Force

function Resolve-SafeDirectory {
    param([string]$Path, [string]$Label)

    $full = [System.IO.Path]::GetFullPath($Path)
    $driveRoot = [System.IO.Path]::GetPathRoot($full).TrimEnd('\')
    if ($full.TrimEnd('\') -eq $driveRoot -or -not [System.IO.Directory]::Exists($full)) {
        throw "$Label must be an existing non-root directory."
    }
    $attributes = [System.IO.File]::GetAttributes($full)
    if (($attributes -band $script:reparseFlag) -ne 0) {
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

function Assert-RepositoryPathChain {
    param([string]$Root, [string]$Path, [string]$Label)

    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $targetFull = [System.IO.Path]::GetFullPath($Path)
    $prefix = $rootFull + [System.IO.Path]::DirectorySeparatorChar
    if (
        $targetFull -ne $rootFull -and
        -not $targetFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "$Label escapes the repository root."
    }
    $current = $rootFull
    $relative = if ($targetFull -eq $rootFull) { '' } else { $targetFull.Substring($prefix.Length) }
    $parts = @($relative -split '[\\/]' | Where-Object { $_ })
    foreach ($part in @('') + $parts) {
        if ($part) {
            $current = Join-Path $current $part
        }
        if ([System.IO.Directory]::Exists($current) -or [System.IO.File]::Exists($current)) {
            $attributes = [System.IO.File]::GetAttributes($current)
            if (($attributes -band $script:reparseFlag) -ne 0) {
                throw "$Label contains a symlink, junction, or reparse point."
            }
        }
    }
    return $targetFull
}

function New-SafeRepositoryDirectory {
    param([string]$Root, [string]$Path, [string]$Label)

    $full = Assert-RepositoryPathChain -Root $Root -Path $Path -Label $Label
    [void][System.IO.Directory]::CreateDirectory($full)
    [void](Assert-RepositoryPathChain -Root $Root -Path $full -Label $Label)
    return $full
}

function Assert-SafeRepositoryFile {
    param([string]$Root, [string]$Path, [string]$Label)

    $full = Assert-RepositoryPathChain -Root $Root -Path $Path -Label $Label
    if (-not [System.IO.File]::Exists($full)) {
        throw "$Label is missing."
    }
    $attributes = [System.IO.File]::GetAttributes($full)
    if (
        ($attributes -band $script:reparseFlag) -ne 0 -or
        ($attributes -band [System.IO.FileAttributes]::Directory) -ne 0
    ) {
        throw "$Label is not a safe regular file."
    }
    return $full
}

function Assert-ExternalRegularFile {
    param([string]$Path, [string]$Label)

    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not [System.IO.File]::Exists($full)) {
        throw "$Label is missing."
    }
    $attributes = [System.IO.File]::GetAttributes($full)
    if (
        ($attributes -band $script:reparseFlag) -ne 0 -or
        ($attributes -band [System.IO.FileAttributes]::Directory) -ne 0
    ) {
        throw "$Label cannot be a symlink or reparse point."
    }
    return $full
}

function Assert-RepositoryLayout {
    param([string]$Root)

    foreach ($relative in @('pyproject.toml', 'uv.lock', 'src\manage.py', '.env.local.example')) {
        [void](Assert-SafeRepositoryFile -Root $Root -Path (Join-Path $Root $relative) -Label "Repository marker $relative")
    }
    foreach ($relative in @('.tools', '.venv', '.local')) {
        [void](Assert-RepositoryPathChain -Root $Root -Path (Join-Path $Root $relative) -Label "Local path $relative")
    }
}

function Assert-HumanizerLayout {
    param([string]$Root)

    $packagePath = Assert-ExternalRegularFile -Path (Join-Path $Root 'package.json') -Label 'Humanizer package.json'
    $package = Get-Content -Raw -LiteralPath $packagePath -Encoding UTF8 | ConvertFrom-Json
    $scriptNames = @($package.scripts.PSObject.Properties.Name)
    if ('build' -notin $scriptNames -or 'start' -notin $scriptNames) {
        throw 'Humanizer package must define build and start scripts.'
    }
}

function Get-ToolchainLock {
    param([string]$Path)

    $lockFile = Assert-ExternalRegularFile -Path $Path -Label 'Toolchain lock'
    $lock = Get-Content -Raw -LiteralPath $lockFile -Encoding UTF8 | ConvertFrom-Json
    if ($lock.schema_version -ne 1 -or $lock.python.version -ne '3.12') {
        throw 'Toolchain lock schema or Python line is invalid.'
    }
    $version = [string]$lock.uv.version
    $url = [string]$lock.uv.windows_x64.url
    $archiveSha256 = [string]$lock.uv.windows_x64.sha256
    $executableSha256 = [string]$lock.uv.windows_x64.executable_sha256
    $expectedUrl = "https://github.com/astral-sh/uv/releases/download/$version/uv-x86_64-pc-windows-msvc.zip"
    if (
        $version -notmatch '^\d+\.\d+\.\d+$' -or
        $url -cne $expectedUrl -or
        $archiveSha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $executableSha256 -cnotmatch '^[0-9a-f]{64}$'
    ) {
        throw 'Toolchain lock contains an invalid official asset declaration.'
    }
    return [pscustomobject]@{
        Version = $version
        Url = $url
        ArchiveSha256 = $archiveSha256
        ExecutableSha256 = $executableSha256
    }
}

function Get-StreamSha256 {
    param([System.IO.Stream]$Stream)

    $Stream.Position = 0
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash($Stream)
    }
    finally {
        $sha.Dispose()
    }
    $Stream.Position = 0
    return ([System.BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
}

function Get-HeldFileSha256 {
    param([string]$Path)

    $stream = New-Object System.IO.FileStream(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::None
    )
    try {
        return Get-StreamSha256 -Stream $stream
    }
    finally {
        $stream.Dispose()
    }
}

function Assert-ProvidedArchiveChecksum {
    param([string]$Path, [string]$Expected)

    $archive = Assert-ExternalRegularFile -Path $Path -Label 'uv archive'
    if ((Get-HeldFileSha256 -Path $archive) -cne $Expected) {
        throw 'uv archive checksum does not match the pinned toolchain lock.'
    }
}

function Assert-PublishedUvDirectory {
    param([string]$Root, [string]$Path, [string]$ExecutableSha256)

    $directory = Assert-RepositoryPathChain -Root $Root -Path $Path -Label 'Published uv directory'
    if (-not [System.IO.Directory]::Exists($directory)) {
        throw 'Published uv directory is missing.'
    }
    $entries = @([System.IO.Directory]::EnumerateFileSystemEntries($directory))
    $names = @($entries | ForEach-Object { [System.IO.Path]::GetFileName($_) } | Sort-Object)
    if (($names -join '|') -cne (($script:allowedUvEntries | Sort-Object) -join '|')) {
        throw 'Published uv directory violates the closed entry allowlist.'
    }
    foreach ($name in $script:allowedUvEntries) {
        [void](Assert-SafeRepositoryFile -Root $Root -Path (Join-Path $directory $name) -Label "Published $name")
    }
    $uv = Join-Path $directory 'uv.exe'
    if ((Get-HeldFileSha256 -Path $uv) -cne $ExecutableSha256) {
        throw 'Published uv executable checksum does not match the toolchain lock.'
    }
    return $uv
}

function Remove-OwnedSetupSession {
    param([string]$Root, [string]$Session)

    if (-not [System.IO.Directory]::Exists($Session)) {
        return
    }
    [void](Assert-RepositoryPathChain -Root $Root -Path $Session -Label 'Owned setup session')
    $extract = Join-Path $Session 'extract'
    if ([System.IO.Directory]::Exists($extract)) {
        foreach ($name in $script:allowedUvEntries) {
            $file = Join-Path $extract $name
            if ([System.IO.File]::Exists($file)) {
                [void](Assert-SafeRepositoryFile -Root $Root -Path $file -Label 'Owned extracted file')
                [System.IO.File]::Delete($file)
            }
        }
        if (@([System.IO.Directory]::EnumerateFileSystemEntries($extract)).Count -eq 0) {
            [System.IO.Directory]::Delete($extract, $false)
        }
    }
    $archive = Join-Path $Session 'uv.zip'
    if ([System.IO.File]::Exists($archive)) {
        [void](Assert-SafeRepositoryFile -Root $Root -Path $archive -Label 'Owned uv archive')
        [System.IO.File]::Delete($archive)
    }
    if (@([System.IO.Directory]::EnumerateFileSystemEntries($Session)).Count -eq 0) {
        [System.IO.Directory]::Delete($Session, $false)
    }
}

function Install-PinnedUv {
    param([string]$Root, [pscustomobject]$Toolchain, [string]$ProvidedArchive)

    $toolsRoot = New-SafeRepositoryDirectory -Root $Root -Path (Join-Path $Root '.tools') -Label 'Tool root'
    $uvParent = New-SafeRepositoryDirectory -Root $Root -Path (Join-Path $toolsRoot 'uv') -Label 'uv parent'
    $published = Join-Path $uvParent $Toolchain.Version
    if ([System.IO.Directory]::Exists($published)) {
        return Assert-PublishedUvDirectory -Root $Root -Path $published -ExecutableSha256 $Toolchain.ExecutableSha256
    }

    $session = Join-Path $toolsRoot ('.setup-' + [System.Guid]::NewGuid().ToString('N'))
    [void](New-SafeRepositoryDirectory -Root $Root -Path $session -Label 'Owned setup session')
    $extract = Join-Path $session 'extract'
    [void](New-SafeRepositoryDirectory -Root $Root -Path $extract -Label 'Owned extract directory')
    $archivePath = Join-Path $session 'uv.zip'
    $archiveStream = $null
    $zip = $null
    $publishedSuccessfully = $false
    try {
        $archiveStream = New-Object System.IO.FileStream(
            $archivePath,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
        if ($ProvidedArchive) {
            $sourcePath = Assert-ExternalRegularFile -Path $ProvidedArchive -Label 'uv archive'
            $source = New-Object System.IO.FileStream(
                $sourcePath,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::Read
            )
            try {
                try {
                    [void](Copy-BoundedStream -Source $source -Destination $archiveStream -MaximumBytes $maximumArchiveBytes)
                }
                catch {
                    throw 'uv compressed archive exceeds its byte limit or could not be read.'
                }
            }
            finally {
                $source.Dispose()
            }
        }
        else {
            [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
            $client = New-Object System.Net.Http.HttpClient
            try {
                $download = $client.GetStreamAsync($Toolchain.Url).GetAwaiter().GetResult()
                try {
                    try {
                        [void](Copy-BoundedStream -Source $download -Destination $archiveStream -MaximumBytes $maximumArchiveBytes)
                    }
                    catch {
                        throw 'uv compressed archive exceeds its byte limit or could not be read.'
                    }
                }
                finally {
                    $download.Dispose()
                }
            }
            finally {
                $client.Dispose()
            }
        }
        $archiveStream.Flush($true)
        if ($archiveStream.Length -gt $maximumArchiveBytes) {
            throw 'uv compressed archive exceeds its byte limit.'
        }
        if ((Get-StreamSha256 -Stream $archiveStream) -cne $Toolchain.ArchiveSha256) {
            throw 'uv archive checksum does not match the pinned toolchain lock.'
        }

        $zip = New-Object System.IO.Compression.ZipArchive(
            $archiveStream,
            [System.IO.Compression.ZipArchiveMode]::Read,
            $true
        )
        $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
        [long]$aggregateExpanded = 0
        foreach ($entry in $zip.Entries) {
            $name = [string]$entry.FullName
            $hasControl = @($name.ToCharArray() | Where-Object { [char]::IsControl($_) }).Count -gt 0
            if (
                $name -cnotin $script:allowedUvEntries -or
                $name.IndexOfAny([char[]]@('/', '\', ':')) -ge 0 -or
                $hasControl
            ) {
                throw 'uv archive contains a noncanonical or non-allowlisted entry.'
            }
            if (-not $seen.Add($name)) {
                throw 'uv archive contains a duplicate or case-aliased entry.'
            }
            if ($entry.CompressedLength -gt $maximumEntryCompressedBytes) {
                throw 'uv archive entry compressed size exceeds its byte limit.'
            }
            if ($entry.Length -gt $maximumEntryExpandedBytes) {
                throw 'uv archive entry expanded size exceeds its byte limit.'
            }
            $aggregateExpanded += $entry.Length
            if ($aggregateExpanded -gt $maximumAggregateExpandedBytes) {
                throw 'uv archive aggregate expanded size exceeds its byte limit.'
            }
        }
        if ($seen.Count -ne $script:allowedUvEntries.Count) {
            throw 'uv archive does not contain the exact closed entry allowlist.'
        }

        foreach ($entry in $zip.Entries) {
            $destinationPath = Join-Path $extract $entry.FullName
            [void](Assert-RepositoryPathChain -Root $Root -Path $destinationPath -Label 'uv archive destination')
            $destination = New-Object System.IO.FileStream(
                $destinationPath,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
            try {
                $entryStream = $entry.Open()
                try {
                    $copied = Copy-BoundedStream -Source $entryStream -Destination $destination -MaximumBytes $maximumEntryExpandedBytes
                    if ($copied -ne $entry.Length) {
                        throw 'uv archive entry stream length differs from its metadata.'
                    }
                }
                finally {
                    $entryStream.Dispose()
                }
                $destination.Flush($true)
                if (
                    $entry.FullName -ceq 'uv.exe' -and
                    (Get-StreamSha256 -Stream $destination) -cne $Toolchain.ExecutableSha256
                ) {
                    throw 'Extracted uv executable checksum does not match the toolchain lock.'
                }
            }
            finally {
                $destination.Dispose()
            }
        }
        $zip.Dispose()
        $zip = $null
        $archiveStream.Dispose()
        $archiveStream = $null
        [void](Assert-RepositoryPathChain -Root $Root -Path $extract -Label 'Owned extract directory')
        try {
            [System.IO.Directory]::Move($extract, $published)
        }
        catch [System.IO.IOException] {
            if (-not [System.IO.Directory]::Exists($published)) {
                throw
            }
        }
        $uv = Assert-PublishedUvDirectory -Root $Root -Path $published -ExecutableSha256 $Toolchain.ExecutableSha256
        $publishedSuccessfully = $true
        return $uv
    }
    finally {
        if ($null -ne $zip) {
            $zip.Dispose()
        }
        if ($null -ne $archiveStream) {
            $archiveStream.Dispose()
        }
        try {
            Remove-OwnedSetupSession -Root $Root -Session $session
        }
        catch {
            if ($publishedSuccessfully) {
                throw
            }
        }
    }
}

function Initialize-LocalEnvironment {
    param([string]$Root, [string]$FaultPhase = '')

    $source = Assert-SafeRepositoryFile -Root $Root -Path (Join-Path $Root '.env.local.example') -Label 'Local environment template'
    $target = Assert-RepositoryPathChain -Root $Root -Path (Join-Path $Root '.env.local') -Label 'Local environment file'
    if ([System.IO.File]::Exists($target)) {
        [void](Assert-SafeRepositoryFile -Root $Root -Path $target -Label 'Existing local environment file')
        return $target
    }
    $payload = [System.IO.File]::ReadAllBytes($source)
    $temporary = Join-Path $Root ('.env.local.setup-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    [void](Assert-RepositoryPathChain -Root $Root -Path $temporary -Label 'Owned environment temporary file')
    $stream = $null
    try {
        $stream = New-Object System.IO.FileStream(
            $temporary,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
    }
    catch [System.IO.IOException] {
        throw 'Owned environment temporary FileMode.CreateNew failed.'
    }

    $ownedIdentity = Get-HeldStreamIdentity -Stream $stream
    try {
        if ($FaultPhase -eq 'write') {
            throw 'Injected environment write failure.'
        }
        $stream.Write($payload, 0, $payload.Length)
        if ($FaultPhase -eq 'flush') {
            throw 'Injected environment flush failure.'
        }
        $stream.Flush($true)
        $stream.Dispose()
        $stream = $null
        if ($FaultPhase -eq 'close') {
            throw 'Injected environment close failure.'
        }
    }
    catch {
        if ($null -ne $stream) {
            try { $stream.Dispose() } catch { }
            $stream = $null
        }
        throw
    }

    $currentIdentity = Get-PathFileIdentity -RepositoryRoot $Root -Path $temporary -Label 'Owned environment temporary file'
    if ($currentIdentity -cne $ownedIdentity) {
        throw 'Owned environment temporary file identity changed before publish.'
    }
    if ($FaultPhase -eq 'substitution') {
        [System.IO.File]::Move($temporary, $temporary + '.original')
        [System.IO.File]::WriteAllText($temporary, "SENTINEL=substitution`n")
    }
    elseif ($FaultPhase -eq 'in-place') {
        [System.IO.File]::WriteAllText($temporary, "SENTINEL=in-place`n")
    }
    if ($FaultPhase -eq 'move-collision') {
        [System.IO.File]::WriteAllText($target, "SENTINEL=move-collision`n")
    }
    if (-not [WisdomeFileIdentityNative]::MoveFileNoReplaceWriteThrough($temporary, $target)) {
        if (-not [System.IO.File]::Exists($target)) {
            throw 'Atomic environment publish failed.'
        }
        [void](Assert-SafeRepositoryFile -Root $Root -Path $target -Label 'Existing local environment file')
        return $target
    }
    [void](Assert-SafeRepositoryFile -Root $Root -Path $target -Label 'Published local environment file')

    $expectedSha = $null
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $expectedSha = ([System.BitConverter]::ToString($sha.ComputeHash($payload))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
    $published = New-Object System.IO.FileStream(
        $target,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    try {
        if ((Get-HeldStreamIdentity -Stream $published) -cne $ownedIdentity) {
            throw 'Published local environment file identity does not match the owned temporary file.'
        }
        if ((Get-HeldStreamSha256 -Stream $published) -cne $expectedSha) {
            throw 'Published local environment file content checksum does not match the template.'
        }
        if ($published.Length -ne $payload.Length) {
            throw 'Published local environment file content bytes do not match the template.'
        }
        $published.Position = 0
        $actual = New-Object byte[] $payload.Length
        $offset = 0
        while ($offset -lt $actual.Length) {
            $read = $published.Read($actual, $offset, $actual.Length - $offset)
            if ($read -le 0) {
                throw 'Published local environment file content bytes do not match the template.'
            }
            $offset += $read
        }
        for ($index = 0; $index -lt $payload.Length; $index += 1) {
            if ($actual[$index] -ne $payload[$index]) {
                throw 'Published local environment file content bytes do not match the template.'
            }
        }
    }
    finally {
        $published.Dispose()
    }
    return $target
}

function Import-LocalEnvironment {
    param([string]$Root, [string]$Path)

    $safePath = Assert-SafeRepositoryFile -Root $Root -Path $Path -Label 'Local environment file'
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

function Initialize-LocalRuntimeRoots {
    param([string]$Root)

    $roots = @(
        [pscustomobject]@{ Name = 'LOCAL_STATE_ROOT'; Default = '.local\state'; Label = 'Local state root' },
        [pscustomobject]@{ Name = 'LOCAL_ARTICLE_ROOT'; Default = 'output\housing'; Label = 'Local article root' },
        [pscustomobject]@{ Name = 'LOCAL_OBJECT_ROOT'; Default = '.local\objects'; Label = 'Local object root' }
    )
    foreach ($rootDefinition in $roots) {
        $configured = [Environment]::GetEnvironmentVariable($rootDefinition.Name, 'Process')
        $path = if ([string]::IsNullOrWhiteSpace($configured)) {
            Join-Path $Root $rootDefinition.Default
        }
        elseif ([System.IO.Path]::IsPathRooted($configured)) {
            $configured
        }
        else {
            Join-Path $Root $configured
        }
        [void](New-SafeRepositoryDirectory -Root $Root -Path $path -Label $rootDefinition.Label)
    }
}

function Invoke-Checked {
    param([string]$FilePath, [string[]]$Arguments, [string]$WorkingDirectory)

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

function Invoke-CheckedUv {
    param([string[]]$Arguments, [string]$WorkingDirectory)

    $result = Invoke-VerifiedUv -RepositoryRoot $repository -Path $uv -ExpectedSha256 $toolchain.ExecutableSha256 -Arguments $Arguments -WorkingDirectory $WorkingDirectory
    if ($result.Stdout) {
        [Console]::Out.Write($result.Stdout)
    }
    if ($result.Stderr) {
        [Console]::Error.Write($result.Stderr)
    }
    if ($result.ExitCode -ne 0) {
        throw "A required verified uv command failed with exit code $($result.ExitCode)."
    }
    return $result
}

if (-not [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
    [System.Runtime.InteropServices.OSPlatform]::Windows
)) {
    throw 'setup-local.ps1 requires Windows.'
}
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
Assert-RepositoryLayout -Root $repository
Assert-HumanizerLayout -Root $humanizer
$toolchain = Get-ToolchainLock -Path $ToolchainLockPath

if ($UvArchivePath -and $ValidateOnly) {
    Assert-ProvidedArchiveChecksum -Path $UvArchivePath -Expected $toolchain.ArchiveSha256
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
if ($InitializeEnvironmentOnly) {
    $envPath = Initialize-LocalEnvironment -Root $repository -FaultPhase $EnvironmentFaultPhase
    Import-LocalEnvironment -Root $repository -Path $envPath
    [void](Initialize-LocalRuntimeRoots -Root $repository)
    Write-Output 'Local environment initialization completed.'
    exit 0
}

$uv = Install-PinnedUv -Root $repository -Toolchain $toolchain -ProvidedArchive $UvArchivePath
if ($ProvisionUvOnly) {
    Write-Output "Pinned uv provisioned at $uv"
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
[void](Assert-ExternalRegularFile -Path (Join-Path $humanizer 'package-lock.json') -Label 'Humanizer package lock')
[void](Assert-ExternalRegularFile -Path (Join-Path $humanizer 'dist\server.js') -Label 'Built humanizer server')

[void](Assert-PublishedUvDirectory -Root $repository -Path (Split-Path -Parent $uv) -ExecutableSha256 $toolchain.ExecutableSha256)
$uvVersionResult = Invoke-CheckedUv -Arguments @('--version') -WorkingDirectory $repository
$uvVersion = $uvVersionResult.Stdout.Trim()
$uvVersionPattern = '^uv ' + [regex]::Escape($toolchain.Version) + '(?:\s|$)'
if ($uvVersion -notmatch $uvVersionPattern) {
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
    [void](Assert-PublishedUvDirectory -Root $repository -Path (Split-Path -Parent $uv) -ExecutableSha256 $toolchain.ExecutableSha256)
    [void](Invoke-CheckedUv -Arguments @('python', 'install', '3.12') -WorkingDirectory $repository)
    $python = '3.12'
}

[void](Assert-PublishedUvDirectory -Root $repository -Path (Split-Path -Parent $uv) -ExecutableSha256 $toolchain.ExecutableSha256)
[void](Invoke-CheckedUv -Arguments @('sync', '--frozen', '--extra', 'dev', '--python', $python) -WorkingDirectory $repository)
$venvPython = Assert-SafeRepositoryFile -Root $repository -Path (Join-Path $repository '.venv\Scripts\python.exe') -Label 'Project Python environment'

$envPath = Initialize-LocalEnvironment -Root $repository -FaultPhase $EnvironmentFaultPhase
Import-LocalEnvironment -Root $repository -Path $envPath
if ($env:WISDOME_ENVIRONMENT -ne 'development' -or $env:WISDOME_RUNTIME_MODE -ne 'local') {
    throw 'Local setup requires development environment and local runtime mode.'
}
[void](Initialize-LocalRuntimeRoots -Root $repository)
Invoke-Checked -FilePath $venvPython -Arguments @('src\manage.py', 'migrate', '--noinput') -WorkingDirectory $repository
Invoke-Checked -FilePath $venvPython -Arguments @('src\manage.py', 'seed_source_registry') -WorkingDirectory $repository
Invoke-Checked -FilePath $venvPython -Arguments @('src\manage.py', 'verify_source_registry_snapshots') -WorkingDirectory $repository
Invoke-Checked -FilePath $venvPython -Arguments @('src\manage.py', 'check') -WorkingDirectory $repository

Write-Output 'Local setup completed. No secret values were printed.'
