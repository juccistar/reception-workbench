// @vitest-environment node
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import * as files from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { WhatsAppSessionHost } from '../src/main/whatsapp-session-host';
vi.mock('node:fs/promises', async importOriginal => {
  const actual = await importOriginal<typeof import('node:fs/promises')>();
  return { ...actual, rename: vi.fn(actual.rename) };
});

describe('WhatsApp login environment generations', () => {
  it('keeps the legacy partition at generation zero and isolates merchants', async () => {
    const root = await mkdtemp(join(tmpdir(), 'wa-session-'));
    try {
      const sessions = new WhatsAppSessionHost(root);
      const old = 'persist:whatsapp-' + createHash('sha256').update(root + ':123').digest('hex');
      expect(await sessions.generation('123')).toBe(0);
      expect(await sessions.partition('123', 0)).toBe(old);
      const id = 'wa_recovery_00000000-0000-4000-8000-000000000001';
      expect(await sessions.commit('123', 0, id)).toBe(1);
      expect(await sessions.commit('123', 0, id)).toBe(1);
      expect(await sessions.generation('456')).toBe(0);
      const compact = await sessions.partition('123', 1);
      expect(compact).toMatch(/^persist:wa-[0-9a-f]{24}$/);
      expect(compact.slice(8)).toHaveLength(27);
      expect(await new WhatsAppSessionHost(root).partition('123', 1)).toBe(compact);
      await expect(sessions.commit('123', 0, 'wa_recovery_00000000-0000-4000-8000-000000000002')).rejects.toThrow();
      const saved = JSON.parse(await readFile(join(root, 'whatsapp-session-generation.json'), 'utf8'));
      expect(Object.keys(saved.merchants)).toEqual([createHash('sha256').update('123').digest('hex')]);
    } finally { await rm(root, { recursive: true, force: true }); }
  });
  it('serializes concurrent recovery requests without losing another merchant', async () => {
    const root = await mkdtemp(join(tmpdir(), 'wa-session-'));
    try {
      const sessions = new WhatsAppSessionHost(root);
      const id = 'wa_recovery_00000000-0000-4000-8000-000000000001';
      expect(await Promise.all([sessions.commit('123', 0, id), sessions.commit('123', 0, id),
        sessions.commit('456', 0, 'wa_recovery_00000000-0000-4000-8000-000000000002')])).toEqual([1, 1, 1]);
      expect(await sessions.generation('123')).toBe(1);
      expect(await sessions.generation('456')).toBe(1);
    } finally { await rm(root, { recursive: true, force: true }); }
  });
  it('keeps the old generation on an interrupted commit and resumes a committed one', async () => {
    const root = await mkdtemp(join(tmpdir(), 'wa-session-'));
    try {
      await writeFile(join(root, 'whatsapp-session-generation.json.interrupted.tmp'), '{incomplete');
      const sessions = new WhatsAppSessionHost(root);
      expect(await sessions.generation('123')).toBe(0);
      vi.mocked(files.rename).mockRejectedValueOnce(new Error('synthetic write failure'));
      await expect(sessions.commit('123', 0, 'wa_recovery_00000000-0000-4000-8000-000000000001')).rejects.toThrow();
      expect(await sessions.generation('123')).toBe(0);
      expect((await readdir(root)).filter(name => name.endsWith('.tmp'))).toHaveLength(1);
      await sessions.commit('123', 0, 'wa_recovery_00000000-0000-4000-8000-000000000002');
      expect(await new WhatsAppSessionHost(root).generation('123')).toBe(1);
    } finally { await rm(root, { recursive: true, force: true }); }
  });
  it('keeps a saved legacy generation unchanged until a confirmed new commit', async () => {
    const root = await mkdtemp(join(tmpdir(), 'wa-session-'));
    try {
      const merchant = createHash('sha256').update('123').digest('hex');
      const original = { version: 1, merchants: { [merchant]: { session_generation: 1, recovery_id: 'wa_recovery_00000000-0000-4000-8000-000000000001', recovery_stage: 'committed' } } };
      const text = JSON.stringify(original);
      await writeFile(join(root, 'whatsapp-session-generation.json'), text);
      const sessions = new WhatsAppSessionHost(root);
      const old = 'persist:whatsapp-' + createHash('sha256').update(root + ':123:generation:1').digest('hex');
      expect(await sessions.partition('123', 1)).toBe(old);
      expect(await readFile(join(root, 'whatsapp-session-generation.json'), 'utf8')).toBe(text);
      expect(await sessions.commit('123', 1, 'wa_recovery_00000000-0000-4000-8000-000000000002')).toBe(2);
      expect(await sessions.partition('123', 2)).toMatch(/^persist:wa-[0-9a-f]{24}$/);
      const saved = JSON.parse(await readFile(join(root, 'whatsapp-session-generation.json'), 'utf8'));
      expect(saved.version).toBe(2);
      expect(saved.merchants[merchant].partition_name).toMatch(/^wa-[0-9a-f]{24}$/);
      await expect(sessions.partition('123', 1)).rejects.toThrow('已变化');
    } finally { await rm(root, { recursive: true, force: true }); }
  });
  it('preserves another legacy merchant when upgrading the session file', async () => {
    const root = await mkdtemp(join(tmpdir(), 'wa-session-'));
    try {
      const merchant = createHash('sha256').update('456').digest('hex');
      const previous = { session_generation: 1, recovery_id: 'wa_recovery_00000000-0000-4000-8000-000000000001', recovery_stage: 'committed' };
      await writeFile(join(root, 'whatsapp-session-generation.json'), JSON.stringify({ version: 1, merchants: { [merchant]: previous } }));
      const sessions = new WhatsAppSessionHost(root);
      const old = await sessions.partition('456', 1);
      await sessions.commit('123', 0, 'wa_recovery_00000000-0000-4000-8000-000000000002');
      expect(await sessions.partition('456', 1)).toBe(old);
      expect(await sessions.partition('123', 1)).not.toBe(old);
      expect(JSON.parse(await readFile(join(root, 'whatsapp-session-generation.json'), 'utf8')).merchants[merchant]).toEqual(previous);
    } finally { await rm(root, { recursive: true, force: true }); }
  });
  it('does not reuse a preexisting directory when selecting the compact name', async () => {
    const root = await mkdtemp(join(tmpdir(), 'wa-session-'));
    try {
      const name = 'wa-' + createHash('sha256').update(root + ':123:compact-v1:1').digest('hex').slice(0, 24);
      await mkdir(join(root, 'Partitions', name), { recursive: true });
      await writeFile(join(root, 'Partitions', name, 'preserved.txt'), 'synthetic previous directory');
      const sessions = new WhatsAppSessionHost(root);
      await expect(sessions.commit('123', 0, 'wa_recovery_00000000-0000-4000-8000-000000000001')).rejects.toThrow('已被占用');
      expect(await sessions.generation('123')).toBe(0);
      expect(await readFile(join(root, 'Partitions', name, 'preserved.txt'), 'utf8')).toBe('synthetic previous directory');
    } finally { await rm(root, { recursive: true, force: true }); }
  });
  it('rejects invalid or shared compact names and compact names in an old file', async () => {
    const root = await mkdtemp(join(tmpdir(), 'wa-session-'));
    try {
      const merchant = createHash('sha256').update('123').digest('hex');
      const second = createHash('sha256').update('456').digest('hex');
      const record = { session_generation: 1, recovery_id: 'wa_recovery_00000000-0000-4000-8000-000000000001', recovery_stage: 'committed', partition_name: 'wa-' + 'a'.repeat(24) };
      for (const invalid of [
        { version: 2, merchants: { [merchant]: { ...record, partition_name: '../outside' } } },
        { version: 2, merchants: { [merchant]: record, [second]: record } },
        { version: 1, merchants: { [merchant]: record } },
      ]) {
        await writeFile(join(root, 'whatsapp-session-generation.json'), JSON.stringify(invalid));
        await expect(new WhatsAppSessionHost(root).partition('123', 1)).rejects.toThrow('配置损坏');
      }
    } finally { await rm(root, { recursive: true, force: true }); }
  });
  it('rejects damaged configuration instead of falling back to generation zero', async () => {
    const root = await mkdtemp(join(tmpdir(), 'wa-session-'));
    try {
      await writeFile(join(root, 'whatsapp-session-generation.json'), '{broken');
      await expect(new WhatsAppSessionHost(root).generation('123')).rejects.toThrow('配置损坏');
    } finally { await rm(root, { recursive: true, force: true }); }
  });
});
