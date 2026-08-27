Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$reparseFlag = [System.IO.FileAttributes]::ReparsePoint

if (-not ('WisdomeLocalJobNative' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class WisdomeLocalJobNative
{
    private const UInt32 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
    private const Int32 JobObjectExtendedLimitInformation = 9;

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public Int64 PerProcessUserTimeLimit;
        public Int64 PerJobUserTimeLimit;
        public UInt32 LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public UInt32 ActiveProcessLimit;
        public UIntPtr Affinity;
        public UInt32 PriorityClass;
        public UInt32 SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public UInt64 ReadOperationCount;
        public UInt64 WriteOperationCount;
        public UInt64 OtherOperationCount;
        public UInt64 ReadTransferCount;
        public UInt64 WriteTransferCount;
        public UInt64 OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr job,
        Int32 infoClass,
        IntPtr info,
        UInt32 length
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr handle);

    public static IntPtr CreateKillOnCloseJob()
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject failed");

        JOBOBJECT_EXTENDED_LIMIT_INFORMATION info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        IntPtr pointer = Marshal.AllocHGlobal(length);
        try
        {
            Marshal.StructureToPtr(info, pointer, false);
            if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, pointer, (UInt32)length))
            {
                int error = Marshal.GetLastWin32Error();
                CloseHandle(job);
                throw new Win32Exception(error, "SetInformationJobObject failed");
            }
        }
        finally
        {
            Marshal.FreeHGlobal(pointer);
        }
        return job;
    }

    public static void AssignProcess(IntPtr job, IntPtr process)
    {
        if (!AssignProcessToJobObject(job, process))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "AssignProcessToJobObject failed");
    }
}
'@
}

if (-not ('WisdomeConsoleStopSignal' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static class WisdomeConsoleStopSignal
{
    private enum ControlType : uint
    {
        CtrlC = 0,
        CtrlBreak = 1,
        Close = 2,
        Logoff = 5,
        Shutdown = 6
    }

    private delegate bool HandlerRoutine(ControlType controlType);
    private static readonly ManualResetEvent Stop = new ManualResetEvent(false);
    private static readonly HandlerRoutine Handler = HandleControl;
    private static readonly object Sync = new object();
    private static bool installed = false;
    private static bool handled = false;
    private static bool succeeded = false;
    private static IntPtr job = IntPtr.Zero;
    private static string statePath = null;
    private static int[] processIds = new int[0];
    private static long[] creationTicks = new long[0];
    private static string stoppedJson = null;
    private static string errorJson = null;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetConsoleCtrlHandler(HandlerRoutine handler, bool add);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    private static bool HandleControl(ControlType controlType)
    {
        if (controlType != ControlType.CtrlC && controlType != ControlType.CtrlBreak)
            return false;
        ExecuteStop();
        return true;
    }

    private static bool DirectProcessesExited()
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(10);
        while (DateTime.UtcNow < deadline)
        {
            bool anyAlive = false;
            for (int index = 0; index < processIds.Length; index++)
            {
                try
                {
                    Process process = Process.GetProcessById(processIds[index]);
                    if (process.StartTime.ToUniversalTime().Ticks == creationTicks[index])
                        anyAlive = true;
                    process.Dispose();
                }
                catch (ArgumentException)
                {
                }
                catch
                {
                    anyAlive = true;
                }
            }
            if (!anyAlive)
                return true;
            Thread.Sleep(50);
        }
        return false;
    }

    private static void AtomicWrite(string path, string payload)
    {
        if (String.IsNullOrEmpty(path) || payload == null)
            return;
        string directory = Path.GetDirectoryName(path);
        string temporary = Path.Combine(directory, ".signal-" + Guid.NewGuid().ToString("N") + ".tmp");
        string backup = Path.Combine(directory, ".signal-" + Guid.NewGuid().ToString("N") + ".backup");
        File.WriteAllText(temporary, payload, new UTF8Encoding(false));
        if (File.Exists(path))
        {
            File.Replace(temporary, path, backup);
            File.Delete(backup);
        }
        else
        {
            File.Move(temporary, path);
        }
    }

    private static void ExecuteStop()
    {
        lock (Sync)
        {
            if (handled)
            {
                Stop.Set();
                return;
            }
            handled = true;
            if (job == IntPtr.Zero)
            {
                succeeded = true;
                Stop.Set();
                return;
            }
            bool closed = CloseHandle(job);
            job = IntPtr.Zero;
            bool exited = closed && DirectProcessesExited();
            succeeded = closed && exited;
            try
            {
                AtomicWrite(statePath, succeeded ? stoppedJson : errorJson);
            }
            catch
            {
                succeeded = false;
                try { AtomicWrite(statePath, errorJson); } catch { }
            }
            Stop.Set();
        }
    }

    public static void Install()
    {
        Stop.Reset();
        handled = false;
        succeeded = false;
        if (installed)
            return;
        if (!SetConsoleCtrlHandler(Handler, true))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "SetConsoleCtrlHandler failed");
        installed = true;
    }

    public static bool Wait(int milliseconds)
    {
        return Stop.WaitOne(milliseconds);
    }

    public static void Uninstall()
    {
        if (!installed)
            return;
        if (!SetConsoleCtrlHandler(Handler, false))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "SetConsoleCtrlHandler remove failed");
        installed = false;
        Stop.Reset();
    }

    public static void Configure(
        IntPtr configuredJob,
        string configuredStatePath,
        int[] configuredProcessIds,
        long[] configuredCreationTicks,
        string configuredStoppedJson,
        string configuredErrorJson
    )
    {
        lock (Sync)
        {
            job = configuredJob;
            statePath = configuredStatePath;
            processIds = configuredProcessIds ?? new int[0];
            creationTicks = configuredCreationTicks ?? new long[0];
            stoppedJson = configuredStoppedJson;
            errorJson = configuredErrorJson;
            handled = false;
            succeeded = false;
            Stop.Reset();
        }
    }

    public static bool WasHandled { get { return handled; } }
    public static bool Succeeded { get { return succeeded; } }

    public static void TriggerForTest()
    {
        ExecuteStop();
    }
}
'@
}

