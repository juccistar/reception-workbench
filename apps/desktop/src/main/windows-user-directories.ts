import { execFile } from 'node:child_process';
import path from 'node:path';
import { promisify } from 'node:util';

// Resolve against this process's Windows access token, not redirected profile variables.
const resolver = String.raw`
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CurrentUserLocalFolder {
  [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
  [DllImport("advapi32.dll", SetLastError=true)] [return: MarshalAs(UnmanagedType.Bool)]
  static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
  [DllImport("kernel32.dll", SetLastError=true)] [return: MarshalAs(UnmanagedType.Bool)]
  static extern bool CloseHandle(IntPtr handle);
  [DllImport("shell32.dll")] static extern int SHGetKnownFolderPath(ref Guid id, uint flags, IntPtr token, out IntPtr result);
  [DllImport("userenv.dll", CharSet=CharSet.Unicode, SetLastError=true)] [return: MarshalAs(UnmanagedType.Bool)]
  static extern bool GetUserProfileDirectory(IntPtr token, System.Text.StringBuilder profile, ref uint size);
  public static string Profile() {
    IntPtr token = IntPtr.Zero;
    try {
      if (!OpenProcessToken(GetCurrentProcess(), 8, out token)) throw new System.ComponentModel.Win32Exception();
      uint size = 0;
      GetUserProfileDirectory(token, null, ref size);
      if (size == 0 || size > 32768) throw new InvalidOperationException();
      var profile = new System.Text.StringBuilder((int)size);
      if (!GetUserProfileDirectory(token, profile, ref size)) throw new System.ComponentModel.Win32Exception();
      return profile.ToString();
    } finally { if (token != IntPtr.Zero) CloseHandle(token); }
  }
  public static string Resolve() {
    IntPtr token = IntPtr.Zero, result = IntPtr.Zero;
    try {
      if (!OpenProcessToken(GetCurrentProcess(), 8, out token)) throw new System.ComponentModel.Win32Exception();
      Guid id = new Guid("F1B32785-6FBA-4FCF-9D55-7B8E7F157091");
      int hr = SHGetKnownFolderPath(ref id, 0x4000, token, out result);
      if (hr != 0 || result == IntPtr.Zero) throw new InvalidOperationException();
      return Marshal.PtrToStringUni(result);
    } finally { if (result != IntPtr.Zero) Marshal.FreeCoTaskMem(result); if (token != IntPtr.Zero) CloseHandle(token); }
  }
}
'@
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::Out.Write([CurrentUserLocalFolder]::Resolve())
`;

export async function resolveWindowsUserLocalAppData(): Promise<string> {
  if (process.platform !== 'win32') throw new Error('windows_user_directory_unavailable');
  try {
    const executable = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe');
    const run = (script: string, env: NodeJS.ProcessEnv) => promisify(execFile)(executable, ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')], { env, windowsHide: true, timeout: 15000, maxBuffer: 4096, encoding: 'utf8' });
    const safe = (value: string) => path.isAbsolute(value) && !/^(?:\\\\|\/\/)/.test(value) && !/[\x00-\x1f\x7f]/.test(value);
    const inherited: NodeJS.ProcessEnv = {};
    for (const [key, value] of Object.entries(process.env)) {
      if (['systemroot', 'windir', 'systemdrive', 'temp', 'tmp', 'path', 'pathext', 'comspec'].includes(key.toLowerCase())) inherited[key] = value;
    }
    const profile = (await run(resolver.replace('[CurrentUserLocalFolder]::Resolve()', '[CurrentUserLocalFolder]::Profile()'), inherited)).stdout;
    if (!safe(profile)) throw new Error();
    const env = { ...inherited, USERPROFILE: profile, LOCALAPPDATA: path.join(profile, 'AppData', 'Local'), APPDATA: path.join(profile, 'AppData', 'Roaming') };
    const { stdout } = await run(resolver, env);
    if (!safe(stdout)) throw new Error();
    return path.resolve(stdout);
  } catch { throw new Error('windows_user_directory_unavailable'); }
}
