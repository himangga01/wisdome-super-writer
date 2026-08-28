Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$reparseFlag = [System.IO.FileAttributes]::ReparsePoint

if (-not ('WisdomeProcessNative' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
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

    public bool HasExited()
    {
        if (Closed) return true;
        UInt32 result = WaitForSingleObject(handle, 0);
        if (result == WAIT_OBJECT_0) return true;
        if (result == WAIT_TIMEOUT) return false;
        throw new InvalidOperationException("PROCESS_WAIT_FAILED");
    }

    public bool WaitForExitAndClose(int milliseconds)
    {
        if (Closed) return true;
        UInt32 result = WaitForSingleObject(handle, (UInt32)Math.Max(milliseconds, 0));
        if (result == WAIT_TIMEOUT) return false;
        if (result == WAIT_FAILED) throw new InvalidOperationException("PROCESS_WAIT_FAILED");
        if (result != WAIT_OBJECT_0) throw new InvalidOperationException("PROCESS_WAIT_FAILED");
        if (!CloseHandle(handle)) throw new InvalidOperationException("PROCESS_CLOSE_FAILED");
        handle = IntPtr.Zero;
        return true;
    }

    public void MarkClosedExternally() { handle = IntPtr.Zero; }
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
    private const UInt32 WAIT_OBJECT_0 = 0;
    private const UInt32 WAIT_FAILED = UInt32.MaxValue;
    private static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);

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

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessTimes(IntPtr process, out FILETIME creation,
        out FILETIME exit, out FILETIME kernel, out FILETIME user);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern UInt32 GetFinalPathNameByHandle(IntPtr file, StringBuilder path,
        UInt32 length, UInt32 flags);

    private static void Fail(string code) { throw new InvalidOperationException(code); }

    private static void CheckedClose(ref IntPtr handle, string code)
    {
        if (handle == IntPtr.Zero || handle == INVALID_HANDLE_VALUE) return;
        if (!CloseHandle(handle)) Fail(code);
        handle = IntPtr.Zero;
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
        string stable = null;
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
            CheckedClose(ref stdin, "NATIVE_CLOSE_FAILED");
            CheckedClose(ref stdout, "NATIVE_CLOSE_FAILED");
            CheckedClose(ref stderr, "NATIVE_CLOSE_FAILED");

            if (fault == "assign" || fault == "terminate" || fault == "wait" ||
                fault == "close") Fail("NATIVE_" + fault.ToUpperInvariant() + "_FAILED");
            if (!AssignProcessToJobObject(job, process.ProcessHandle)) Fail("NATIVE_ASSIGN_FAILED");
            if (fault == "resume" || ResumeThread(process.ThreadHandle) == UInt32.MaxValue)
                Fail("NATIVE_RESUME_FAILED");
            CheckedClose(ref process.ThreadHandle, "NATIVE_CLOSE_FAILED");
            FILETIME creation, exit, kernel, user;
            if (!GetProcessTimes(process.ProcessHandle, out creation, out exit,
                out kernel, out user)) Fail("NATIVE_PROCESS_TIME_FAILED");
            return new WisdomeOwnedProcess(process.ProcessHandle, (int)process.ProcessId,
                DateTime.FromFileTimeUtc(creation.Ticks).Ticks);
        }
        catch (Exception error)
        {
            stable = error.Message.StartsWith("NATIVE_") ? error.Message : "NATIVE_CREATE_FAILED";
            if (created && process.ProcessHandle != IntPtr.Zero)
            {
                bool terminated = TerminateProcess(process.ProcessHandle, 1);
                if (!terminated || fault == "terminate") stable = "NATIVE_TERMINATE_FAILED";
                UInt32 waited = WaitForSingleObject(process.ProcessHandle, 5000);
                if (waited != WAIT_OBJECT_0 || fault == "wait") stable = "NATIVE_WAIT_FAILED";
                bool threadClosed = process.ThreadHandle == IntPtr.Zero || CloseHandle(process.ThreadHandle);
                process.ThreadHandle = IntPtr.Zero;
                bool processClosed = CloseHandle(process.ProcessHandle);
                process.ProcessHandle = IntPtr.Zero;
                if (!threadClosed || !processClosed || fault == "close") stable = "NATIVE_CLOSE_FAILED";
            }
            throw new InvalidOperationException(stable);
        }
        finally
        {
            if (listInitialized && !listDeleted) DeleteProcThreadAttributeList(list);
            if (handleValues != IntPtr.Zero) Marshal.FreeHGlobal(handleValues);
            if (list != IntPtr.Zero) Marshal.FreeHGlobal(list);
            if (stdin != IntPtr.Zero && stdin != INVALID_HANDLE_VALUE && !CloseHandle(stdin))
                Fail("NATIVE_CLOSE_FAILED");
            if (stdout != IntPtr.Zero && stdout != INVALID_HANDLE_VALUE && !CloseHandle(stdout))
                Fail("NATIVE_CLOSE_FAILED");
            if (stderr != IntPtr.Zero && stderr != INVALID_HANDLE_VALUE && !CloseHandle(stderr))
                Fail("NATIVE_CLOSE_FAILED");
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

    public static IntPtr CreateInheritableSentinel(string path)
    {
        return CreateInheritedFile(path, GENERIC_WRITE, CREATE_NEW);
    }

    public static void CloseTestHandle(IntPtr handle)
    {
        if (!CloseHandle(handle)) Fail("NATIVE_CLOSE_FAILED");
    }
}

public sealed class WisdomeCleanupProbe
{
    public int CallerCount { get; set; }
    public bool CleanupCompleted { get; set; }
    public int JobCloseCount { get; set; }
    public int OwnerCount { get; set; }
    public int ProcessCloseCount { get; set; }
    public bool Succeeded { get; set; }
}

public static class WisdomeConsoleStopSignal
{
    private enum ControlType : uint { CtrlC = 0, CtrlBreak = 1 }
    private delegate bool HandlerRoutine(ControlType value);
    private static readonly ManualResetEvent StopEvent = new ManualResetEvent(false);
    private static readonly ManualResetEvent CleanupCompletedEvent = new ManualResetEvent(false);
    private static readonly HandlerRoutine Handler = HandleControl;
    private static int stopRequested;
    private static int cleanupOwner;
    private static int ownerCount;
    private static int callerCount;
    private static int jobCloseCount;
    private static int processCloseCount;
    private static bool installed;
    private static bool configured;
    private static bool handled;
    private static bool succeeded;
    private static IntPtr job;
    private static IntPtr[] processHandles = new IntPtr[0];
    private static string statePath;
    private static string stoppedJson;
    private static string errorJson;
    private static string cleanupFault;
    private static string pauseBoundary;
    private static ManualResetEvent pauseReached;
    private static ManualResetEvent pauseRelease;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetConsoleCtrlHandler(HandlerRoutine handler, bool add);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern UInt32 WaitForSingleObject(IntPtr handle, UInt32 milliseconds);

    private static bool HandleControl(ControlType value)
    {
        if (value != ControlType.CtrlC && value != ControlType.CtrlBreak) return false;
        RequestStop();
        ExecuteStop();
        return true;
    }

    private static void Boundary(string value)
    {
        if (pauseBoundary == value && pauseReached != null)
        {
            pauseReached.Set();
            pauseRelease.WaitOne();
        }
    }

    private static void AtomicWrite(string path, string payload)
    {
        string directory = Path.GetDirectoryName(path);
        string temporary = Path.Combine(directory, ".signal-" + Guid.NewGuid().ToString("N") + ".tmp");
        string backup = Path.Combine(directory, ".signal-" + Guid.NewGuid().ToString("N") + ".backup");
        File.WriteAllText(temporary, payload, new UTF8Encoding(false));
        if (File.Exists(path))
        {
            File.Replace(temporary, path, backup);
            File.Delete(backup);
        }
        else File.Move(temporary, path);
    }

    private static string ErrorCodeForFault(string fault)
    {
        if (fault == "job-close") return "JOB_CLOSE_FAILED";
        if (fault == "process-wait") return "PROCESS_WAIT_FAILED";
        if (fault == "process-close") return "PROCESS_CLOSE_FAILED";
        if (fault == "state-write") return "STATE_WRITE_FAILED";
        return null;
    }

    private static string ErrorPayload(string code)
    {
        return errorJson.Replace("JOB_CLOSE_FAILED", code);
    }

    private static bool ExecuteStop()
    {
        Interlocked.Increment(ref callerCount);
        if (!configured || job == IntPtr.Zero)
        {
            if (!configured)
            {
                handled = true; succeeded = true; CleanupCompletedEvent.Set();
            }
            return succeeded;
        }
        if (Interlocked.CompareExchange(ref cleanupOwner, 1, 0) != 0)
        {
            CleanupCompletedEvent.WaitOne();
            return succeeded;
        }
        Interlocked.Increment(ref ownerCount);
        string code = null;
        try
        {
            Boundary("owner-acquired");
            Interlocked.Increment(ref jobCloseCount);
            bool jobClosed = cleanupFault != "job-close" && CloseHandle(job);
            if (!jobClosed) code = "JOB_CLOSE_FAILED";
            else job = IntPtr.Zero;
            Boundary("job-closed");
            if (code == null)
            {
                for (int index = 0; index < processHandles.Length; index++)
                {
                    UInt32 waited = cleanupFault == "process-wait" ? UInt32.MaxValue :
                        WaitForSingleObject(processHandles[index], 10000);
                    if (waited != 0) { code = "PROCESS_WAIT_FAILED"; break; }
                }
            }
            Boundary("processes-waited");
            if (code == null)
            {
                for (int index = 0; index < processHandles.Length; index++)
                {
                    bool closed = cleanupFault != "process-close" &&
                        CloseHandle(processHandles[index]);
                    if (!closed) { code = "PROCESS_CLOSE_FAILED"; break; }
                    processHandles[index] = IntPtr.Zero;
                    Interlocked.Increment(ref processCloseCount);
                }
            }
            bool stateWritten = false;
            try
            {
                if (cleanupFault == "state-write")
                    throw new IOException("injected");
                AtomicWrite(statePath, code == null ? stoppedJson : ErrorPayload(code));
                stateWritten = true;
            }
            catch
            {
                code = "STATE_WRITE_FAILED";
                try { AtomicWrite(statePath, ErrorPayload(code)); stateWritten = true; }
                catch { stateWritten = false; }
            }
            if (stateWritten) Boundary("state-written");
            succeeded = code == null && stateWritten;
            handled = stateWritten;
            return succeeded;
        }
        finally { CleanupCompletedEvent.Set(); }
    }

    private static void RequestStop()
    {
        Interlocked.Exchange(ref stopRequested, 1);
        StopEvent.Set();
    }

    public static void Install()
    {
        StopEvent.Reset(); CleanupCompletedEvent.Reset();
        stopRequested = cleanupOwner = ownerCount = callerCount = 0;
        jobCloseCount = processCloseCount = 0;
        configured = handled = succeeded = false;
        cleanupFault = pauseBoundary = null;
        if (!installed && !SetConsoleCtrlHandler(Handler, true))
            throw new InvalidOperationException("CONSOLE_HANDLER_INSTALL_FAILED");
        installed = true;
    }

    public static void Uninstall()
    {
        if (StopRequested && !CleanupCompletedEvent.WaitOne(15000))
            throw new InvalidOperationException("CLEANUP_NOT_COMPLETED");
        if (installed && !SetConsoleCtrlHandler(Handler, false))
            throw new InvalidOperationException("CONSOLE_HANDLER_REMOVE_FAILED");
        installed = false;
    }

    public static void ConfigureState(string path, string stopped, string error)
    {
        if (configured) throw new InvalidOperationException("SIGNAL_ALREADY_CONFIGURED");
        statePath = path; stoppedJson = stopped; errorJson = error; configured = true;
    }

    public static void UpdateState(string stopped, string error)
    { stoppedJson = stopped; errorJson = error; }
    public static void UpdateProcesses(IntPtr[] handles)
    { processHandles = handles ?? new IntPtr[0]; }
    public static void AssociateJob(IntPtr value)
    {
        job = value;
        if (StopRequested) ExecuteStop();
    }
    public static bool Wait(int milliseconds) { return StopEvent.WaitOne(milliseconds); }
    public static bool WaitForCleanup(int milliseconds)
    { return CleanupCompletedEvent.WaitOne(milliseconds); }
    public static bool StopRequested { get { return stopRequested != 0; } }
    public static bool CleanupIsCompleted { get { return CleanupCompletedEvent.WaitOne(0); } }
    public static bool WasHandled { get { return handled; } }
    public static bool Succeeded { get { return succeeded; } }
    public static void RequestStopForTest() { RequestStop(); }
    public static bool RunCleanupForTest() { return ExecuteStop(); }
    public static bool RunCleanup() { return ExecuteStop(); }
    public static void TriggerForTest() { RequestStop(); ExecuteStop(); }
    public static void SetCleanupFaultForTest(string value) { cleanupFault = value; }

    public static WisdomeCleanupProbe ExerciseCleanupInterleavingForTest(string boundary)
    {
        pauseBoundary = boundary;
        pauseReached = new ManualResetEvent(false);
        pauseRelease = new ManualResetEvent(false);
        RequestStop();
        Thread first = new Thread(() => ExecuteStop());
        Thread second = new Thread(() => ExecuteStop());
        first.Start();
        if (!pauseReached.WaitOne(5000)) throw new InvalidOperationException("BOUNDARY_NOT_REACHED");
        second.Start();
        Thread.Sleep(50);
        pauseRelease.Set();
        first.Join(); second.Join();
        return new WisdomeCleanupProbe {
            CallerCount = callerCount,
            CleanupCompleted = CleanupIsCompleted,
            JobCloseCount = jobCloseCount,
            OwnerCount = ownerCount,
            ProcessCloseCount = processCloseCount,
            Succeeded = succeeded
        };
    }
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
    try { $stream.Write($payload, 0, $payload.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }
    if ([IO.File]::Exists($Context.StatePath)) {
        $backup = $temporary + '.backup'; [IO.File]::Replace($temporary, $Context.StatePath, $backup)
        [IO.File]::Delete($backup)
    } else { [IO.File]::Move($temporary, $Context.StatePath) }
}

function Release-LocalMutex {
    param([pscustomobject]$Context)
    if ($Context.MutexOwned) { $Context.Mutex.ReleaseMutex(); $Context.MutexOwned = $false }
    $Context.Mutex.Dispose()
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
            Mutex=$mutex; MutexOwned=$true; JobHandle=$job
            Processes=(New-Object Collections.ArrayList)
            NativeProcesses=(New-Object Collections.ArrayList)
            SignalConfigured=$false; TerminalStatus='stopped'; TerminalErrorCode=''
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
    [WisdomeConsoleStopSignal]::AssociateJob($Context.JobHandle)
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
    if ($Context.SignalConfigured) { Update-LocalStopSignalContext $Context }
    return $process
}

function Set-LocalSupervisorTerminalStatus {
    [CmdletBinding()]
    param([pscustomobject]$Context,[string]$Status,[string]$ErrorCode='')
    $Context.TerminalStatus=$Status; $Context.TerminalErrorCode=$ErrorCode
    Write-LocalSupervisorState $Context $true $Status $ErrorCode
    if ($Context.SignalConfigured) { Update-LocalStopSignalContext $Context }
}

function Set-LocalSupervisorRunning {
    [CmdletBinding()] param([pscustomobject]$Context)
    Write-LocalSupervisorState $Context $true 'running'
    if ($Context.SignalConfigured) { Update-LocalStopSignalContext $Context }
}

function Write-CleanupError {
    param([pscustomobject]$Context,[string]$Code)
    try { Write-LocalSupervisorState $Context $true 'cleanup_error' $Code }
    catch { return $false }
    return $true
}

function Exit-LocalSupervisor {
    [CmdletBinding()]
    param([pscustomobject]$Context,
        [scriptblock]$CloseHandle={param($h)[WisdomeProcessNative]::CloseHandle($h)},
        [int]$TimeoutSeconds=10,
        [ValidateSet('','job-close','process-wait','process-close','state-write')]
        [string]$CleanupFault='')
    try {
        if ($Context.JobHandle -eq [IntPtr]::Zero -or
            $CleanupFault -eq 'job-close' -or
            -not [bool](& $CloseHandle $Context.JobHandle)) {
            [void](Write-CleanupError $Context 'JOB_CLOSE_FAILED'); return $false
        }
        $Context.JobHandle=[IntPtr]::Zero
        if($CleanupFault -eq 'process-wait'){
            [void](Write-CleanupError $Context 'PROCESS_WAIT_FAILED');return $false
        }
        if($CleanupFault -eq 'process-close'){
            [void](Write-CleanupError $Context 'PROCESS_CLOSE_FAILED');return $false
        }
        $remaining=$TimeoutSeconds*1000
        foreach($native in $Context.NativeProcesses) {
            $started=[Environment]::TickCount
            try { $done=$native.WaitForExitAndClose($remaining) }
            catch {
                $code=if($_.Exception.Message -match 'PROCESS_CLOSE'){'PROCESS_CLOSE_FAILED'}else{'PROCESS_WAIT_FAILED'}
                [void](Write-CleanupError $Context $code); return $false
            }
            if(-not $done){[void](Write-CleanupError $Context 'PROCESS_WAIT_FAILED');return $false}
            $remaining=[Math]::Max(0,$remaining-[Math]::Max(0,[Environment]::TickCount-$started))
        }
        if($CleanupFault -eq 'state-write'){
            [void](Write-CleanupError $Context 'STATE_WRITE_FAILED');return $false
        }
        try { Write-LocalSupervisorState $Context $false $Context.TerminalStatus $Context.TerminalErrorCode }
        catch { [void](Write-CleanupError $Context 'STATE_WRITE_FAILED'); return $false }
        Release-LocalMutex $Context; return $true
    } catch { [void](Write-CleanupError $Context 'CLEANUP_INTERNAL_FAILED'); return $false }
}

function Enable-LocalStopSignal {[WisdomeConsoleStopSignal]::Install()}
function Wait-LocalStopSignal { [CmdletBinding()]param([int]$Milliseconds)
    return [WisdomeConsoleStopSignal]::Wait($Milliseconds) }
function Disable-LocalStopSignal {[WisdomeConsoleStopSignal]::Uninstall()}

function Initialize-LocalStopSignalContext {
    [CmdletBinding()]param([pscustomobject]$Context)
    if($Context.SignalConfigured){throw 'Stop signal context is already configured.'}
    $stopped=Get-LocalSupervisorStateJson $Context $false $Context.TerminalStatus $Context.TerminalErrorCode
    $error=Get-LocalSupervisorStateJson $Context $true 'cleanup_error' 'JOB_CLOSE_FAILED'
    [WisdomeConsoleStopSignal]::ConfigureState($Context.StatePath,$stopped,$error)
    $Context.SignalConfigured=$true
    Update-LocalStopSignalContext $Context
}

function Update-LocalStopSignalContext {
    [CmdletBinding()]param([pscustomobject]$Context)
    if(-not $Context.SignalConfigured){Initialize-LocalStopSignalContext $Context;return}
    $stopped=Get-LocalSupervisorStateJson $Context $false $Context.TerminalStatus $Context.TerminalErrorCode
    $error=Get-LocalSupervisorStateJson $Context $true 'cleanup_error' 'JOB_CLOSE_FAILED'
    [WisdomeConsoleStopSignal]::UpdateState($stopped,$error)
    [WisdomeConsoleStopSignal]::UpdateProcesses(
        [IntPtr[]]@($Context.NativeProcesses|ForEach-Object{$_.NativeHandle}))
    if($Context.JobHandle-ne[IntPtr]::Zero){[WisdomeConsoleStopSignal]::AssociateJob($Context.JobHandle)}
}

function Test-LocalStopSignalHandled {return [WisdomeConsoleStopSignal]::WasHandled}
function Complete-LocalSupervisorAfterSignal {
    [CmdletBinding()]param([pscustomobject]$Context)
    if(-not [WisdomeConsoleStopSignal]::CleanupIsCompleted -and
        -not [WisdomeConsoleStopSignal]::WaitForCleanup(15000)){return $false}
    $Context.JobHandle=[IntPtr]::Zero
    if(-not [WisdomeConsoleStopSignal]::Succeeded){return $false}
    foreach($native in $Context.NativeProcesses){$native.MarkClosedExternally()}
    Release-LocalMutex $Context; return $true
}

Export-ModuleMember -Function @(
    'Assert-LocalRepositoryFile','Enter-LocalSupervisor','Initialize-LocalSupervisorJob',
    'Start-LocalOwnedProcess','Set-LocalSupervisorRunning',
    'Set-LocalSupervisorTerminalStatus','Exit-LocalSupervisor','Enable-LocalStopSignal',
    'Wait-LocalStopSignal','Disable-LocalStopSignal','Update-LocalStopSignalContext',
    'Initialize-LocalStopSignalContext','Test-LocalStopSignalHandled',
    'Complete-LocalSupervisorAfterSignal'
)