function Assert-WindowsPlatform {
    if (-not [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
        [System.Runtime.InteropServices.OSPlatform]::Windows
    )) {
        throw 'The local process supervisor requires Windows.'
    }
}

function Assert-LocalPathChain {
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
    foreach ($part in @('') + @($relative -split '[\\/]' | Where-Object { $_ })) {
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

function New-LocalDirectory {
    param([string]$Root, [string]$Path, [string]$Label)

    $full = Assert-LocalPathChain -Root $Root -Path $Path -Label $Label
    [void][System.IO.Directory]::CreateDirectory($full)
    [void](Assert-LocalPathChain -Root $Root -Path $full -Label $Label)
    return $full
}

function Assert-LocalRepositoryFile {
    [CmdletBinding()]
    param([string]$RepositoryRoot, [string]$Path, [string]$Label)

    $full = Assert-LocalPathChain -Root $RepositoryRoot -Path $Path -Label $Label
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

function Get-RepositoryMutexName {
    param([string]$RepositoryRoot)

    $normalized = [System.IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\').ToLowerInvariant()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash($bytes)
    }
    finally {
        $sha.Dispose()
    }
    $hex = ([System.BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
    return "Local\WisdomeWriter-$hex"
}

function Get-LocalSupervisorStateJson {
    param(
        [pscustomobject]$Context,
        [bool]$Active,
        [string]$Status,
        [string]$ErrorCode = ''
    )

    [void](Assert-LocalPathChain -Root $Context.RepositoryRoot -Path $Context.InstanceRoot -Label 'Supervisor instance root')
    $state = [ordered]@{
        schemaVersion = 2
        instanceId = $Context.InstanceId
        repositoryRoot = $Context.RepositoryRoot
        active = $Active
        status = $Status
        errorCode = if ($ErrorCode) { $ErrorCode } else { $null }
        updatedAt = [DateTimeOffset]::UtcNow.ToString('o')
        processes = @($Context.Processes)
    }
    return $state | ConvertTo-Json -Depth 6
}

function Write-LocalSupervisorState {
    param(
        [pscustomobject]$Context,
        [bool]$Active,
        [string]$Status,
        [string]$ErrorCode = ''
    )

    [void](Assert-LocalPathChain -Root $Context.RepositoryRoot -Path $Context.InstanceRoot -Label 'Supervisor instance root')
    $json = Get-LocalSupervisorStateJson -Context $Context -Active $Active -Status $Status -ErrorCode $ErrorCode
    $payload = [System.Text.Encoding]::UTF8.GetBytes($json)
    $temporary = Join-Path $Context.InstanceRoot ('.state-' + [System.Guid]::NewGuid().ToString('N') + '.tmp')
    $stream = New-Object System.IO.FileStream(
        $temporary,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $stream.Write($payload, 0, $payload.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
    if ([System.IO.File]::Exists($Context.StatePath)) {
        $backup = Join-Path $Context.InstanceRoot ('.state-' + [System.Guid]::NewGuid().ToString('N') + '.backup')
        [System.IO.File]::Replace($temporary, $Context.StatePath, $backup)
        [System.IO.File]::Delete($backup)
    }
    else {
        [System.IO.File]::Move($temporary, $Context.StatePath)
    }
}

function Release-LocalMutex {
    param([pscustomobject]$Context)

    if ($Context.MutexOwned) {
        $Context.Mutex.ReleaseMutex()
        $Context.MutexOwned = $false
    }
    $Context.Mutex.Dispose()
}

function Enter-LocalSupervisor {
    [CmdletBinding()]
    param([string]$RepositoryRoot)

    Assert-WindowsPlatform
    $repository = [System.IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
    if (-not [System.IO.Directory]::Exists($repository)) {
        throw 'Repository root is missing.'
    }
    [void](Assert-LocalPathChain -Root $repository -Path $repository -Label 'Repository root')
    $mutex = New-Object System.Threading.Mutex($false, (Get-RepositoryMutexName $repository))
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne(0)
        }
        catch [System.Threading.AbandonedMutexException] {
            $acquired = $true
        }
        if (-not $acquired) {
            throw 'A local supervisor is already active for this repository.'
        }
        $stateRoot = New-LocalDirectory -Root $repository -Path (Join-Path $repository '.local\state\start-local') -Label 'Supervisor state root'
        $instanceId = [System.Guid]::NewGuid().ToString('N')
        $instanceRoot = New-LocalDirectory -Root $repository -Path (Join-Path $stateRoot $instanceId) -Label 'Supervisor instance root'
        $logRoot = New-LocalDirectory -Root $repository -Path (Join-Path $instanceRoot 'logs') -Label 'Supervisor log root'
        $job = [WisdomeLocalJobNative]::CreateKillOnCloseJob()
        $context = [pscustomobject]@{
            RepositoryRoot = $repository
            InstanceId = $instanceId
            InstanceRoot = $instanceRoot
            StatePath = Join-Path $instanceRoot 'state.json'
            LogRoot = $logRoot
            Mutex = $mutex
            MutexOwned = $true
            JobHandle = $job
            Processes = New-Object System.Collections.ArrayList
            TerminalStatus = 'stopped'
            TerminalErrorCode = ''
        }
        Write-LocalSupervisorState -Context $context -Active $true -Status 'starting'
        return $context
    }
    catch {
        if ($acquired) {
            try { $mutex.ReleaseMutex() } catch { }
        }
        $mutex.Dispose()
        throw
    }
}

function Start-LocalOwnedProcess {
    [CmdletBinding()]
    param(
        [pscustomobject]$Context,
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory = '',
        [string]$StandardOutput = '',
        [string]$StandardError = ''
    )

    if ($Context.JobHandle -eq [IntPtr]::Zero) {
        throw 'The local supervisor job is not active.'
    }
    if (-not $StandardOutput) {
        $StandardOutput = Join-Path $Context.LogRoot "$Name.stdout.log"
    }
    if (-not $StandardError) {
        $StandardError = Join-Path $Context.LogRoot "$Name.stderr.log"
    }
    $startArguments = @{
        FilePath = $FilePath
        ArgumentList = $Arguments
        PassThru = $true
        WindowStyle = 'Hidden'
        RedirectStandardOutput = $StandardOutput
        RedirectStandardError = $StandardError
    }
    if ($WorkingDirectory) {
        $startArguments.WorkingDirectory = $WorkingDirectory
    }
    $process = Start-Process @startArguments
    try {
        [WisdomeLocalJobNative]::AssignProcess($Context.JobHandle, $process.Handle)
    }
    catch {
        $same = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
        if ($null -ne $same) {
            Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
            Wait-Process -Id $process.Id -Timeout 5 -ErrorAction SilentlyContinue
        }
        throw
    }
    $record = [pscustomobject]@{
        name = $Name
        pid = $process.Id
        creationTime = $process.StartTime.ToUniversalTime().ToString('o')
        stdout = $StandardOutput
        stderr = $StandardError
    }
    [void]$Context.Processes.Add($record)
    Write-LocalSupervisorState -Context $Context -Active $true -Status 'running'
    return $process
}

function Set-LocalSupervisorTerminalStatus {
    [CmdletBinding()]
    param([pscustomobject]$Context, [string]$Status, [string]$ErrorCode = '')

    $Context.TerminalStatus = $Status
    $Context.TerminalErrorCode = $ErrorCode
    Write-LocalSupervisorState -Context $Context -Active $true -Status $Status -ErrorCode $ErrorCode
    if (-not [WisdomeConsoleStopSignal]::WasHandled) {
        Update-LocalStopSignalContext -Context $Context
    }
}

function Set-LocalSupervisorRunning {
    [CmdletBinding()]
    param([pscustomobject]$Context)

    Write-LocalSupervisorState -Context $Context -Active $true -Status 'running'
    if (-not [WisdomeConsoleStopSignal]::WasHandled) {
        Update-LocalStopSignalContext -Context $Context
    }
}

function Exit-LocalSupervisor {
    [CmdletBinding()]
    param(
        [pscustomobject]$Context,
        [scriptblock]$CloseHandle = { param($handle) [WisdomeLocalJobNative]::CloseHandle($handle) },
        [int]$TimeoutSeconds = 10
    )

    $closed = $false
    if ($Context.JobHandle -ne [IntPtr]::Zero) {
        $closed = [bool](& $CloseHandle $Context.JobHandle)
        if ($closed) {
            $Context.JobHandle = [IntPtr]::Zero
        }
    }
    if (-not $closed) {
        Write-LocalSupervisorState -Context $Context -Active $true -Status 'cleanup_error' -ErrorCode 'JOB_CLOSE_FAILED'
        return $false
    }

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    $alive = @()
    do {
        $alive = @()
        foreach ($record in $Context.Processes) {
            $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
            if (
                $null -ne $process -and
                $process.StartTime.ToUniversalTime().ToString('o') -ceq $record.creationTime
            ) {
                $alive += $record.pid
            }
        }
        if ($alive.Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 50
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    if ($alive.Count -gt 0) {
        Write-LocalSupervisorState -Context $Context -Active $true -Status 'cleanup_error' -ErrorCode 'OWNED_PROCESS_REMAINS'
        return $false
    }

    Write-LocalSupervisorState -Context $Context -Active $false -Status $Context.TerminalStatus -ErrorCode $Context.TerminalErrorCode
    Release-LocalMutex -Context $Context
    return $true
}

function Enable-LocalStopSignal {
    [WisdomeConsoleStopSignal]::Install()
}

function Wait-LocalStopSignal {
    [CmdletBinding()]
    param([int]$Milliseconds)

    return [WisdomeConsoleStopSignal]::Wait($Milliseconds)
}

function Disable-LocalStopSignal {
    [WisdomeConsoleStopSignal]::Uninstall()
}

function Update-LocalStopSignalContext {
    [CmdletBinding()]
    param([pscustomobject]$Context)

    $processIds = [int[]]@($Context.Processes | ForEach-Object { [int]$_.pid })
    $creationTicks = [long[]]@(
        $Context.Processes | ForEach-Object {
            [DateTimeOffset]::Parse([string]$_.creationTime).UtcDateTime.Ticks
        }
    )
    $stopped = Get-LocalSupervisorStateJson -Context $Context -Active $false -Status $Context.TerminalStatus -ErrorCode $Context.TerminalErrorCode
    $error = Get-LocalSupervisorStateJson -Context $Context -Active $true -Status 'cleanup_error' -ErrorCode 'JOB_CLOSE_FAILED'
    [WisdomeConsoleStopSignal]::Configure(
        $Context.JobHandle,
        $Context.StatePath,
        $processIds,
        $creationTicks,
        $stopped,
        $error
    )
}

function Test-LocalStopSignalHandled {
    return [WisdomeConsoleStopSignal]::WasHandled
}

function Complete-LocalSupervisorAfterSignal {
    [CmdletBinding()]
    param([pscustomobject]$Context)

    if (-not [WisdomeConsoleStopSignal]::WasHandled) {
        return $false
    }
    $Context.JobHandle = [IntPtr]::Zero
    if (-not [WisdomeConsoleStopSignal]::Succeeded) {
        return $false
    }
    Release-LocalMutex -Context $Context
    return $true
}

Export-ModuleMember -Function @(
    'Assert-LocalRepositoryFile',
    'Enter-LocalSupervisor',
    'Start-LocalOwnedProcess',
    'Set-LocalSupervisorRunning',
    'Set-LocalSupervisorTerminalStatus',
    'Exit-LocalSupervisor',
    'Enable-LocalStopSignal',
    'Wait-LocalStopSignal',
    'Disable-LocalStopSignal',
    'Update-LocalStopSignalContext',
    'Test-LocalStopSignalHandled',
    'Complete-LocalSupervisorAfterSignal'
)
