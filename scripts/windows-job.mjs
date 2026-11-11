// A suspended child is assigned before any of its code runs. Closing the owning
// helper kills the whole descendant job. Both read-only probes and installers
// use this primitive; callers supply the fixed executable and argument list.
export const windowsJobProgram = `Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
public static class ReceptionProcessJob {
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  struct STARTUPINFO {
    public uint cb; public string lpReserved,lpDesktop,lpTitle;
    public uint dwX,dwY,dwXSize,dwYSize,dwXCountChars,dwYCountChars,dwFillAttribute,dwFlags;
    public ushort wShowWindow,cbReserved2; public IntPtr lpReserved2,hStdInput,hStdOutput,hStdError;
  }
  [StructLayout(LayoutKind.Sequential)] struct PROCESS_INFORMATION { public IntPtr process,thread; public uint processId,threadId; }
  [StructLayout(LayoutKind.Sequential)] struct BASIC_LIMIT {
    public long processTime,jobTime; public uint flags; public UIntPtr minimum,maximum;
    public uint processLimit; public UIntPtr affinity; public uint priority,scheduling;
  }
  [StructLayout(LayoutKind.Sequential)] struct IO_COUNTERS { public ulong readOps,writeOps,otherOps,readBytes,writeBytes,otherBytes; }
  [StructLayout(LayoutKind.Sequential)] struct EXTENDED_LIMIT {
    public BASIC_LIMIT basic; public IO_COUNTERS io; public UIntPtr processMemory,jobMemory,peakProcess,peakJob;
  }
  [StructLayout(LayoutKind.Sequential)] struct ACCOUNTING {
    public long userTime,kernelTime,periodUserTime,periodKernelTime;
    public uint pageFaults,totalProcesses,activeProcesses,terminatedProcesses;
  }
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr attributes,string name);
  [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int type,ref EXTENDED_LIMIT value,uint size);
  [DllImport("kernel32.dll",SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr job,int type,out ACCOUNTING value,uint size,IntPtr returned);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcess(string app,StringBuilder command,IntPtr processAttributes,IntPtr threadAttributes,bool inherit,uint flags,IntPtr environment,string directory,ref STARTUPINFO startup,out PROCESS_INFORMATION info);
  [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
  [DllImport("kernel32.dll",SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
  [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr process,out uint code);
  [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr process,uint code);
  [DllImport("kernel32.dll")] static extern bool TerminateJobObject(IntPtr job,uint code);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
  [DllImport("kernel32.dll",SetLastError=true)] static extern uint WaitForSingleObject(IntPtr handle,uint milliseconds);
  public static int Run(string executable,string arguments,int timeoutMilliseconds) {
    return Run(executable,arguments,timeoutMilliseconds,IntPtr.Zero);
  }
  public static int Run(string executable,string arguments,int timeoutMilliseconds,IntPtr owner) {
    IntPtr job=CreateJobObject(IntPtr.Zero,null); if(job==IntPtr.Zero) throw new Win32Exception();
    PROCESS_INFORMATION pi=new PROCESS_INFORMATION(); bool assigned=false;
    try {
      EXTENDED_LIMIT limits=new EXTENDED_LIMIT(); limits.basic.flags=0x2000;
      if(!SetInformationJobObject(job,9,ref limits,(uint)Marshal.SizeOf(typeof(EXTENDED_LIMIT)))) throw new Win32Exception();
      STARTUPINFO si=new STARTUPINFO(); si.cb=(uint)Marshal.SizeOf(typeof(STARTUPINFO));
      StringBuilder command=new StringBuilder("\\\""+executable+"\\\" "+arguments);
      if(!CreateProcess(executable,command,IntPtr.Zero,IntPtr.Zero,false,0x08000004,IntPtr.Zero,System.IO.Path.GetDirectoryName(executable),ref si,out pi)) throw new Win32Exception();
      if(!AssignProcessToJobObject(job,pi.process)) throw new Win32Exception(); assigned=true;
      if(ResumeThread(pi.thread)==0xffffffff) throw new Win32Exception();
      CloseHandle(pi.thread); pi.thread=IntPtr.Zero;
      Stopwatch clock=Stopwatch.StartNew();
      for(;;) {
        if(owner!=IntPtr.Zero && WaitForSingleObject(owner,0)!=0x102) throw new OperationCanceledException();
        ACCOUNTING state;
        if(!QueryInformationJobObject(job,1,out state,(uint)Marshal.SizeOf(typeof(ACCOUNTING)),IntPtr.Zero)) throw new Win32Exception();
        if(state.activeProcesses==0) break;
        if(clock.ElapsedMilliseconds>timeoutMilliseconds) { TerminateJobObject(job,1460); throw new TimeoutException(); }
        Thread.Sleep(100);
      }
      uint exitCode; if(!GetExitCodeProcess(pi.process,out exitCode)) throw new Win32Exception();
      return unchecked((int)exitCode);
    } finally {
      if(pi.process!=IntPtr.Zero && !assigned) TerminateProcess(pi.process,1);
      if(pi.thread!=IntPtr.Zero) CloseHandle(pi.thread);
      if(pi.process!=IntPtr.Zero) CloseHandle(pi.process);
      CloseHandle(job);
    }
  }
}
'@`;
export function windowsArgument(value) {
  return '"' + value.replace(/(\\*)"/g, '$1$1\\"').replace(/(\\+)$/g, '$1$1') + '"';
}
export function powershellLiteral(value) { return "'" + value.replace(/'/g, "''") + "'"; }
