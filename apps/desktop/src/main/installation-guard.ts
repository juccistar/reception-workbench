import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash } from 'node:crypto';
import { realpath } from 'node:fs/promises';
import path from 'node:path';

// Volume GUID paths resolve drive aliases, junctions and case without relying on a
// mutable drive-letter mapping. Unsupported filesystems fail closed.
export const physicalPathProgram = `
if(-not ('ReceptionPhysicalPath' -as [type])) {
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
public static class ReceptionPhysicalPath {
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern SafeFileHandle CreateFile(string p,uint a,uint s,IntPtr sa,uint c,uint f,IntPtr t);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern uint GetFinalPathNameByHandle(SafeFileHandle h,StringBuilder p,uint n,uint flags);
  public static string Resolve(string p) {
    using(var h=CreateFile(p,0,7,IntPtr.Zero,3,0,IntPtr.Zero)) {
      if(h.IsInvalid) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
      var b=new StringBuilder(32768); uint n=GetFinalPathNameByHandle(h,b,(uint)b.Capacity,1);
      if(n==0 || n>=b.Capacity) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
      string value=b.ToString();
      if(!value.StartsWith(@"\\\\?\\Volume{",StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("Unsupported volume");
      return value.ToLowerInvariant();
    }
  }
}
'@
}`;

export async function canonicalInstallationPath(executable: string): Promise<string> {
  if (!path.isAbsolute(executable)) throw new Error('安装程序路径无效。');
  if (process.platform !== 'win32') return path.normalize(await realpath(executable)).toLowerCase();
  const encoded = Buffer.from(executable, 'utf8').toString('base64');
  const program = `$ErrorActionPreference='Stop';${physicalPathProgram};[Console]::Out.WriteLine([ReceptionPhysicalPath]::Resolve([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${encoded}'))))`;
  const shell = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe');
  const { stdout } = await promisify(execFile)(shell, ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(program, 'utf16le').toString('base64')], { windowsHide: true, timeout: 15000 });
  const result = stdout.trim();
  if (!/^\\\\\?\\volume\{[0-9a-f-]+\}\\/i.test(result)) throw new Error('无法确定安装程序的物理路径。');
  return result;
}

/** Identical physical executable paths share the installation startup/update gate. */
export async function installationGateName(executable: string): Promise<string> {
  const physical = await canonicalInstallationPath(executable);
  return 'Global\\ReceptionInstallation_' + createHash('sha256').update(physical).digest('hex');
}

export class InstallationGuardError extends Error {
  constructor(public code: 'installation_busy' | 'installation_guard_unavailable', message: string) { super(message); this.name = 'InstallationGuardError'; }
}

export async function acquireInstallationStartGuard(executable: string): Promise<() => Promise<void>> {
  if (process.platform !== 'win32') throw new Error('安装启动保护仅支持 Windows。');
  const name = await installationGateName(executable);
  const program = `$ErrorActionPreference='Stop';$m=[Threading.Mutex]::new($false,'${name}');$owned=$false;try{try{$owned=$m.WaitOne(0)}catch [Threading.AbandonedMutexException]{$owned=$true};if(-not $owned){[Console]::Out.WriteLine('busy');exit 2};[Console]::Out.WriteLine('acquired');[Console]::Out.Flush();[void][Console]::ReadLine()}finally{if($owned){$m.ReleaseMutex()};$m.Dispose()}`;
  const child = spawn(path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe'), ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(program, 'utf16le').toString('base64')], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  child.stdin.on('error', () => {}); child.stderr.resume();
  let exited = false;
  const exit = new Promise<void>(resolve => child.once('close', () => { exited = true; resolve(); }));
  await new Promise<void>((resolve, reject) => {
    let settled = false; let output = '';
    const fail = (message: string, code: 'installation_busy' | 'installation_guard_unavailable' = 'installation_guard_unavailable') => { if (settled) return; settled = true; clearTimeout(timer); child.stdin.end(); child.kill(); reject(new InstallationGuardError(code, message)); };
    const timer = setTimeout(() => fail('安装启动保护未就绪。'), 15000);
    child.once('error', () => fail('安装启动保护无法启动。'));
    child.once('close', () => fail('安装启动保护已退出。'));
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => {
      if (settled) return;
      output += chunk;
      if (output.length > 1024) return fail('安装启动保护响应无效。');
      if (!output.includes('\n')) return;
      if (output.trim() !== 'acquired') return fail('安装程序正在更新，请稍后重试。', output.trim() === 'busy' ? 'installation_busy' : 'installation_guard_unavailable');
      settled = true; clearTimeout(timer); resolve();
    });
  });
  let release: Promise<void> | undefined;
  return () => release ||= (async () => { if (!exited) child.stdin.end('\n'); await exit; })();
}
