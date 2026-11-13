import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import path from 'node:path';
import type { CredentialEncryption } from './translation-credentials';

const FAILURE = 'Windows 用户加密不可用。';
const LIMIT = 256 * 1024;

/** DPAPI CurrentUser is independent of Electron's per-userData Chromium key. */
async function crypt(action: 'Protect' | 'Unprotect', data: Buffer): Promise<Buffer> {
  if (process.platform !== 'win32' || data.length > LIMIT) throw new Error(FAILURE);
  const program = `$ErrorActionPreference='Stop';try{Add-Type -AssemblyName System.Security;[Console]::InputEncoding=[Text.UTF8Encoding]::new($false);[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$b=[Convert]::FromBase64String([Console]::In.ReadToEnd());$e=[Text.Encoding]::UTF8.GetBytes('ReceptionWorkbench.SharedTranslation.v1');$r=[Security.Cryptography.ProtectedData]::${action}($b,$e,[Security.Cryptography.DataProtectionScope]::CurrentUser);[Console]::Out.Write([Convert]::ToBase64String($r))}catch{exit 2}`;
  const env: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (['systemroot', 'windir', 'systemdrive', 'temp', 'tmp', 'path', 'pathext', 'comspec', 'userprofile', 'localappdata'].includes(key.toLowerCase())) env[key] = value;
  }
  return new Promise((resolve, reject) => {
    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe'),
        ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(program, 'utf16le').toString('base64')],
        { env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    } catch { reject(new Error(FAILURE)); return; }
    let finished = false;
    let bytes = 0;
    const chunks: Buffer[] = [];
    const timer = setTimeout(() => { child.kill(); finish(); }, 10000);
    function finish(value?: Buffer) {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      if (value) resolve(value); else reject(new Error(FAILURE));
    }
    child.stdin!.on('error', () => {});
    child.stderr!.resume();
    child.stdout!.on('data', (chunk: Buffer) => {
      bytes += chunk.length;
      if (bytes > LIMIT) { child.kill(); finish(); }
      else chunks.push(chunk);
    });
    child.once('error', () => finish());
    child.once('close', code => {
      if (finished) return;
      if (code !== 0) { finish(); return; }
      const encoded = Buffer.concat(chunks).toString('ascii').trim();
      if (!encoded || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) { finish(); return; }
      const decoded = Buffer.from(encoded, 'base64');
      if (decoded.toString('base64') !== encoded) { finish(); return; }
      finish(decoded);
    });
    // Secrets travel only in this owned pipe, never in arguments or environment.
    child.stdin!.end(data.toString('base64'));
  });
}

export function createWindowsUserEncryption(): CredentialEncryption {
  let cachedHash = '';
  let cachedValue: Promise<string> | undefined;
  return {
    isEncryptionAvailable: () => process.platform === 'win32',
    encryptString: value => crypt('Protect', Buffer.from(value, 'utf8')),
    decryptString: value => {
      const hash = createHash('sha256').update(value).digest('hex');
      if (hash === cachedHash && cachedValue) return cachedValue;
      cachedHash = hash;
      cachedValue = crypt('Unprotect', value).then(bytes => {
        try { return new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
        catch { throw new Error(FAILURE); }
      }).catch(() => {
        if (cachedHash === hash) { cachedHash = ''; cachedValue = undefined; }
        throw new Error(FAILURE);
      });
      return cachedValue;
    },
  };
}
