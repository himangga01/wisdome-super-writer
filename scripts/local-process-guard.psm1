Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$reparseFlag = [System.IO.FileAttributes]::ReparsePoint

if (-not ('WisdomeProcessNative' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using Microsoft.Win32.SafeHandles;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public sealed class WisdomeOwnedProcess
{
    private const UInt32 WAIT_OBJECT_0 = 0;
    private const UInt32 WAIT_TIMEOUT = 258;
    private const UInt32 WAIT_FAILED = UInt32.MaxValue;
    private IntPtr handle;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern UInt32 WaitForSingleObject(IntPtr handle, UInt32 milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    public WisdomeOwnedProcess(IntPtr value, int id, long creationTicks)
    {
        handle = value;
        Id = id;
        CreationTimeTicks = creationTicks;
    }

    public int Id { get; private set; }
    public long CreationTimeTicks { get; private set; }
    public IntPtr NativeHandle { get { return handle; } }
    public bool Closed { get { return handle == IntPtr.Zero; } }
    public int WaitAttemptCount { get; private set; }
    public int CloseAttemptCount { get; private set; }

    public bool HasExited()
    {
        if (Closed) return true;
        UInt32 result = WaitForSingleObject(handle, 0);
        if (result == WAIT_OBJECT_0) return true;
        if (result == WAIT_TIMEOUT) return false;
        throw new InvalidOperationException("PROCESS_WAIT_FAILED");
    }

    public bool WaitForExit(int milliseconds, bool fail)
    {
        if (Closed) return true;
        WaitAttemptCount++;
        UInt32 result = fail ? WAIT_FAILED :
            WaitForSingleObject(handle, (UInt32)Math.Max(milliseconds, 0));
        if (result == WAIT_TIMEOUT) return false;
        if (result == WAIT_FAILED) throw new InvalidOperationException("PROCESS_WAIT_FAILED");
        if (result != WAIT_OBJECT_0) throw new InvalidOperationException("PROCESS_WAIT_FAILED");
        return true;
    }

    public bool Close(bool fail)
    {
        if (Closed) return true;
        CloseAttemptCount++;
        if (fail || !CloseHandle(handle)) return false;
        handle = IntPtr.Zero;
        return true;
    }

    public bool WaitForExitAndClose(int milliseconds)
    {
        if (!WaitForExit(milliseconds, false)) return false;
        if (!Close(false)) throw new InvalidOperationException("PROCESS_CLOSE_FAILED");
        return true;
    }

    public void MarkClosedExternally() { handle = IntPtr.Zero; }
}

public sealed class WisdomeNativeCleanupProbe
{
    public WisdomeNativeCleanupProbe()
    {
        RetainedHandleKinds = new string[0];
    }

    public string FailureCode { get; set; }
    public int StandardInputCloseAttemptCount { get; set; }
    public int StandardOutputCloseAttemptCount { get; set; }
    public int StandardErrorCloseAttemptCount { get; set; }
    public int TerminateAttemptCount { get; set; }
    public int WaitAttemptCount { get; set; }
    public int ProcessCloseAttemptCount { get; set; }
    public int ThreadCloseAttemptCount { get; set; }
    public int RetainedHandleCount { get; set; }
    public string[] RetainedHandleKinds { get; set; }
}

public static class WisdomeProcessNative
{
    private const UInt32 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
    private const Int32 JobObjectExtendedLimitInformation = 9;
    private const UInt32 GENERIC_READ = 0x80000000;
    private const UInt32 GENERIC_WRITE = 0x40000000;
    private const UInt32 FILE_SHARE_READ = 0x00000001;
    private const UInt32 FILE_SHARE_WRITE = 0x00000002;
    private const UInt32 FILE_SHARE_DELETE = 0x00000004;
    private const UInt32 CREATE_NEW = 1;
    private const UInt32 OPEN_EXISTING = 3;
    private const UInt32 FILE_ATTRIBUTE_NORMAL = 0x00000080;
    private const UInt32 FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;
    private const UInt32 CREATE_SUSPENDED = 0x00000004;
    private const UInt32 EXTENDED_STARTUPINFO_PRESENT = 0x00080000;
    private const UInt32 CREATE_NO_WINDOW = 0x08000000;
    private const UInt32 STARTF_USESTDHANDLES = 0x00000100;
    private const UInt32 PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002;
    private const UInt32 MOVEFILE_REPLACE_EXISTING = 0x00000001;
    private const UInt32 MOVEFILE_WRITE_THROUGH = 0x00000008;
    private const UInt32 WAIT_OBJECT_0 = 0;
    private const UInt32 WAIT_FAILED = UInt32.MaxValue;
    private static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);
    private static readonly List<IntPtr> RetainedFailureHandles = new List<IntPtr>();

    public static WisdomeNativeCleanupProbe LastStartCleanupProbe { get; private set; }

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
    private struct STARTUPINFOEX
    {
        public STARTUPINFO StartupInfo;
        public IntPtr AttributeList;
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
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, Int32 kind, IntPtr info, UInt32 length);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFile(string name, UInt32 access, UInt32 share,
        ref SECURITY_ATTRIBUTES security, UInt32 creation, UInt32 flags, IntPtr template);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool InitializeProcThreadAttributeList(IntPtr list, Int32 count,
        UInt32 flags, ref IntPtr size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool UpdateProcThreadAttribute(IntPtr list, UInt32 flags,
        IntPtr attribute, IntPtr value, IntPtr size, IntPtr previous, IntPtr returned);

    [DllImport("kernel32.dll")]
    private static extern void DeleteProcThreadAttributeList(IntPtr list);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(string applicationName, StringBuilder commandLine,
        IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles,
        UInt32 creationFlags, IntPtr environment, string currentDirectory,
        ref STARTUPINFOEX startupInfo, out PROCESS_INFORMATION processInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern UInt32 ResumeThread(IntPtr thread);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, UInt32 exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern UInt32 WaitForSingleObject(IntPtr handle, UInt32 milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool MoveFileEx(string existingPath, string newPath, UInt32 flags);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessTimes(IntPtr process, out FILETIME creation,
        out FILETIME exit, out FILETIME kernel, out FILETIME user);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern UInt32 GetFinalPathNameByHandle(IntPtr file, StringBuilder path,
        UInt32 length, UInt32 flags);

    private static void Fail(string code) { throw new InvalidOperationException(code); }

    private static bool TryCloseOnce(ref IntPtr handle, bool fail, ref int attempts)
    {
        if (handle == IntPtr.Zero || handle == INVALID_HANDLE_VALUE) return true;
        if (attempts != 0) return false;
        attempts++;
        if (fail || !CloseHandle(handle)) return false;
        handle = IntPtr.Zero;
        return true;
    }

    private static void CheckedClose(ref IntPtr handle, string code, ref int attempts)
    {
        if (!TryCloseOnce(ref handle, false, ref attempts)) Fail(code);
    }

    private static IntPtr CreateInheritedFile(string path, UInt32 access, UInt32 creation)
    {
        SECURITY_ATTRIBUTES security = new SECURITY_ATTRIBUTES();
        security.Length = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
        security.InheritHandle = true;
        IntPtr handle = CreateFile(path, access, FILE_SHARE_READ, ref security, creation,
            FILE_ATTRIBUTE_NORMAL, IntPtr.Zero);
        if (handle == INVALID_HANDLE_VALUE) Fail("NATIVE_LOG_CREATE_FAILED");
        return handle;
    }

    public static IntPtr CreateKillOnCloseJob()
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) Fail("JOB_CREATE_FAILED");
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        IntPtr pointer = Marshal.AllocHGlobal(length);
        try
        {
            Marshal.StructureToPtr(info, pointer, false);
            if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                pointer, (UInt32)length))
            {
                if (!CloseHandle(job)) Fail("JOB_CLOSE_FAILED");
                Fail("JOB_CONFIGURE_FAILED");
            }
        }
        finally { Marshal.FreeHGlobal(pointer); }
        return job;
    }

    public static WisdomeOwnedProcess Start(string application, string commandLine,
        string workingDirectory, string stdoutPath, string stderrPath, IntPtr job, string fault)
    {
        IntPtr stdout = IntPtr.Zero, stderr = IntPtr.Zero, stdin = IntPtr.Zero;
        IntPtr list = IntPtr.Zero, handleValues = IntPtr.Zero;
        bool listInitialized = false, listDeleted = false, created = false;
        PROCESS_INFORMATION process = new PROCESS_INFORMATION();
        int stdinCloseAttempts = 0, stdoutCloseAttempts = 0, stderrCloseAttempts = 0;
        int processCloseAttempts = 0, threadCloseAttempts = 0;
        LastStartCleanupProbe = null;
        try
        {
            stdout = CreateInheritedFile(stdoutPath, GENERIC_WRITE, CREATE_NEW);
            stderr = CreateInheritedFile(stderrPath, GENERIC_WRITE, CREATE_NEW);
            stdin = CreateInheritedFile("NUL", GENERIC_READ, OPEN_EXISTING);
            IntPtr size = IntPtr.Zero;
            if (fault == "attribute-initialize") Fail("NATIVE_ATTRIBUTE_INITIALIZE_FAILED");
            bool first = InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);
            if (first || Marshal.GetLastWin32Error() != 122 || size == IntPtr.Zero)
                Fail("NATIVE_ATTRIBUTE_INITIALIZE_FAILED");
            list = Marshal.AllocHGlobal(size);
            if (!InitializeProcThreadAttributeList(list, 1, 0, ref size))
                Fail("NATIVE_ATTRIBUTE_INITIALIZE_FAILED");
            listInitialized = true;
            handleValues = Marshal.AllocHGlobal(IntPtr.Size * 3);
            Marshal.WriteIntPtr(handleValues, 0, stdin);
            Marshal.WriteIntPtr(handleValues, IntPtr.Size, stdout);
            Marshal.WriteIntPtr(handleValues, IntPtr.Size * 2, stderr);
            if (fault == "attribute-update" || !UpdateProcThreadAttribute(list, 0,
                new IntPtr(PROC_THREAD_ATTRIBUTE_HANDLE_LIST), handleValues,
                new IntPtr(IntPtr.Size * 3), IntPtr.Zero, IntPtr.Zero))
                Fail("NATIVE_ATTRIBUTE_UPDATE_FAILED");

            STARTUPINFOEX startup = new STARTUPINFOEX();
            startup.StartupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFOEX));
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
            startup.StartupInfo.hStdInput = stdin;
            startup.StartupInfo.hStdOutput = stdout;
            startup.StartupInfo.hStdError = stderr;
            startup.AttributeList = list;
            if (fault == "create") Fail("NATIVE_CREATE_FAILED");
            if (!CreateProcess(application, new StringBuilder(commandLine), IntPtr.Zero,
                IntPtr.Zero, true, CREATE_SUSPENDED | CREATE_NO_WINDOW |
                EXTENDED_STARTUPINFO_PRESENT, IntPtr.Zero, workingDirectory,
                ref startup, out process))
                Fail("NATIVE_CREATE_FAILED");
            created = true;

            DeleteProcThreadAttributeList(list);
            listDeleted = true;
            if (fault == "attribute-delete") Fail("NATIVE_ATTRIBUTE_DELETE_FAILED");
            Marshal.FreeHGlobal(list); list = IntPtr.Zero;
            Marshal.FreeHGlobal(handleValues); handleValues = IntPtr.Zero;
            CheckedClose(ref stdin, "NATIVE_CLOSE_FAILED", ref stdinCloseAttempts);
            CheckedClose(ref stdout, "NATIVE_CLOSE_FAILED", ref stdoutCloseAttempts);
            CheckedClose(ref stderr, "NATIVE_CLOSE_FAILED", ref stderrCloseAttempts);

            if (fault == "assign" || fault == "terminate" || fault == "wait" ||
                fault == "close" || !AssignProcessToJobObject(job, process.ProcessHandle))
                Fail("NATIVE_ASSIGN_FAILED");
            if (fault == "resume" || ResumeThread(process.ThreadHandle) == UInt32.MaxValue)
                Fail("NATIVE_RESUME_FAILED");
            CheckedClose(ref process.ThreadHandle, "NATIVE_CLOSE_FAILED", ref threadCloseAttempts);
            FILETIME creation, exit, kernel, user;
            if (!GetProcessTimes(process.ProcessHandle, out creation, out exit,
                out kernel, out user)) Fail("NATIVE_PROCESS_TIME_FAILED");
            return new WisdomeOwnedProcess(process.ProcessHandle, (int)process.ProcessId,
                DateTime.FromFileTimeUtc(creation.Ticks).Ticks);
        }
        catch (Exception error)
        {
            string stable = error.Message.StartsWith("NATIVE_") ?
                error.Message : "NATIVE_CREATE_FAILED";
            WisdomeNativeCleanupProbe probe = new WisdomeNativeCleanupProbe();
            List<string> retainedKinds = new List<string>();
            if (created && process.ProcessHandle != IntPtr.Zero)
            {
                probe.TerminateAttemptCount++;
                bool terminateResult = TerminateProcess(process.ProcessHandle, 1);
                if (fault == "terminate") terminateResult = false;
                if (!terminateResult) stable = "NATIVE_TERMINATE_FAILED";

                probe.WaitAttemptCount++;
                UInt32 waitResult = WaitForSingleObject(process.ProcessHandle, 5000);
                if (fault == "wait") waitResult = WAIT_FAILED;
                if (waitResult != WAIT_OBJECT_0 && stable != "NATIVE_TERMINATE_FAILED")
                    stable = "NATIVE_WAIT_FAILED";

                bool processClosed = TryCloseOnce(ref process.ProcessHandle,
                    fault == "close", ref processCloseAttempts);
                if (!processClosed)
                {
                    lock (RetainedFailureHandles)
                    { RetainedFailureHandles.Add(process.ProcessHandle); }
                    probe.RetainedHandleCount++;
                    retainedKinds.Add("process");
                    if (stable != "NATIVE_TERMINATE_FAILED" && stable != "NATIVE_WAIT_FAILED")
                        stable = "NATIVE_CLOSE_FAILED";
                }
                bool threadClosed = TryCloseOnce(ref process.ThreadHandle, false,
                    ref threadCloseAttempts);
                if (!threadClosed)
                {
                    lock (RetainedFailureHandles)
                    { RetainedFailureHandles.Add(process.ThreadHandle); }
                    probe.RetainedHandleCount++;
                    retainedKinds.Add("thread");
                    if (stable != "NATIVE_TERMINATE_FAILED" && stable != "NATIVE_WAIT_FAILED")
                        stable = "NATIVE_CLOSE_FAILED";
                }
            }
            if (!TryCloseOnce(ref stdin, false, ref stdinCloseAttempts))
            {
                lock (RetainedFailureHandles) { RetainedFailureHandles.Add(stdin); }
                probe.RetainedHandleCount++;
                retainedKinds.Add("stdin");
                stable = "NATIVE_CLOSE_FAILED";
            }
            if (!TryCloseOnce(ref stdout, false, ref stdoutCloseAttempts))
            {
                lock (RetainedFailureHandles) { RetainedFailureHandles.Add(stdout); }
                probe.RetainedHandleCount++;
                retainedKinds.Add("stdout");
                stable = "NATIVE_CLOSE_FAILED";
            }
            if (!TryCloseOnce(ref stderr, false, ref stderrCloseAttempts))
            {
                lock (RetainedFailureHandles) { RetainedFailureHandles.Add(stderr); }
                probe.RetainedHandleCount++;
                retainedKinds.Add("stderr");
                stable = "NATIVE_CLOSE_FAILED";
            }
            probe.FailureCode = stable;
            probe.StandardInputCloseAttemptCount = stdinCloseAttempts;
            probe.StandardOutputCloseAttemptCount = stdoutCloseAttempts;
            probe.StandardErrorCloseAttemptCount = stderrCloseAttempts;
            probe.ProcessCloseAttemptCount = processCloseAttempts;
            probe.ThreadCloseAttemptCount = threadCloseAttempts;
            probe.RetainedHandleKinds = retainedKinds.ToArray();
            LastStartCleanupProbe = probe;
            throw new InvalidOperationException(stable);
        }
        finally
        {
            if (listInitialized && !listDeleted) DeleteProcThreadAttributeList(list);
            if (handleValues != IntPtr.Zero) Marshal.FreeHGlobal(handleValues);
            if (list != IntPtr.Zero) Marshal.FreeHGlobal(list);
        }
    }

    public static string FinalDirectoryPath(string path)
    {
        SECURITY_ATTRIBUTES security = new SECURITY_ATTRIBUTES();
        security.Length = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
        IntPtr handle = CreateFile(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE |
            FILE_SHARE_DELETE, ref security, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS,
            IntPtr.Zero);
        if (handle == INVALID_HANDLE_VALUE) Fail("REPOSITORY_HANDLE_OPEN_FAILED");
        try
        {
            StringBuilder value = new StringBuilder(32768);
            UInt32 written = GetFinalPathNameByHandle(handle, value,
                (UInt32)value.Capacity, 0);
            if (written == 0 || written >= value.Capacity)
                Fail("REPOSITORY_FINAL_PATH_FAILED");
            string result = value.ToString();
            return result.StartsWith("\\\\?\\") ? result.Substring(4) : result;
        }
        finally { if (!CloseHandle(handle)) Fail("REPOSITORY_HANDLE_CLOSE_FAILED"); }
    }

    public static bool MoveFileReplaceWriteThrough(string source, string target)
    {
        return MoveFileEx(source, target,
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
    }

    public static bool CloseHandleForCleanup(IntPtr handle, bool fail)
    {
        return !fail && CloseHandle(handle);
    }

    public static IntPtr CreateInheritableSentinel(string path)
    {
        IntPtr handle = CreateInheritedFile(path, GENERIC_READ | GENERIC_WRITE, CREATE_NEW);
        SafeFileHandle safe = new SafeFileHandle(handle, false);
        using (FileStream stream = new FileStream(safe, FileAccess.ReadWrite))
        {
            stream.WriteByte(90);
            stream.Flush(true);
            stream.Position = 0;
        }
        return handle;
    }

    public static int ReadSentinelForTest(IntPtr handle)
    {
        SafeFileHandle safe = new SafeFileHandle(handle, false);
        using (FileStream stream = new FileStream(safe, FileAccess.Read))
        {
            stream.Position = 0;
            int value = stream.ReadByte();
            stream.Position = 0;
            return value;
        }
    }

    public static void CloseTestHandle(IntPtr handle)
    {
        if (!CloseHandle(handle)) Fail("NATIVE_CLOSE_FAILED");
    }
}

