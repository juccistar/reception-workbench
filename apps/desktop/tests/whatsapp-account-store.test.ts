// @vitest-environment node
import { afterEach, expect, it } from 'vitest';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { WhatsAppAccountStore } from '../src/main/whatsapp-account-store';

const roots: string[] = [];
afterEach(async () => { for (const root of roots.splice(0)) await rm(root, { recursive: true, force: true }); });
async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'wa-store-')); roots.push(root);
  const locks = new Set<string>();
  const lock = async (key: string) => {
    if (locks.has(key)) throw new Error('busy');
    locks.add(key);
    return async () => { locks.delete(key); };
  };
  return { root, store: new WhatsAppAccountStore(root, lock) };
}
it('persists bounded public metadata and uses distinct opaque short profile paths', async () => {
  const { root, store } = await fixture();
  const a = await store.create(' First '), b = await store.create('第二个');
  expect(await store.selected()).toBeNull();
  expect(await store.select(b.id)).toEqual({ ...b, selected: true });
  expect(await store.list()).toEqual([a, { ...b, selected: true }]);
  expect(await store.profileDirectory(a.id)).toBe(path.join(root, 'ARWBrowser', 'wa', a.id.replaceAll('-', '')));
  expect(await store.directory(a.id)).not.toBe(await store.directory(b.id));
  expect(JSON.parse(await readFile(path.join(root, 'ARWData', 'whatsapp', 'accounts.json'), 'utf8')).accounts).toEqual([
    { id: a.id, label: 'First', account_tail: '' }, { id: b.id, label: '第二个', account_tail: '' }
  ]);
});
it('requires a known workspace and holds one consumer lock across accounts', async () => {
  const { store } = await fixture();
  const a = await store.create('a'), b = await store.create('b');
  await expect(store.profileDirectory('../bad')).rejects.toThrow();
  const release = await store.acquire(a.id);
  await expect(store.acquire(b.id)).rejects.toThrow('busy');
  await release();
  const releaseB = await store.acquire(b.id);
  await releaseB();
});
it('does not let a new environment bypass the original account ledger and hides its account digest', async () => {
  const { store } = await fixture();
  const first = await store.create('First'), second = await store.create('Second');
  await store.bindAccount(first.id, '123456789@c.us');
  await store.bindAccount(first.id, '123456789@c.us');
  await expect(store.bindAccount(second.id, '123456789@c.us')).rejects.toThrow('原环境');
  await expect(store.bindAccount(first.id, '987654321@c.us')).rejects.toThrow('原账号不同');
  const listed = await store.list();
  expect(listed[0].account_tail).toBe('6789');
  expect(Object.keys(listed[0]).sort()).toEqual(['account_tail', 'id', 'label', 'selected']);
  expect(JSON.stringify(listed)).not.toContain('123456789');
});
it('fails closed on malformed metadata', async () => {
  const { root, store } = await fixture();
  await store.create('a');
  await writeFile(path.join(root, 'ARWData', 'whatsapp', 'accounts.json'), '{broken');
  await expect(store.list()).rejects.toThrow('whatsapp_accounts_unavailable');
  await expect(store.create('b')).rejects.toThrow('whatsapp_accounts_unavailable');
});
