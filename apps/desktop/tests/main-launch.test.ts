import { describe, expect, it } from 'vitest';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { buildAccioLaunchInfo, detachedPowershellCommand, powershellLiteral, windowsArgument } from '../src/main/launch-command';

describe('authorized terminal launch command', () => {
  it('generates installed and moved portable instructions from the actual executable and keeps managed data unpinned', () => {
    for (const distribution of ['installed', 'portable'] as const) {
      const executable = "E:\\中文 空格\\O'Reilly $(literal)\\ReceptionWorkbench.exe";
      const info = buildAccioLaunchInfo({ version: '0.2.18', distribution, executable });
      expect(info.executable_path).toBe(executable); expect(info.distribution).toBe(distribution);
      expect(info.command).toBe(detachedPowershellCommand(executable, ['--connect-from-accio']));
      expect(info.command).not.toContain('--data-dir'); expect(info.command).not.toContain('index.js');
      expect(info.instructions).toBe('请仅在允许启动此工作台的当前目标商家 Accio Work 授权 PowerShell 会话中执行以下命令。\n\n'
        + info.command + '\n\n该命令启动本机当前版本后立即返回，请勿等待桌面进程退出。');
    }
  });
  it('keeps the source entry and omits workspace bindings', () => {
    const executable = 'electron.exe', developmentEntry = 'source-entry.js';
    const info = buildAccioLaunchInfo({ version: '0.2.18', distribution: 'development', executable, developmentEntry });
    expect(info.command).toBe(detachedPowershellCommand(executable, [developmentEntry, '--connect-from-accio']));
    expect(info.instructions.split('\n\n')).toEqual([
      '请仅在允许启动此工作台的当前目标商家 Accio Work 授权 PowerShell 会话中执行以下命令。',
      info.command,
      '该命令启动本机当前版本后立即返回，请勿等待桌面进程退出。',
    ]);
  });
  it('quotes literal PowerShell strings and trailing Windows backslashes', () => {
    expect(powershellLiteral("O'Reilly $(literal)")).toBe("'O''Reilly $(literal)'");
    expect(windowsArgument('C:\\space folder\\')).toBe('"C:\\space folder\\\\"');
    expect(windowsArgument('a"b')).toBe('"a\\"b"');
    expect(detachedPowershellCommand('C:\\Tools\\app.exe', ['--connect-from-accio'])).toBe('Start-Process -FilePath \'C:\\Tools\\app.exe\' -ArgumentList \'"--connect-from-accio"\'');
  });

  it.skipIf(process.platform !== 'win32')('runs the real PowerShell command without waiting for the child or expanding argument text', async () => {
    const root = mkdtempSync(path.join(os.tmpdir(), 'reception-powershell-'));
    const marker = path.join(root, 'marker.json');
    const release = path.join(root, 'release');
    const script = path.join(root, 'marker.mjs');
    const values = ['中文 空格', "O'Reilly", '$(this_must_not_execute)', 'E:\\中文 空格\\'];
    writeFileSync(script, 'import {writeFileSync,existsSync} from "node:fs"; const deadline=Date.now()+45000; while(!existsSync(process.argv[3])&&Date.now()<deadline)await new Promise(r=>setTimeout(r,20)); if(existsSync(process.argv[3]))writeFileSync(process.argv[2],JSON.stringify(process.argv.slice(4)));', 'utf8');
    try {
      const command = detachedPowershellCommand(process.execPath, [script, marker, release, ...values]);
      // The child cannot finish until the terminal returns and the parent releases it.
      // This proves detachment without depending on a cold-start timing race.
      const result = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', command], { encoding: 'utf8', timeout: 30000, windowsHide: true });
      expect(result.status, result.stderr).toBe(0);
      expect(existsSync(marker), 'The terminal must return before the child finishes').toBe(false);
      writeFileSync(release, 'ready');
      const deadline = Date.now() + 30000;
      while (!existsSync(marker) && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve,20));
      expect(JSON.parse(readFileSync(marker,'utf8'))).toEqual(values);
    } finally { rmSync(root, { recursive:true, force:true }); }
  }, 65000);
});