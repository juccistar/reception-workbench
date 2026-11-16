// @vitest-environment node
import { afterEach, expect, it } from 'vitest';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
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
  const ownership = path.join(root, 'shared');
  return { root, ownership, lock, store: new WhatsAppAccountStore(path.join(root, 'a'), lock, ownership) };
}
it('reads old v1 metadata and applies rename with revision CAS', async () => {
  const { root, store } = await fixture();
  const account = await store.create('first');
  const file = path.join(root, 'a', 'ARWData', 'whatsapp', 'accounts.json');
  const index = JSON.parse(await readFile(file, 'utf8'));
  delete index.accounts[0].revision;
  index.selected = account.id;
  await writeFile(file, JSON.stringify(index));
  expect((await store.list())[0].revision).toBe(0);
  expect((await store.rename(account.id, 'renamed', 0)).revision).toBe(1);
  await expect(store.rename(account.id, 'stale', 0)).rejects.toThrow('revision_conflict');
  expect((await store.list())[0].label).toBe('renamed');
});
it('holds independent workspace locks and refuses a duplicate owner across roots', async () => {
  const { root, store, lock, ownership } = await fixture();
  const other = new WhatsAppAccountStore(path.join(root, 'b'), lock, ownership);
  const a = await store.create('a'), b = await store.create('b'), c = await other.create('c');
  const releaseA = await store.acquire(a.id), releaseB = await store.acquire(b.id), releaseC = await other.acquire(c.id);
  await store.bindAccount(a.id, '123456789:4@c.us');
  await store.bindAccount(b.id, '222222222@c.us');
  await expect(other.bindAccount(c.id, '123456789@c.us')).rejects.toThrow('busy');
  await releaseA();
  await expect(other.bindAccount(c.id, '123456789@c.us')).rejects.toThrow('原环境');
  await releaseB(); await releaseC();
  const again = await store.acquire(a.id);
  await store.bindAccount(a.id, '123456789@c.us');
  await again();
  expect(JSON.stringify(await store.list())).not.toContain('123456789');
});
it('rejects a copied workspace before its backend starts in another root', async () => {
  const { root, store, lock, ownership } = await fixture();
  const other = new WhatsAppAccountStore(path.join(root, 'b'), lock, ownership);
  const a = await store.create('a'); await other.create('temporary');
  const release = await store.acquire(a.id);
  await store.bindAccount(a.id, '123456789@c.us');
  const original = await readFile(path.join(root, 'a', 'ARWData', 'whatsapp', 'accounts.json'));
  await writeFile(path.join(root, 'b', 'ARWData', 'whatsapp', 'accounts.json'), original);
  await expect(other.acquire(a.id)).rejects.toThrow('busy');
  await release();
  await expect(other.acquire(a.id)).rejects.toThrow('原环境');
});
it('freezes new identities until legacy device-digest owners are verified', async () => {
  const { root, store } = await fixture();
  const old = await store.create('old'), fresh = await store.create('fresh');
  const file = path.join(root, 'a', 'ARWData', 'whatsapp', 'accounts.json');
  const index = JSON.parse(await readFile(file, 'utf8'));
  index.accounts[0].account_digest = createHash('sha256').update('123456789:4@c.us').digest('hex');
  await writeFile(file, JSON.stringify(index));
  const releaseOld = await store.acquire(old.id), releaseFresh = await store.acquire(fresh.id);
  await expect(store.bindAccount(fresh.id, '123456789@c.us')).rejects.toThrow('旧账号身份尚未核验');
  await store.bindAccount(old.id, '123456789:4@c.us');
  await expect(store.bindAccount(fresh.id, '123456789@c.us')).rejects.toThrow('原环境');
  await releaseOld(); await releaseFresh();
});
it('rejects malformed account metadata', async () => {
  const { root, store } = await fixture();
  await store.create('a');
  await writeFile(path.join(root, 'a', 'ARWData', 'whatsapp', 'accounts.json'), '{broken');
  await expect(store.list()).rejects.toThrow('whatsapp_accounts_unavailable');
});
