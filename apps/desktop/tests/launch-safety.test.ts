import { describe, expect, it } from 'vitest';
import { createHash } from 'node:crypto';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { genericLaunchMessage, launchErrorMessage, registeredNewRoot, workbenchRestartArguments } from '../src/main/launch-safety';

const seller = '123456789';
describe('authorized workbench launch safeguards', () => {
  it('allows only a registered created original root and prepares it with the real maintenance backend', () => {
    const base = mkdtempSync(path.join(os.tmpdir(), 'host-created-'));
    try {
      const registry = path.join(base, 'registry');
      const state = path.join(registry, 'profiles', createHash('sha256').update(seller).digest('hex'), 'state');
      const root = path.join(base, 'workspace');
      mkdirSync(state, { recursive: true }); mkdirSync(path.join(root, 'host', 'live', 'default'), { recursive: true });
      const record = { sellerId: seller, stateDirectory: state, initialDataRoot: root, status: 'created' };
      const saveRegistry = () => writeFileSync(path.join(registry, 'merchants.json'), JSON.stringify({ format: 1, profiles: { [seller]: record } }));
      const saveConfig = (dataRoot: string, previousRoot?: string) => writeFileSync(path.join(state, 'storage.json'), JSON.stringify({ version: 1, dataRoot, ...(previousRoot ? { previousRoot } : {}) }));
      saveRegistry(); saveConfig(root);
      expect(registeredNewRoot(registry, state, root, seller)).toBe(true);
      const python = path.resolve('services/reception/.venv/Scripts/python.exe');
      const script = path.resolve('services/reception/run.py');
      const run = (allow: boolean) => spawnSync(python, ['-I', script, '--maintenance', 'prepare', '--source', root, ...(allow ? ['--allow-new'] : [])], { encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
      expect(run(false).status).not.toBe(0);
      const result = run(true); expect(result.status, result.stderr).toBe(0); expect(JSON.parse(result.stdout).new).toBe(true);
      record.status = 'verified'; saveRegistry(); expect(registeredNewRoot(registry, state, root, seller)).toBe(false);
      record.status = 'created'; saveRegistry(); saveConfig(path.join(base, 'migrated'), root);
      expect(registeredNewRoot(registry, state, root, seller)).toBe(false);
      saveConfig(root); expect(registeredNewRoot(registry, state, root, '123456780')).toBe(false);
      rmSync(root, { recursive: true }); expect(registeredNewRoot(registry, state, root, seller)).toBe(false);
    } finally { rmSync(base, { recursive: true, force: true }); }
  });
  it('keeps the seller bound while removing stale roots and executable bindings', () => {
    expect(workbenchRestartArguments(['--connect-from-accio', '--expect-seller', seller, '--data-dir', 'old', '--workctl=old-tool'], 'new')).toEqual(['--connect-from-accio', '--expect-seller', seller]);
    expect(workbenchRestartArguments(['--connect-from-accio', '--seller-id=' + seller, '--data-dir=old', '--workctl', 'old-tool'], 'new')).toEqual(['--connect-from-accio', '--seller-id=' + seller]);
  });
  it('shows only fixed messages for bounded known error codes', () => {
    const encode = (s: string) => Buffer.from(s);
    expect(launchErrorMessage(encode('{"status":"blocked","code":"seller_mismatch","message":"secret"}'), false)).toContain('不一致');
    for (const raw of ['secret /private/path', '{"status":"blocked","code":"__proto__"}', '{"status":"other","code":"seller_mismatch"}', '{"status":"blocked","code":"unknown"}']) expect(launchErrorMessage(encode(raw), false)).toBe(genericLaunchMessage);
    expect(launchErrorMessage(encode('{"status":"blocked","code":"seller_mismatch"}'), true)).toBe(genericLaunchMessage);
  });
});
