import { spawn } from 'node:child_process';
import { mkdtemp, readFile, writeFile, lstat, realpath, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { windowsJobProgram, windowsArgument, powershellLiteral } from './windows-job.mjs';

const equalPath = (a, b) => process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b;
async function safeExisting(value, directory) {
  if (typeof value !== 'string' || !path.isAbsolute(value) || /[\x00-\x1f]/.test(value) || /^(?:\\\\|\/\/)/.test(value)) throw new Error('identity_probe_invalid');
  const target = path.resolve(value);
  let cursor = path.parse(target).root;
  for (const part of target.slice(cursor.length).split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    const info = await lstat(cursor);
    if (info.isSymbolicLink() || !equalPath(await realpath(cursor), cursor)) throw new Error('identity_probe_invalid');
  }
  const info = await lstat(target);
  if (directory ? !info.isDirectory() : !info.isFile()) throw new Error('identity_probe_invalid');
  return target;
}

// Only the fixed native identity command runs. The Windows job owns descendants
// before their first instruction, so timeout/parent failure cannot leave a probe
// process behind. Authorization is inherited in memory, never serialized.
export async function probeNativeSeller({ cli, env, tempRoot = os.tmpdir(), timeoutMs = 150000 }) {
  if (!env || typeof env !== 'object' || !Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 150000) throw new Error('identity_probe_invalid');
  cli = await safeExisting(cli, false); tempRoot = await safeExisting(tempRoot, true);
  const directory = await mkdtemp(path.join(tempRoot, 'reception-identity-'));
  const output = path.join(directory, 'identity.json');
  try {
    const args = ['workflow', 'chat-analysis', 'resolve-id', '--output-file', output, '--format', 'json'];
    let executable = cli, commandArgs = args;
    if (process.platform === 'win32') {
      const payload = Buffer.from(JSON.stringify({ executable: cli, arguments: args.map(windowsArgument).join(' '), timeout: timeoutMs, parent: process.pid, parentStarted: Date.now() - process.uptime() * 1000 }), 'utf8').toString('base64');
      const program = `$ErrorActionPreference='Stop'\n${windowsJobProgram}\n$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${payload}'))|ConvertFrom-Json\n$parent=$null\ntry{$parent=Get-Process -Id $p.parent -ErrorAction Stop;$started=([DateTimeOffset]$parent.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds();if([Math]::Abs($started-$p.parentStarted) -gt 2000){exit 2};$code=[ReceptionProcessJob]::Run($p.executable,$p.arguments,$p.timeout,$parent.Handle);if($code -ne 0){exit 2};[Console]::Out.WriteLine('ok');exit 0}catch{if($_.Exception -is [TimeoutException] -or $_.Exception.InnerException -is [TimeoutException]){exit 124};exit 2}finally{if($parent){$parent.Dispose()}}`;
      const script = path.join(directory, 'probe.ps1');
      await writeFile(script, program, { encoding: 'utf8', flag: 'wx', mode: 0o600 });
      const invoke = '& ([ScriptBlock]::Create([IO.File]::ReadAllText(' + powershellLiteral(script) + ',[Text.Encoding]::UTF8)))';
      executable = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe');
      commandArgs = ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(invoke, 'utf16le').toString('base64')];
    }
    await new Promise((resolve, reject) => {
      const child = spawn(executable, commandArgs, { env, cwd: directory, windowsHide: true, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'] });
      let size = 0, reason, settled = false;
      const finish = error => { if (settled) return; settled = true; clearTimeout(timer); error ? reject(error) : resolve(); };
      const stop = error => {
        reason ||= error;
        if (process.platform !== 'win32' && child.pid) { try { process.kill(-child.pid, 'SIGKILL'); } catch {} }
        else child.kill();
      };
      const timer = setTimeout(() => stop(new Error('identity_probe_timeout')), timeoutMs + (process.platform === 'win32' ? 15000 : 0));
      for (const stream of [child.stdout, child.stderr]) stream.on('data', chunk => { size += chunk.length; if (size > 65536) stop(new Error('identity_probe_output_limit')); });
      child.once('error', () => finish(new Error('identity_probe_failed')));
      child.once('close', code => finish(reason || (code === 124 ? new Error('identity_probe_timeout') : code === 0 ? null : new Error('identity_probe_failed'))));
    });
    const info = await lstat(output);
    if (!info.isFile() || info.isSymbolicLink() || info.size > 65536) throw new Error('identity_probe_invalid');
    const bytes = await readFile(output);
    if (bytes.length > 65536) throw new Error('identity_probe_invalid');
    let value; try { value = JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/, '')); } catch { throw new Error('identity_probe_invalid'); }
    const candidate = value?.CURRENT_SELF_ALIID;
    const seller = typeof candidate === 'string' ? candidate : Number.isSafeInteger(candidate) && candidate > 0 ? String(candidate) : '';
    if (!/^[1-9][0-9]{0,127}$/.test(seller)) throw new Error('identity_probe_invalid');
    return seller;
  } finally { await rm(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
}
