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

if (-not ('WisdomeSuspendedLauncher' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

public sealed class WisdomeOwnedProcess
{
    private const UInt32 WAIT_OBJECT_0 = 0;
    private const UInt32 WAIT_TIMEOUT = 258;
    private IntPtr handle;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern UInt32 WaitForSingleObject(IntPtr handle, UInt32 milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, UInt32 exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    public WisdomeOwnedProcess(IntPtr processHandle, int processId, long creationTimeTicks)
    {
        handle = processHandle;
        Id = processId;
        CreationTimeTicks = creationTimeTicks;
    }

    public int Id { get; private set; }
    public long CreationTimeTicks { get; private set; }
    public IntPtr NativeHandle { get { return handle; } }
    public bool Closed { get { return handle == IntPtr.Zero; } }

    public bool HasExited()
    {
        if (Closed)
            return true;
        UInt32 result = WaitForSingleObject(handle, 0);
        if (result == WAIT_OBJECT_0)
            return true;
        if (result == WAIT_TIMEOUT)
            return false;
        throw new Win32Exception(Marshal.GetLastWin32Error(), "WaitForSingleObject failed");
    }

    public bool WaitForExitAndClose(int milliseconds)
    {
        if (Closed)
            return true;
        UInt32 result = WaitForSingleObject(handle, (UInt32)Math.Max(milliseconds, 0));
        if (result == WAIT_TIMEOUT)
            return false;
        if (result != WAIT_OBJECT_0)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "WaitForSingleObject failed");
        if (!CloseHandle(handle))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "process CloseHandle failed");
        handle = IntPtr.Zero;
        return true;
    }

    public void MarkClosedExternally()
    {
        handle = IntPtr.Zero;
    }

    public void TerminateAndClose()
    {
        if (Closed)
            return;
        TerminateProcess(handle, 1);
        WaitForSingleObject(handle, 5000);
        CloseHandle(handle);
        handle = IntPtr.Zero;
    }
}

public static class WisdomeSuspendedLauncher
{
    private const UInt32 GENERIC_WRITE = 0x40000000;
    private const UInt32 FILE_SHARE_READ = 0x00000001;
    private const UInt32 CREATE_NEW = 1;
    private const UInt32 FILE_ATTRIBUTE_NORMAL = 0x00000080;
    private const UInt32 CREATE_SUSPENDED = 0x00000004;
    private const UInt32 CREATE_NO_WINDOW = 0x08000000;
    private const UInt32 STARTF_USESTDHANDLES = 0x00000100;
    private const UInt32 HANDLE_FLAG_INHERIT = 0x00000001;
    private const UInt32 WAIT_OBJECT_0 = 0;
    private static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);

    [StructLayout(LayoutKind.Sequential)]
    private struct SECURITY_ATTRIBUTES
    {
        public Int32 Length;
        public IntPtr SecurityDescriptor;
        [MarshalAs(UnmanagedType.Bool)] public bool InheritHandle;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFO
    {
        public Int32 cb;
        public string lpReserved;
        public string lpDesktop;
        public string lpTitle;
        public UInt32 dwX;
        public UInt32 dwY;
        public UInt32 dwXSize;
        public UInt32 dwYSize;
        public UInt32 dwXCountChars;
        public UInt32 dwYCountChars;
        public UInt32 dwFillAttribute;
        public UInt32 dwFlags;
        public UInt16 wShowWindow;
        public UInt16 cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput;
        public IntPtr hStdOutput;
        public IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION
    {
        public IntPtr ProcessHandle;
        public IntPtr ThreadHandle;
        public UInt32 ProcessId;
        public UInt32 ThreadId;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FILETIME
    {
        public UInt32 Low;
        public UInt32 High;
        public long Ticks { get { return ((long)High << 32) + Low; } }
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFile(
        string name,
        UInt32 access,
        UInt32 share,
        ref SECURITY_ATTRIBUTES security,
        UInt32 creation,
        UInt32 flags,
        IntPtr template
    );

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateProcess(
        string applicationName,
        StringBuilder commandLine,
        IntPtr processAttributes,
        IntPtr threadAttributes,
        [MarshalAs(UnmanagedType.Bool)] bool inheritHandles,
        UInt32 creationFlags,
        IntPtr environment,
        string currentDirectory,
        ref STARTUPINFO startupInfo,
        out PROCESS_INFORMATION processInformation
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern UInt32 ResumeThread(IntPtr thread);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, UInt32 exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern UInt32 WaitForSingleObject(IntPtr handle, UInt32 milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessTimes(
        IntPtr process,
        out FILETIME creation,
        out FILETIME exit,
        out FILETIME kernel,
        out FILETIME user
    );

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetStdHandle(Int32 standardHandle);

    private static IntPtr CreateLog(string path)
    {
        SECURITY_ATTRIBUTES security = new SECURITY_ATTRIBUTES();
        security.Length = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
        security.InheritHandle = true;
        IntPtr handle = CreateFile(
            path,
            GENERIC_WRITE,
            FILE_SHARE_READ,
            ref security,
            CREATE_NEW,
            FILE_ATTRIBUTE_NORMAL,
            IntPtr.Zero
        );
        if (handle == INVALID_HANDLE_VALUE)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "log CreateNew failed");
        return handle;
    }

    public static WisdomeOwnedProcess Start(
        string application,
        string commandLine,
        string workingDirectory,
        string standardOutput,
        string standardError,
        IntPtr job,
        bool simulateAssignmentFailure
    )
    {
        IntPtr stdout = IntPtr.Zero;
        IntPtr stderr = IntPtr.Zero;
        PROCESS_INFORMATION process = new PROCESS_INFORMATION();
        bool processCreated = false;
        try
        {
            stdout = CreateLog(standardOutput);
            stderr = CreateLog(standardError);
            STARTUPINFO startup = new STARTUPINFO();
            startup.cb = Marshal.SizeOf(typeof(STARTUPINFO));
            startup.dwFlags = STARTF_USESTDHANDLES;
            startup.hStdInput = GetStdHandle(-10);
            startup.hStdOutput = stdout;
            startup.hStdError = stderr;
            if (!CreateProcess(
                application,
                new StringBuilder(commandLine),
                IntPtr.Zero,
                IntPtr.Zero,
                true,
                CREATE_SUSPENDED | CREATE_NO_WINDOW,
                IntPtr.Zero,
                workingDirectory,
                ref startup,
                out process
            ))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateProcessW failed");
            processCreated = true;
            CloseHandle(stdout);
            stdout = IntPtr.Zero;
            CloseHandle(stderr);
            stderr = IntPtr.Zero;

            if (simulateAssignmentFailure)
                throw new InvalidOperationException("Injected assignment failure.");
            if (!AssignProcessToJobObject(job, process.ProcessHandle))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Job assignment failed");
            if (ResumeThread(process.ThreadHandle) == UInt32.MaxValue)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "ResumeThread failed");
            CloseHandle(process.ThreadHandle);
            process.ThreadHandle = IntPtr.Zero;
            FILETIME creation, exit, kernel, user;
            if (!GetProcessTimes(process.ProcessHandle, out creation, out exit, out kernel, out user))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "GetProcessTimes failed");
            return new WisdomeOwnedProcess(
                process.ProcessHandle,
                (int)process.ProcessId,
                DateTime.FromFileTimeUtc(creation.Ticks).Ticks
            );
        }
        catch
        {
            if (processCreated && process.ProcessHandle != IntPtr.Zero)
            {
                TerminateProcess(process.ProcessHandle, 1);
                WaitForSingleObject(process.ProcessHandle, 5000);
            }
            if (process.ThreadHandle != IntPtr.Zero)
                CloseHandle(process.ThreadHandle);
            if (process.ProcessHandle != IntPtr.Zero)
                CloseHandle(process.ProcessHandle);
            throw;
        }
        finally
        {
            if (stdout != IntPtr.Zero && stdout != INVALID_HANDLE_VALUE)
                CloseHandle(stdout);
            if (stderr != IntPtr.Zero && stderr != INVALID_HANDLE_VALUE)
                CloseHandle(stderr);
        }
    }

    private const UInt32 FILE_SHARE_WRITE = 0x00000002;
    private const UInt32 FILE_SHARE_DELETE = 0x00000004;
    private const UInt32 OPEN_EXISTING = 3;
    private const UInt32 FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern UInt32 GetFinalPathNameByHandle(
        IntPtr file,
        StringBuilder path,
        UInt32 length,
        UInt32 flags
    );

    public static string FinalDirectoryPath(string path)
    {
        SECURITY_ATTRIBUTES security = new SECURITY_ATTRIBUTES();
        security.Length = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
        IntPtr handle = CreateFile(
            path,
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            ref security,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            IntPtr.Zero
        );
        if (handle == INVALID_HANDLE_VALUE)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "repository directory open failed");
        try
        {
            StringBuilder value = new StringBuilder(32768);
            UInt32 written = GetFinalPathNameByHandle(handle, value, (UInt32)value.Capacity, 0);
            if (written == 0 || written >= value.Capacity)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "GetFinalPathNameByHandle failed");
            string result = value.ToString();
            return result.StartsWith("\\\\?\\") ? result.Substring(4) : result;
        }
        finally
        {
            CloseHandle(handle);
        }
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
    private static bool stopRequested = false;
    private static bool configured = false;
    private static IntPtr job = IntPtr.Zero;
    private static string statePath = null;
    private static IntPtr[] processHandles = new IntPtr[0];
    private static string stoppedJson = null;
    private static string errorJson = null;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetConsoleCtrlHandler(HandlerRoutine handler, bool add);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern UInt32 WaitForSingleObject(IntPtr handle, UInt32 milliseconds);

    private static bool HandleControl(ControlType controlType)
    {
        if (controlType != ControlType.CtrlC && controlType != ControlType.CtrlBreak)
            return false;
        RequestStop();
        return true;
    }

    private static bool DirectProcessesExited()
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(10);
        while (DateTime.UtcNow < deadline)
        {
            bool anyAlive = false;
            for (int index = 0; index < processHandles.Length; index++)
            {
                if (processHandles[index] == IntPtr.Zero)
                    continue;
                UInt32 result = WaitForSingleObject(processHandles[index], 0);
                if (result == 258)
                    anyAlive = true;
                else if (result != 0)
                    anyAlive = true;
            }
            if (!anyAlive)
            {
                for (int index = 0; index < processHandles.Length; index++)
                {
                    if (processHandles[index] != IntPtr.Zero)
                    {
                        CloseHandle(processHandles[index]);
                        processHandles[index] = IntPtr.Zero;
                    }
                }
                return true;
            }
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
            if (!stopRequested || job == IntPtr.Zero)
            {
                Stop.Set();
                return;
            }
            handled = true;
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
        stopRequested = false;
        configured = false;
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

    public static void ConfigureState(
        string configuredStatePath,
        string configuredStoppedJson,
        string configuredErrorJson
    )
    {
        lock (Sync)
        {
            if (configured)
                throw new InvalidOperationException("stop signal context is already configured");
            statePath = configuredStatePath;
            stoppedJson = configuredStoppedJson;
            errorJson = configuredErrorJson;
            configured = true;
            if (stopRequested)
                ExecuteStop();
        }
    }

    public static void UpdateState(string configuredStoppedJson, string configuredErrorJson)
    {
        lock (Sync)
        {
            stoppedJson = configuredStoppedJson;
            errorJson = configuredErrorJson;
        }
    }

    public static void UpdateProcesses(IntPtr[] configuredProcessHandles)
    {
        lock (Sync)
        {
            processHandles = configuredProcessHandles ?? new IntPtr[0];
            if (stopRequested)
                ExecuteStop();
        }
    }

    public static void AssociateJob(IntPtr configuredJob)
    {
        lock (Sync)
        {
            job = configuredJob;
            if (stopRequested)
                ExecuteStop();
        }
    }

    private static void RequestStop()
    {
        stopRequested = true;
        Stop.Set();
        ExecuteStop();
    }

    public static bool WasHandled { get { return handled; } }
    public static bool Succeeded { get { return succeeded; } }

    public static void TriggerForTest()
    {
        RequestStop();
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
    return "Global\WisdomeWriter-$hex"
}

function ConvertTo-LocalProcessArgument {
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

function Assert-InstanceLogPath {
    param([pscustomobject]$Context, [string]$Path, [string]$Label)

    $root = [IO.Path]::GetFullPath($Context.LogRoot).TrimEnd('\')
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = $root + [IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must remain inside the unique owned instance log root."
    }
    [void](Assert-LocalPathChain -Root $Context.RepositoryRoot -Path $full -Label $Label)
    if ([IO.File]::Exists($full) -or [IO.Directory]::Exists($full)) {
        throw "$Label must be created with FileMode.CreateNew."
    }
    return $full
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
    param(
        [string]$RepositoryRoot,
        [switch]$DeferJob,
        [switch]$SimulateInitialStateFailure
    )

    Assert-WindowsPlatform
    $requested = [System.IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
    if (-not [System.IO.Directory]::Exists($requested)) {
        throw 'Repository root is missing.'
    }
    $requestedAttributes = [IO.File]::GetAttributes($requested)
    if (($requestedAttributes -band $script:reparseFlag) -ne 0) {
        throw 'Repository root cannot be a symlink, junction, or reparse point.'
    }
    $repository = [WisdomeSuspendedLauncher]::FinalDirectoryPath($requested).TrimEnd('\')
    [void](Assert-LocalPathChain -Root $repository -Path $repository -Label 'Repository root')
    $mutexName = Get-RepositoryMutexName $repository
    try {
        $mutex = New-Object System.Threading.Mutex($false, $mutexName)
    }
    catch {
        throw 'The Global repository supervisor mutex is unavailable.'
    }
    $acquired = $false
    $job = [IntPtr]::Zero
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
        if (-not $DeferJob) {
            $job = [WisdomeLocalJobNative]::CreateKillOnCloseJob()
        }
        $context = [pscustomobject]@{
            RepositoryRoot = $repository
            PhysicalRepositoryRoot = $repository
            MutexName = $mutexName
            InstanceId = $instanceId
            InstanceRoot = $instanceRoot
            StatePath = Join-Path $instanceRoot 'state.json'
            LogRoot = $logRoot
            Mutex = $mutex
            MutexOwned = $true
            JobHandle = $job
            Processes = New-Object System.Collections.ArrayList
            NativeProcesses = New-Object System.Collections.ArrayList
            SignalConfigured = $false
            TerminalStatus = 'stopped'
            TerminalErrorCode = ''
        }
        if ($SimulateInitialStateFailure) {
            throw 'Injected initial state failure.'
        }
        Write-LocalSupervisorState -Context $context -Active $true -Status 'starting'
        return $context
    }
    catch {
        if ($job -ne [IntPtr]::Zero) {
            [void][WisdomeLocalJobNative]::CloseHandle($job)
            $job = [IntPtr]::Zero
        }
        if ($acquired) {
            try { $mutex.ReleaseMutex() } catch { }
        }
        $mutex.Dispose()
        throw
    }
}

function Initialize-LocalSupervisorJob {
    [CmdletBinding()]
    param([pscustomobject]$Context)

    if ($Context.JobHandle -ne [IntPtr]::Zero) {
        return
    }
    $job = [WisdomeLocalJobNative]::CreateKillOnCloseJob()
    $Context.JobHandle = $job
    [WisdomeConsoleStopSignal]::AssociateJob($job)
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
        [string]$StandardError = '',
        [switch]$SimulateAssignmentFailure
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
    $StandardOutput = Assert-InstanceLogPath -Context $Context -Path $StandardOutput -Label 'Standard output log'
    $StandardError = Assert-InstanceLogPath -Context $Context -Path $StandardError -Label 'Standard error log'
    if ($StandardOutput -eq $StandardError) {
        throw 'Standard output and error logs must be distinct.'
    }
    $application = (Get-Command $FilePath -CommandType Application -ErrorAction Stop).Source
    $commandLine = (@($application) + $Arguments | ForEach-Object { ConvertTo-LocalProcessArgument $_ }) -join ' '
    $directory = if ($WorkingDirectory) { $WorkingDirectory } else { $Context.RepositoryRoot }
    $process = [WisdomeSuspendedLauncher]::Start(
        $application,
        $commandLine,
        $directory,
        $StandardOutput,
        $StandardError,
        $Context.JobHandle,
        [bool]$SimulateAssignmentFailure
    )
    [void]$Context.NativeProcesses.Add($process)
    $record = [pscustomobject]@{
        name = $Name
        pid = $process.Id
        creationTime = ([DateTime]::new($process.CreationTimeTicks, [DateTimeKind]::Utc)).ToString('o')
        stdout = $StandardOutput
        stderr = $StandardError
    }
    [void]$Context.Processes.Add($record)
    Write-LocalSupervisorState -Context $Context -Active $true -Status 'running'
    if ('WisdomeConsoleStopSignal' -as [type]) {
        Update-LocalStopSignalContext -Context $Context
    }
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

    $remainingMilliseconds = $TimeoutSeconds * 1000
    foreach ($native in $Context.NativeProcesses) {
        $started = [Environment]::TickCount
        if (-not $native.WaitForExitAndClose($remainingMilliseconds)) {
            Write-LocalSupervisorState -Context $Context -Active $true -Status 'cleanup_error' -ErrorCode 'OWNED_PROCESS_REMAINS'
            return $false
        }
        $elapsed = [Math]::Max(0, [Environment]::TickCount - $started)
        $remainingMilliseconds = [Math]::Max(0, $remainingMilliseconds - $elapsed)
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

    if (-not $Context.SignalConfigured) {
        Initialize-LocalStopSignalContext -Context $Context
        return
    }
    $stopped = Get-LocalSupervisorStateJson -Context $Context -Active $false -Status $Context.TerminalStatus -ErrorCode $Context.TerminalErrorCode
    $error = Get-LocalSupervisorStateJson -Context $Context -Active $true -Status 'cleanup_error' -ErrorCode 'JOB_CLOSE_FAILED'
    [WisdomeConsoleStopSignal]::UpdateState($stopped, $error)
    $handles = [IntPtr[]]@($Context.NativeProcesses | ForEach-Object { $_.NativeHandle })
    [WisdomeConsoleStopSignal]::UpdateProcesses($handles)
    if ($Context.JobHandle -ne [IntPtr]::Zero) {
        [WisdomeConsoleStopSignal]::AssociateJob($Context.JobHandle)
    }
}

function Initialize-LocalStopSignalContext {
    [CmdletBinding()]
    param([pscustomobject]$Context)

    if ($Context.SignalConfigured) {
        throw 'Stop signal context is already configured.'
    }
    $stopped = Get-LocalSupervisorStateJson -Context $Context -Active $false -Status $Context.TerminalStatus -ErrorCode $Context.TerminalErrorCode
    $error = Get-LocalSupervisorStateJson -Context $Context -Active $true -Status 'cleanup_error' -ErrorCode 'JOB_CLOSE_FAILED'
    [WisdomeConsoleStopSignal]::ConfigureState($Context.StatePath, $stopped, $error)
    $Context.SignalConfigured = $true
    $handles = [IntPtr[]]@($Context.NativeProcesses | ForEach-Object { $_.NativeHandle })
    [WisdomeConsoleStopSignal]::UpdateProcesses($handles)
    if ($Context.JobHandle -ne [IntPtr]::Zero) {
        [WisdomeConsoleStopSignal]::AssociateJob($Context.JobHandle)
    }
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
    foreach ($native in $Context.NativeProcesses) {
        $native.MarkClosedExternally()
    }
    Release-LocalMutex -Context $Context
    return $true
}

Export-ModuleMember -Function @(
    'Assert-LocalRepositoryFile',
    'Enter-LocalSupervisor',
    'Initialize-LocalSupervisorJob',
    'Start-LocalOwnedProcess',
    'Set-LocalSupervisorRunning',
    'Set-LocalSupervisorTerminalStatus',
    'Exit-LocalSupervisor',
    'Enable-LocalStopSignal',
    'Wait-LocalStopSignal',
    'Disable-LocalStopSignal',
    'Update-LocalStopSignalContext',
    'Initialize-LocalStopSignalContext',
    'Test-LocalStopSignalHandled',
    'Complete-LocalSupervisorAfterSignal'
)
