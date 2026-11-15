// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { ModelCheckHost } from '../src/main/model-check-host';

describe('isolated model check', () => {
  it('stops a process whose output exceeds the limit', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'check-host-'));
    const script = path.join(dir, 'check.cjs');
    await writeFile(script, "process.stdout.write('x'.repeat(1100000)); setInterval(() => {}, 1000);");
    const host = new ModelCheckHost({ executable: process.execPath, prefix: [script], cwd: dir, env: process.env, timeoutMs: 3000 });
    try {
      await expect(host.check({ profile_id: 'a', config_revision: 1, credential_revision: 1, purpose: 'reception' }, {})).rejects.toThrow('model_check_invalid_result');
    } finally { host.close(); await rm(dir, { recursive: true, force: true }); }
  });
  it('deduplicates the same purpose while keeping purposes independent and cancels after process exit', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'check-host-'));
    const script = path.join(dir, 'check.cjs');
    await writeFile(script, "process.stdin.resume(); process.stdin.on('end', () => setTimeout(() => console.log(JSON.stringify({pid:process.pid})), 300));");
    const host = new ModelCheckHost({ executable: process.execPath, prefix: [script], cwd: dir, env: process.env, timeoutMs: 3000 });
    try {
      const identity = { profile_id: 'a', config_revision: 1, credential_revision: 1, purpose: 'reception' as const };
      const first = host.check(identity, {});
      expect(host.check(identity, {})).toBe(first);
      const other = host.check({ ...identity, purpose: 'translation' }, {});
      const [a, b] = await Promise.all([first, other]) as Array<{pid: number}>;
      expect(a.pid).not.toBe(b.pid);
      const pending = host.check(identity, {});
      host.cancel('a');
      await expect(pending).rejects.toThrow('model_check_cancelled');
    } finally { host.close(); await rm(dir, { recursive: true, force: true }); }
  });
});
