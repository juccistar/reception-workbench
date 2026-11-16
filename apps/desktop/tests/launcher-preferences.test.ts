// @vitest-environment node
import { afterEach, describe, expect, it } from 'vitest';
import { link, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { LauncherPreferences } from '../src/main/launcher-preferences';

const directories: string[] = [];
const id = '12345678-1234-5678-1234-123456789abc';
async function fixture() {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'arw-launcher-label-'));
  directories.push(directory);
  let pending = Promise.resolve();
  const lock = async () => {
    const previous = pending;
    let release!: () => void;
    pending = new Promise<void>(resolve => { release = resolve; });
    await previous;
    return async () => release();
  };
  return { directory, store: new LauncherPreferences(directory, lock), second: new LauncherPreferences(directory, lock) };
}
afterEach(async () => { for (const directory of directories.splice(0)) await rm(directory, { recursive: true, force: true }); });

describe('local merchant display names', () => {
  it('writes a local alias with revision while preserving the company identity outside this file', async () => {
    const { directory, store } = await fixture();
    expect(await store.list()).toEqual([]);
    await store.rename({ merchant_id: id, name: '  华东业务  ', expected_revision: 0 });
    expect(await store.list()).toEqual([{ merchant_id: id, name: '华东业务', revision: 1 }]);
    const file = path.join(directory, 'display-names.json');
    const before = await readFile(file, 'utf8');
    await store.rename({ merchant_id: id, name: '华东业务', expected_revision: 1 });
    expect(await readFile(file, 'utf8')).toBe(before);
    expect(Object.keys(JSON.parse(before)).sort()).toEqual(['format', 'merchants']);
  });
  it('rejects a stale edit from another window', async () => {
    const { store, second } = await fixture();
    const results = await Promise.allSettled([
      store.rename({ merchant_id: id, name: '甲', expected_revision: 0 }),
      second.rename({ merchant_id: id, name: '乙', expected_revision: 0 }),
    ]);
    expect(results.filter(value => value.status === 'fulfilled')).toHaveLength(1);
    expect(results.filter(value => value.status === 'rejected')).toHaveLength(1);
    expect((await store.list())[0].revision).toBe(1);
  });
  it('rejects corrupt or hardlinked metadata without replacing it', async () => {
    const { directory, store } = await fixture();
    const file = path.join(directory, 'display-names.json');
    await writeFile(file, 'preserved invalid metadata');
    await expect(store.rename({ merchant_id: id, name: '甲', expected_revision: 0 })).rejects.toThrow('读取失败');
    expect(await readFile(file, 'utf8')).toBe('preserved invalid metadata');
    await writeFile(file, JSON.stringify({ format: 'launcher-display-v1', merchants: [] }));
    await link(file, path.join(directory, 'linked.json'));
    await expect(store.list()).rejects.toThrow('读取失败');
  });
  it('rejects unsupported fields and control characters', async () => {
    const { store } = await fixture();
    await expect(store.rename({ merchant_id: id, name: '甲', expected_revision: 0, seller_id: '123' })).rejects.toThrow('有效');
    await expect(store.rename({ merchant_id: id, name: '甲\n乙', expected_revision: 0 })).rejects.toThrow('有效');
    await expect(store.rename([])).rejects.toThrow('有效');
  });
});
