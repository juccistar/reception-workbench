// @vitest-environment node
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
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
      expect(sessions.partition('123', 0)).toBe(old);
      const id = 'wa_recovery_00000000-0000-4000-8000-000000000001';
      expect(await sessions.commit('123', 0, id)).toBe(1);
      expect(await sessions.commit('123', 0, id)).toBe(1);
      expect(await sessions.generation('456')).toBe(0);
      expect(sessions.partition('123', 1)).not.toBe(old);
      await expect(sessions.commit('123', 0, 'wa_recovery_00000000-0000-4000-8000-000000000002')).rejects.toThrow();
      expect(await readFile(join(root, 'whatsapp-session-generation.json'), 'utf8')).not.toContain('123');
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
  it('rejects damaged configuration instead of falling back to generation zero', async () => {
    const root = await mkdtemp(join(tmpdir(), 'wa-session-'));
    try {
      await writeFile(join(root, 'whatsapp-session-generation.json'), '{broken');
      await expect(new WhatsAppSessionHost(root).generation('123')).rejects.toThrow('配置损坏');
    } finally { await rm(root, { recursive: true, force: true }); }
  });
});
