// @vitest-environment node
import { it, expect } from 'vitest';
import { execFileSync } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { resolveWindowsUserLocalAppData } from '../src/main/windows-user-directories';
import { resolveModelSettingsDirectory } from '../src/main/model-settings-store';
import { resolveSharedTranslationDirectory } from '../src/main/shared-translation';

it.skipIf(process.platform !== 'win32')('uses the current token folder when profile variables are redirected', async () => {
  const keys = ['USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'HOME'] as const;
  const original = Object.fromEntries(keys.map(k => [k, process.env[k]]));
  const override = 'E:\\AccioMultiOpen\\instances\\A\\home';
  const ps = (script: string) => execFileSync(path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe'), ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')], { encoding: 'utf8', windowsHide: true });
  const oldResolver = () => ps('[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);[Console]::Out.Write([Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData))');
  const profileResolver = () => ps(`$ErrorActionPreference='Stop'; Add-Type -TypeDefinition @'
using System; using System.Text; using System.Runtime.InteropServices;
public static class ProfileProbe {
[DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
[DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr p, uint a, out IntPtr t);
[DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
[DllImport("userenv.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool GetUserProfileDirectory(IntPtr t, StringBuilder b, ref uint n);
public static string Get() { IntPtr t; if(!OpenProcessToken(GetCurrentProcess(),8,out t)) throw new Exception(); try { uint n=0; GetUserProfileDirectory(t,null,ref n); var b=new StringBuilder((int)n); if(!GetUserProfileDirectory(t,b,ref n)) throw new Exception(); return b.ToString(); } finally { CloseHandle(t); } }
}
'@; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); [Console]::Out.Write([ProfileProbe]::Get())`);
  try {
    const legacyNormal = oldResolver();
    const profileNormal = profileResolver();
    const normal = await resolveWindowsUserLocalAppData();
    process.env.USERPROFILE = override;
    process.env.HOME = override;
    process.env.LOCALAPPDATA = path.join(override, 'AppData', 'Local');
    process.env.APPDATA = path.join(override, 'AppData', 'Roaming');
    const legacyRedirected = oldResolver();
    const profileRedirected = profileResolver();
    const redirected = await resolveWindowsUserLocalAppData();
    expect(profileRedirected.toLowerCase()).toBe(profileNormal.toLowerCase());
    const models = await resolveModelSettingsDirectory();
    const translation = await resolveSharedTranslationDirectory('live');
    expect(legacyRedirected.toLowerCase()).not.toBe(legacyNormal.toLowerCase());
    expect(redirected.toLowerCase()).toBe(normal.toLowerCase());
    expect(models).toBe(path.join(normal, 'AlibabaReceptionWorkbenchShared', 'models'));
    expect(translation).toBe(path.join(normal, 'AlibabaReceptionWorkbenchShared', 'translation', 'live'));
    const dir = path.resolve('artifacts/verification/model-directory-fixed-040');
    await mkdir(dir, { recursive: true });
    await writeFile(path.join(dir, 'report.json'), JSON.stringify({ legacy_normal: legacyNormal, legacy_redirected: legacyRedirected, token_profile_normal: profileNormal, token_profile_redirected: profileRedirected, token_known_folder_normal: normal, token_known_folder_redirected: redirected, models, translation, same_token_folder: normal.toLowerCase() === redirected.toLowerCase(), scope: 'Current process token and synthetic child environment; no real settings read.' }, null, 2) + '\n');
  } finally {
    for (const k of keys) { if (original[k] === undefined) delete process.env[k]; else process.env[k] = original[k]; }
  }
}, 60000);