public static class WisdomeConsoleStopSignal
{
    private enum ControlType : uint { CtrlC = 0, CtrlBreak = 1 }
    private delegate bool HandlerRoutine(ControlType value);
    private static readonly ManualResetEvent StopEvent = new ManualResetEvent(false);
    private static readonly HandlerRoutine Handler = HandleControl;
    private static int stopRequested;
    private static bool installed;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetConsoleCtrlHandler(HandlerRoutine handler, bool add);

    private static bool HandleControl(ControlType value)
    {
        if (value != ControlType.CtrlC && value != ControlType.CtrlBreak) return false;
        RequestStop();
        return true;
    }

    private static void RequestStop()
    {
        Interlocked.Exchange(ref stopRequested, 1);
        StopEvent.Set();
    }

    public static void Install()
    {
        StopEvent.Reset();
        stopRequested = 0;
        if (!installed && !SetConsoleCtrlHandler(Handler, true))
            throw new InvalidOperationException("CONSOLE_HANDLER_INSTALL_FAILED");
        installed = true;
    }

    public static void Uninstall()
    {
        if (installed && !SetConsoleCtrlHandler(Handler, false))
            throw new InvalidOperationException("CONSOLE_HANDLER_REMOVE_FAILED");
        installed = false;
    }
    public static bool Wait(int milliseconds) { return StopEvent.WaitOne(milliseconds); }
    public static bool StopRequested { get { return stopRequested != 0; } }
    public static void RequestStopForTest() { RequestStop(); }
    public static void TriggerForTest() { RequestStop(); }
}
'@
}

