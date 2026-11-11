import { afterEach, describe, expect, it } from 'vitest';
import { mkdtemp, mkdir, rm, writeFile, symlink } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { acquireStorageOperation, cleanStorageRequest, validateStorageDestination, runMaintenance, maintenanceArguments, safetyBackupParent } from '../src/main/storage-host';

const roots: string[] = [];
async function temporary() { const root = await mkdtemp(path.join(os.tmpdir(), 'reception-storage-host-')); roots.push(root); return root; }
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });
describe('offline storage host boundary', () => {
  it('keeps offline maintenance free of platform credentials and raw recovery separate from backup', async () => {
    const root = await temporary(); const script = path.join(root, 'offline.cjs');
    await writeFile(script, 'console.log(JSON.stringify({status:"completed",action:"prepare",data_root:process.argv[2],backup:null,recovery_backup:process.argv[3],credentials_present:Object.keys(process.env).some(k=>/ACCIO|PHOENIX|SYNTHETIC_SECRET/.test(k))}));');
    const raw = path.join(root, 'raw-evidence');
    const result = await runMaintenance({ executable: process.execPath, prefix: [script], cwd: root,
      env: { ...process.env, ACCIO_GATEWAY_TOKEN: 'synthetic-secret', PHOENIX_TOKEN: 'synthetic-secret', SYNTHETIC_SECRET: 'synthetic-secret' } }, [root, raw]);
    expect(result.credentials_present).toBe(false);
    expect(result.backup).toBeNull(); expect(result.recovery_backup).toBe(raw);
    const registry = path.join(root, 'isolated', 'AlibabaReceptionWorkbenchHost');
    const backup = safetyBackupParent(path.join(registry, 'profiles', 'a'.repeat(64), 'state'), registry);
    expect(path.relative(registry, backup).startsWith('..')).toBe(true);
    expect(backup.length).toBeLessThan(path.join(registry, 'profiles', 'a'.repeat(64), 'state', 'backups').length);
  });
  it('maps maintenance failures to fixed local messages', async () => {
    const root = await temporary(); const script = path.join(root, 'refused.cjs');
    const command = { executable: process.execPath, prefix: [script], cwd: root, env: process.env };
    await writeFile(script, 'console.log(JSON.stringify({status:"refused",code:"storage_path_too_long",message:"PRIVATE_NATIVE_OUTPUT"}));process.exitCode=1;');
    await expect(runMaintenance(command, [])).rejects.toThrow('数据目录路径过长');
    await writeFile(script, 'console.log(JSON.stringify({status:"refused",code:"__proto__",message:"PRIVATE_NATIVE_OUTPUT"}));process.exitCode=1;');
    await expect(runMaintenance(command, [])).rejects.toThrow('离线校验未通过');
  });
  it('rejects relative paths and renderer-supplied sources or commands', () => {
    expect(() => cleanStorageRequest({ action: 'migrate', target: '../data' })).toThrow('绝对路径');
    expect(() => cleanStorageRequest({ action: 'migrate', target: path.join(os.tmpdir(), 'data'), source: 'other' })).toThrow('参数');
    expect(() => cleanStorageRequest({ action: 'execute', target: path.join(os.tmpdir(), 'data') })).toThrow('参数');
    expect(() => cleanStorageRequest({ action: 'restore', target: path.join(os.tmpdir(), 'data') })).toThrow('绝对路径');
  });
  it('refuses nested source/target and bootstrap or application directories', async () => {
    const root = await temporary();
    const source = path.join(root, 'source'); const state = path.join(root, 'bootstrap'); const app = path.join(root, 'application');
    for (const target of [source, path.join(source, 'child'), root, state, path.join(state, 'child'), app]) {
      await expect(validateStorageDestination({ action: 'migrate', target }, source, state, app)).rejects.toThrow();
    }
    await expect(validateStorageDestination({ action: 'migrate', target: path.join(root, '客户 数据') }, source, state, app)).resolves.toBeUndefined();
  });
  it('refuses a directory junction in a destination ancestry', async () => {
    const root = await temporary(); const real = path.join(root, 'real'); const linked = path.join(root, 'linked');
    await mkdir(real); await symlink(real, linked, process.platform === 'win32' ? 'junction' : 'dir');
    await expect(validateStorageDestination({ action: 'migrate', target: path.join(linked, 'child') }, path.join(root, 'source'), path.join(root, 'state'), path.join(root, 'app'))).rejects.toThrow();
  });
  it('serializes bootstrap writers across separate operations', async () => {
    const root = await temporary(); const state = path.join(root, 'state');
    const release = await acquireStorageOperation(state);
    await expect(acquireStorageOperation(state)).rejects.toThrow('另一个');
    await release();
    const nextRelease = await acquireStorageOperation(state); await nextRelease();
  }, 60000); // Three real PowerShell starts; each production acquisition allows 15s.
  it('passes paths as argument values without a shell and accepts only completed structured results', async () => {
    const root = await temporary(); const script = path.join(root, 'maintenance.cjs');
    const target = path.join(root, '中文 $(literal)');
    await writeFile(script, 'process.stderr.write("PRIVATE_NATIVE_OUTPUT"); console.log(JSON.stringify({status:"completed",action:process.argv[3],data_root:process.argv[5],backup:null,args:process.argv.slice(2)}));');
    const args = maintenanceArguments('inspect', target, { mode: 'demo' });
    const result = await runMaintenance({ executable: process.execPath, prefix: [script], cwd: root, env: process.env }, args);
    expect(result.data_root).toBe(target); expect(result.args).toEqual(args);
    await writeFile(script, 'console.log(JSON.stringify({status:"refused",code:"workspace_busy",message:"工作区正在使用"})); process.exitCode=1;');
    await expect(runMaintenance({ executable: process.execPath, prefix: [script], cwd: root, env: process.env }, args)).rejects.toThrow('工作区正在使用');
    await writeFile(script, 'console.log("not-json");');
    await expect(runMaintenance({ executable: process.execPath, prefix: [script], cwd: root, env: process.env }, args)).rejects.toThrow('未收到完整');
  });
});
