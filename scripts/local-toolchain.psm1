Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$reparseFlag = [System.IO.FileAttributes]::ReparsePoint

if (-not ('WisdomeFileIdentityNative' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class WisdomeFileIdentityNative
{
    private const UInt32 MOVEFILE_WRITE_THROUGH = 0x00000008;

    [StructLayout(LayoutKind.Sequential)]
    private struct FILETIME
    {
        public UInt32 Low;
        public UInt32 High;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BY_HANDLE_FILE_INFORMATION
    {
        public UInt32 FileAttributes;
        public FILETIME CreationTime;
        public FILETIME LastAccessTime;
        public FILETIME LastWriteTime;
        public UInt32 VolumeSerialNumber;
        public UInt32 FileSizeHigh;
        public UInt32 FileSizeLow;
        public UInt32 NumberOfLinks;
        public UInt32 FileIndexHigh;
        public UInt32 FileIndexLow;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(
        IntPtr handle,
        out BY_HANDLE_FILE_INFORMATION information
    );

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool MoveFileEx(
        string existingPath,
        string newPath,
        UInt32 flags
    );

    public static string Identity(IntPtr handle)
    {
        BY_HANDLE_FILE_INFORMATION information;
        if (!GetFileInformationByHandle(handle, out information))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "GetFileInformationByHandle failed");
        return information.VolumeSerialNumber.ToString("x8") + ":" +
            information.FileIndexHigh.ToString("x8") + information.FileIndexLow.ToString("x8");
    }

    public static bool MoveFileNoReplaceWriteThrough(string source, string target)
    {
        return MoveFileEx(source, target, MOVEFILE_WRITE_THROUGH);
    }
}
'@
}

function Assert-ToolchainPath {
    param([string]$RepositoryRoot, [string]$Path, [string]$Label)

    $root = [System.IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
    $target = [System.IO.Path]::GetFullPath($Path)
    $prefix = $root + [System.IO.Path]::DirectorySeparatorChar
    if ($target -ne $root -and -not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label escapes the repository root."
    }
    $current = $root
    $relative = if ($target -eq $root) { '' } else { $target.Substring($prefix.Length) }
    foreach ($part in @('') + @($relative -split '[\\/]' | Where-Object { $_ })) {
        if ($part) {
            $current = Join-Path $current $part
        }
        if ([IO.Directory]::Exists($current) -or [IO.File]::Exists($current)) {
            $attributes = [IO.File]::GetAttributes($current)
            if (($attributes -band $script:reparseFlag) -ne 0) {
                throw "$Label contains a symlink, junction, or reparse point."
            }
        }
    }
    return $target
}

function Get-HeldStreamSha256 {
    param([System.IO.Stream]$Stream)

    $Stream.Position = 0
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash($Stream)
    }
    finally {
        $sha.Dispose()
    }
    $Stream.Position = 0
    return ([BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
}

function Get-HeldStreamIdentity {
    param([System.IO.FileStream]$Stream)

    return [WisdomeFileIdentityNative]::Identity($Stream.SafeFileHandle.DangerousGetHandle())
}

function Get-PathFileIdentity {
    [CmdletBinding()]
    param([string]$RepositoryRoot, [string]$Path, [string]$Label = 'File')

    $safe = Assert-ToolchainPath -RepositoryRoot $RepositoryRoot -Path $Path -Label $Label
    $stream = New-Object IO.FileStream(
        $safe,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        return Get-HeldStreamIdentity -Stream $stream
    }
    finally {
        $stream.Dispose()
    }
}

function Copy-BoundedStream {
    [CmdletBinding()]
    param(
        [System.IO.Stream]$Source,
        [System.IO.Stream]$Destination,
        [long]$MaximumBytes
    )

    if ($MaximumBytes -lt 0) {
        throw 'Stream limit must be non-negative.'
    }
    $buffer = New-Object byte[] (1024 * 1024)
    [long]$total = 0
    while (($read = $Source.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $total += $read
        if ($total -gt $MaximumBytes) {
            throw 'Stream exceeded its byte limit.'
        }
        $Destination.Write($buffer, 0, $read)
    }
    return $total
}

function ConvertTo-WindowsArgument {
    param([string]$Value)

    if ($Value -and $Value -notmatch '[\s"]') {
        return $Value
    }
    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $slashes += 1
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * ($slashes * 2 + 1)))
            [void]$builder.Append('"')
        }
        else {
            [void]$builder.Append(('\' * $slashes))
            [void]$builder.Append($character)
        }
        $slashes = 0
    }
    [void]$builder.Append(('\' * ($slashes * 2)))
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-VerifiedUv {
    [CmdletBinding()]
    param(
        [string]$RepositoryRoot,
        [string]$Path,
        [string]$ExpectedSha256,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )

    $safe = Assert-ToolchainPath -RepositoryRoot $RepositoryRoot -Path $Path -Label 'uv executable'
    if (-not [IO.File]::Exists($safe)) {
        throw 'uv executable is missing.'
    }
    $attributes = [IO.File]::GetAttributes($safe)
    if (
        ($attributes -band $script:reparseFlag) -ne 0 -or
        ($attributes -band [IO.FileAttributes]::Directory) -ne 0
    ) {
        throw 'uv executable is not a safe regular file.'
    }
    $stream = New-Object IO.FileStream(
        $safe,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $process = $null
    try {
        $identity = Get-HeldStreamIdentity -Stream $stream
        if ((Get-HeldStreamSha256 -Stream $stream) -cne $ExpectedSha256) {
            throw 'uv executable checksum does not match the toolchain lock.'
        }
        $start = New-Object Diagnostics.ProcessStartInfo
        $start.FileName = $safe
        $start.WorkingDirectory = $WorkingDirectory
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $start.Arguments = (($Arguments | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join ' ')
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $start
        if (-not $process.Start()) {
            throw 'Verified uv process could not be started.'
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ((Get-HeldStreamIdentity -Stream $stream) -cne $identity) {
            throw 'uv executable identity changed while running.'
        }
        if ((Get-HeldStreamSha256 -Stream $stream) -cne $ExpectedSha256) {
            throw 'uv executable checksum changed while running.'
        }
        [void](Assert-ToolchainPath -RepositoryRoot $RepositoryRoot -Path $safe -Label 'uv executable')
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Stdout = $stdout
            Stderr = $stderr
        }
    }
    finally {
        if ($null -ne $process) {
            $process.Dispose()
        }
        $stream.Dispose()
    }
}

Export-ModuleMember -Function @(
    'Copy-BoundedStream',
    'Get-HeldStreamSha256',
    'Get-HeldStreamIdentity',
    'Get-PathFileIdentity',
    'Invoke-VerifiedUv'
)