function Assert-WindowsPlatform {
    if (-not [Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
        [Runtime.InteropServices.OSPlatform]::Windows
    )) { throw 'The local process supervisor requires Windows.' }
}

function Assert-LocalPathChain {
    param([string]$Root, [string]$Path, [string]$Label)
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $target = [IO.Path]::GetFullPath($Path)
    $prefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    if ($target -ne $rootFull -and
        -not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase))
    { throw "$Label escapes the repository root." }
    $current = $rootFull
    $relative = if ($target -eq $rootFull) { '' } else { $target.Substring($prefix.Length) }
    foreach ($part in @('') + @($relative -split '[\\/]' | Where-Object { $_ })) {
        if ($part) { $current = Join-Path $current $part }
        if ([IO.Directory]::Exists($current) -or [IO.File]::Exists($current)) {
            if (([IO.File]::GetAttributes($current) -band $script:reparseFlag) -ne 0)
            { throw "$Label contains a symlink, junction, or reparse point." }
        }
    }
    return $target
}

function New-LocalDirectory {
    param([string]$Root, [string]$Path, [string]$Label)
    $full = Assert-LocalPathChain $Root $Path $Label
    [void][IO.Directory]::CreateDirectory($full)
    [void](Assert-LocalPathChain $Root $full $Label)
    return $full
}

function Assert-LocalRepositoryFile {
    [CmdletBinding()]
    param([string]$RepositoryRoot, [string]$Path, [string]$Label)
    $full = Assert-LocalPathChain $RepositoryRoot $Path $Label
    if (-not [IO.File]::Exists($full)) { throw "$Label is missing." }
    $attributes = [IO.File]::GetAttributes($full)
    if (($attributes -band $script:reparseFlag) -ne 0 -or
        ($attributes -band [IO.FileAttributes]::Directory) -ne 0)
    { throw "$Label is not a safe regular file." }
    return $full
}

function Get-RepositoryMutexName {
    param([string]$RepositoryRoot)
    $bytes = [Text.Encoding]::UTF8.GetBytes($RepositoryRoot.ToLowerInvariant())
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = $sha.ComputeHash($bytes) } finally { $sha.Dispose() }
    return 'Global\WisdomeWriter-' + ([BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
}

function ConvertTo-LocalProcessArgument {
    param([string]$Value)
    if ($Value -and $Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"'); $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes += 1; continue }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * ($slashes * 2 + 1))); [void]$builder.Append('"')
        } else {
            [void]$builder.Append(('\' * $slashes)); [void]$builder.Append($character)
        }
        $slashes = 0
    }
    [void]$builder.Append(('\' * ($slashes * 2))); [void]$builder.Append('"')
    return $builder.ToString()
}

function Get-LocalSupervisorStateJson {
    param([pscustomobject]$Context, [bool]$Active, [string]$Status,
        [string]$ErrorCode = '')
    $state = [ordered]@{
        schemaVersion = 3; instanceId = $Context.InstanceId
        repositoryRoot = $Context.RepositoryRoot; active = $Active; status = $Status
        errorCode = if ($ErrorCode) { $ErrorCode } else { $null }
        updatedAt = [DateTimeOffset]::UtcNow.ToString('o'); processes = @($Context.Processes)
    }
    return $state | ConvertTo-Json -Depth 6
}

function Write-LocalSupervisorState {
    param([pscustomobject]$Context, [bool]$Active, [string]$Status,
        [string]$ErrorCode = '')
    $json = Get-LocalSupervisorStateJson $Context $Active $Status $ErrorCode
    $payload = [Text.Encoding]::UTF8.GetBytes($json)
    $temporary = Join-Path $Context.InstanceRoot ('.state-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $stream = New-Object IO.FileStream($temporary, [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $stream.Write($payload, 0, $payload.Length)
        $stream.Flush($true)
    }
    catch { throw 'STATE_WRITE_FAILED' }
    finally { $stream.Dispose() }
    if (-not [WisdomeProcessNative]::MoveFileReplaceWriteThrough(
        $temporary, $Context.StatePath
    )) { throw 'STATE_WRITE_FAILED' }
}

function Release-LocalMutex {
    param([pscustomobject]$Context)
    if ($Context.MutexOwned) { $Context.Mutex.ReleaseMutex(); $Context.MutexOwned = $false }
    if (-not $Context.MutexDisposed) {
        $Context.Mutex.Dispose()
        $Context.MutexDisposed = $true
    }
}

function Enter-LocalSupervisor {
    [CmdletBinding()]
    param([string]$RepositoryRoot, [switch]$DeferJob,
        [switch]$SimulateInitialStateFailure)
    Assert-WindowsPlatform
    $requested = [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
    if (-not [IO.Directory]::Exists($requested)) { throw 'Repository root is missing.' }
    if (([IO.File]::GetAttributes($requested) -band $script:reparseFlag) -ne 0)
    { throw 'Repository root cannot be a symlink, junction, or reparse point.' }
    $repository = [WisdomeProcessNative]::FinalDirectoryPath($requested).TrimEnd('\')
    $mutexName = Get-RepositoryMutexName $repository
    try { $mutex = New-Object Threading.Mutex($false, $mutexName) }
    catch { throw 'The Global repository supervisor mutex is unavailable.' }
    $acquired = $false; $job = [IntPtr]::Zero
    try {
        try { $acquired = $mutex.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { throw 'A local supervisor is already active for this repository.' }
        $stateRoot = New-LocalDirectory $repository (Join-Path $repository '.local\state\start-local') 'Supervisor state root'
        $instanceId = [Guid]::NewGuid().ToString('N')
        $instanceRoot = New-LocalDirectory $repository (Join-Path $stateRoot $instanceId) 'Supervisor instance root'
        $logRoot = New-LocalDirectory $repository (Join-Path $instanceRoot 'logs') 'Supervisor log root'
        if (-not $DeferJob) { $job = [WisdomeProcessNative]::CreateKillOnCloseJob() }
        $context = [pscustomobject]@{
            RepositoryRoot=$repository; PhysicalRepositoryRoot=$repository; MutexName=$mutexName
            InstanceId=$instanceId; InstanceRoot=$instanceRoot
            StatePath=(Join-Path $instanceRoot 'state.json'); LogRoot=$logRoot
            Mutex=$mutex; MutexOwned=$true; MutexDisposed=$false; JobHandle=$job
            Processes=(New-Object Collections.ArrayList)
            NativeProcesses=(New-Object Collections.ArrayList)
            TerminalStatus='stopped'; TerminalErrorCode=''
            CleanupStarted=$false; CleanupCompleted=$false; CleanupSucceeded=$false
            CleanupErrorCode=''; JobCloseAttempts=0; CleanupStateWriteAttempts=0
        }
        if ($SimulateInitialStateFailure) { throw 'Injected initial state failure.' }
        Write-LocalSupervisorState $context $true 'starting'
        return $context
    } catch {
        $initialFailure = $_
        $jobCloseFailed = $false
        if ($job -ne [IntPtr]::Zero -and -not [WisdomeProcessNative]::CloseHandle($job))
        { $jobCloseFailed = $true }
        if ($acquired) { try { $mutex.ReleaseMutex() } catch { } }
        $mutex.Dispose()
        if ($jobCloseFailed) { throw 'JOB_CLOSE_FAILED' }
        throw $initialFailure
    }
}

function Initialize-LocalSupervisorJob {
    [CmdletBinding()]
    param([pscustomobject]$Context)
    if ($Context.JobHandle -ne [IntPtr]::Zero) { return }
    $Context.JobHandle = [WisdomeProcessNative]::CreateKillOnCloseJob()
}

function Assert-InstanceLogPath {
    param([pscustomobject]$Context, [string]$Path, [string]$Label)
    $root = [IO.Path]::GetFullPath($Context.LogRoot).TrimEnd('\')
    $full = [IO.Path]::GetFullPath($Path); $prefix = $root + [IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase))
    { throw "$Label must remain inside the unique owned instance log root." }
    [void](Assert-LocalPathChain $Context.RepositoryRoot $full $Label)
    if ([IO.File]::Exists($full) -or [IO.Directory]::Exists($full))
    { throw "$Label must be created with FileMode.CreateNew." }
    return $full
}

function Start-LocalOwnedProcess {
    [CmdletBinding()]
    param([pscustomobject]$Context, [string]$Name, [string]$FilePath,
        [string[]]$Arguments, [string]$WorkingDirectory='', [string]$StandardOutput='',
        [string]$StandardError='', [switch]$SimulateAssignmentFailure,
        [string]$NativeFault='')
    if ($Context.JobHandle -eq [IntPtr]::Zero) { throw 'The local supervisor job is not active.' }
    if (-not $StandardOutput) { $StandardOutput = Join-Path $Context.LogRoot "$Name.stdout.log" }
    if (-not $StandardError) { $StandardError = Join-Path $Context.LogRoot "$Name.stderr.log" }
    $StandardOutput = Assert-InstanceLogPath $Context $StandardOutput 'Standard output log'
    $StandardError = Assert-InstanceLogPath $Context $StandardError 'Standard error log'
    if ($StandardOutput -eq $StandardError) { throw 'Standard output and error logs must be distinct.' }
    $application = (Get-Command $FilePath -CommandType Application -ErrorAction Stop).Source
    $commandLine = (@($application) + $Arguments |
        ForEach-Object { ConvertTo-LocalProcessArgument $_ }) -join ' '
    $directory = if ($WorkingDirectory) { $WorkingDirectory } else { $Context.RepositoryRoot }
    $fault = if ($SimulateAssignmentFailure) { 'assign' } else { $NativeFault }
    $process = [WisdomeProcessNative]::Start($application, $commandLine, $directory,
        $StandardOutput, $StandardError, $Context.JobHandle, $fault)
    [void]$Context.NativeProcesses.Add($process)
    $record = [pscustomobject]@{
        name=$Name; pid=$process.Id
        creationTime=([DateTime]::new($process.CreationTimeTicks,
            [DateTimeKind]::Utc)).ToString('o')
        stdout=$StandardOutput; stderr=$StandardError
    }
    [void]$Context.Processes.Add($record)
    Write-LocalSupervisorState $Context $true 'running'
    return $process
}

function Set-LocalSupervisorTerminalStatus {
    [CmdletBinding()]
    param([pscustomobject]$Context,[string]$Status,[string]$ErrorCode='')
    $Context.TerminalStatus=$Status; $Context.TerminalErrorCode=$ErrorCode
    Write-LocalSupervisorState $Context $true $Status $ErrorCode
}

function Set-LocalSupervisorRunning {
    [CmdletBinding()] param([pscustomobject]$Context)
    Write-LocalSupervisorState $Context $true 'running'
}

function Exit-LocalSupervisor {
    [CmdletBinding()]
    param([pscustomobject]$Context,
        [scriptblock]$CloseHandle={param($h)[WisdomeProcessNative]::CloseHandleForCleanup($h,$false)},
        [int]$TimeoutSeconds=10,
        [ValidateSet('','job-close','process-wait','process-close','state-write')]
        [string[]]$CleanupFault=@())
    if ($Context.CleanupCompleted) { return [bool]$Context.CleanupSucceeded }
    if ($Context.CleanupStarted) { return $false }
    $Context.CleanupStarted=$true
    $faults=@($CleanupFault)
    $resourceError=''

    if ($Context.JobHandle -ne [IntPtr]::Zero) {
        $Context.JobCloseAttempts += 1
        $jobClosed=$false
        try {
            if ($faults -contains 'job-close') {
                $jobClosed=[WisdomeProcessNative]::CloseHandleForCleanup($Context.JobHandle,$true)
            } else {
                $jobClosed=[bool](& $CloseHandle $Context.JobHandle)
            }
        } catch { $jobClosed=$false }
        if ($jobClosed) { $Context.JobHandle=[IntPtr]::Zero }
        elseif (-not $resourceError) { $resourceError='JOB_CLOSE_FAILED' }
    }

    $remaining=[Math]::Max(0,$TimeoutSeconds*1000)
    foreach($native in $Context.NativeProcesses) {
        $started=[Environment]::TickCount
        $waited=$false
        try {
            $waited=$native.WaitForExit(
                $remaining, [bool]($faults -contains 'process-wait')
            )
        } catch { $waited=$false }
        if (-not $waited -and -not $resourceError) { $resourceError='PROCESS_WAIT_FAILED' }
        $elapsed=[Math]::Max(0,[Environment]::TickCount-$started)
        $remaining=[Math]::Max(0,$remaining-$elapsed)

        $processClosed=$false
        try {
            $processClosed=$native.Close([bool]($faults -contains 'process-close'))
        } catch { $processClosed=$false }
        if (-not $processClosed -and -not $resourceError) { $resourceError='PROCESS_CLOSE_FAILED' }
    }

    $stateFailure=$false
    $stateWritten=$false
    $Context.CleanupStateWriteAttempts += 1
    try {
        if ($faults -contains 'state-write') { throw 'STATE_WRITE_FAILED' }
        if ($resourceError) {
            Write-LocalSupervisorState $Context $true 'cleanup_error' $resourceError
        } else {
            Write-LocalSupervisorState $Context $false $Context.TerminalStatus $Context.TerminalErrorCode
        }
        $stateWritten=$true
    } catch { $stateFailure=$true }

    if (-not $stateWritten) {
        $fallbackCode=if($resourceError){$resourceError}else{'STATE_WRITE_FAILED'}
        $Context.CleanupStateWriteAttempts += 1
        try {
            Write-LocalSupervisorState $Context $true 'cleanup_error' $fallbackCode
            $stateWritten=$true
        } catch { $stateWritten=$false }
    }

    $cleanupCode=if($resourceError){$resourceError}elseif($stateFailure){'STATE_WRITE_FAILED'}else{''}
    $mutexReleased=$true
    try { Release-LocalMutex $Context }
    catch { $mutexReleased=$false; if(-not $cleanupCode){$cleanupCode='CLEANUP_INTERNAL_FAILED'} }
    $Context.CleanupErrorCode=$cleanupCode
    $Context.CleanupSucceeded=(-not $cleanupCode -and $stateWritten -and $mutexReleased)
    $Context.CleanupCompleted=$true
    return [bool]$Context.CleanupSucceeded
}

function Enable-LocalStopSignal {[WisdomeConsoleStopSignal]::Install()}
function Wait-LocalStopSignal { [CmdletBinding()]param([int]$Milliseconds)
    return [WisdomeConsoleStopSignal]::Wait($Milliseconds) }
function Disable-LocalStopSignal {[WisdomeConsoleStopSignal]::Uninstall()}

Export-ModuleMember -Function @(
    'Assert-LocalRepositoryFile','Enter-LocalSupervisor','Initialize-LocalSupervisorJob',
    'Start-LocalOwnedProcess','Set-LocalSupervisorRunning',
    'Set-LocalSupervisorTerminalStatus','Exit-LocalSupervisor','Enable-LocalStopSignal',
    'Wait-LocalStopSignal','Disable-LocalStopSignal'
)
