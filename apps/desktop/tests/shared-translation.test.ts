// @vitest-environment node
import { afterEach, describe, expect, it } from 'vitest';
import { mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createSharedTranslationStore, resolveSharedTranslationDirectory, SharedTranslationStore } from '../src/main/shared-translation';

const roots: string[] = [];
const originalOverride = process.env.RECEPTION_SHARED_SETTINGS_DIR;
async function temporary() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'shared-translation-'));
  roots.push(root);
  return root;
}
afterEach(async () => {
  if (originalOverride === undefined) delete process.env.RECEPTION_SHARED_SETTINGS_DIR;
  else process.env.RECEPTION_SHARED_SETTINGS_DIR = originalOverride;
  await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true })));
});

const encryption = {
  isEncryptionAvailable: () => true,
  encryptString: (value: string) => Buffer.from(value, 'utf8').map(byte => byte ^ 0xaa),
  decryptString: (value: Buffer) => value.map(byte => byte ^ 0xaa).toString('utf8'),
};
function mutex() {
  let last = Promise.resolve();
  return async () => {
    const prior = last;
    let unlock!: () => void;
    last = new Promise<void>(resolve => { unlock = resolve; });
    await prior;
    return async () => unlock();
  };
}
async function stores() {
  const directory = path.join(await temporary(), 'settings', 'live');
  const lock = mutex();
  return { directory, first: new SharedTranslationStore(directory, encryption, lock), second: new SharedTranslationStore(directory, encryption, lock) };
}
const endpoint = 'https://example.com/v1';

describe('shared translation connection', () => {
  it('shares a single encrypted connection between stores and rejects stale edits', async () => {
    const { directory, first, second } = await stores();
    const key = 'synthetic-private-key';
    expect(await second.read()).toBeNull();
    const initial = await first.save({ revision: 0, base_url: endpoint, model: '  sample-model  ', api_key: key });
    expect(initial).toEqual({ revision: 1, base_url: endpoint, model: 'sample-model', api_key: key, credential_ref: expect.stringMatching(/^[a-f0-9]{64}$/) });
    expect(await second.read()).toEqual(initial);
    const file = path.join(directory, initial.credential_ref!);
    expect(await readdir(directory)).toEqual([initial.credential_ref]);
    const encrypted = await readFile(file);
    for (const text of [key, endpoint, 'sample-model']) expect(encrypted.includes(Buffer.from(text))).toBe(false);
    await expect(second.save({ revision: 0, base_url: endpoint, model: 'stale' })).rejects.toThrow('共享翻译配置已变化，请保留输入并重新读取。');
    const edited = await second.save({ revision: 1, base_url: endpoint, model: 'new-model' });
    expect(edited.api_key).toBe(key);
    expect(edited.credential_ref).toBe(initial.credential_ref);
    expect(edited.revision).toBe(2);
    expect(await readdir(directory)).toEqual([initial.credential_ref]);
    expect(await first.read()).toEqual(edited);
  });

  it('clears globally and prevents a later legacy import from restoring the key', async () => {
    const { first, second } = await stores();
    const imported = await first.importIfEmpty({ base_url: endpoint, model: 'old', api_key: 'legacy-synthetic-key' });
    expect(await second.importIfEmpty({ base_url: endpoint, model: 'ignored', api_key: 'new-legacy-key' })).toEqual(imported);
    const cleared = await second.save({ revision: imported.revision, base_url: endpoint, model: 'old', clear_key: true });
    expect(cleared).toEqual({ revision: 2, base_url: endpoint, model: 'old', api_key: '', credential_ref: null });
    expect(await first.importIfEmpty({ base_url: endpoint, model: 'resurrection', api_key: 'legacy-synthetic-key' })).toEqual(cleared);
    expect(await first.read()).toEqual(cleared);
  });

  it('serializes concurrent changes and reads complete encrypted replacements', async () => {
    const { first, second, directory } = await stores();
    const initial = await first.save({ revision: 0, base_url: endpoint, model: 'old', api_key: 'old-key' });
    const reads = Promise.all(Array.from({ length: 30 }, () => second.read()));
    const [one, two] = await Promise.allSettled([
      first.save({ revision: 1, base_url: endpoint, model: 'new', api_key: 'new-key' }),
      second.save({ revision: 1, base_url: endpoint, model: 'other', api_key: 'other-key' }),
    ]);
    for (const result of await reads) {
      expect([initial, one.status === 'fulfilled' ? one.value : two.status === 'fulfilled' ? two.value : initial]).toContainEqual(result);
    }
    expect([one.status, two.status].sort()).toEqual(['fulfilled', 'rejected']);
    const latest = (await first.read())!;
    expect(latest.revision).toBe(2);
    for (const result of await Promise.all(Array.from({ length: 30 }, () => second.read()))) expect(result).toEqual(latest);
    expect(await readdir(directory)).toEqual([initial.credential_ref]);
  });

  it('fails closed for unavailable encryption, corrupt records and unsafe directories', async () => {
    const { first, directory } = await stores();
    const key = 'private-synthetic-key';
    const unavailable = new SharedTranslationStore(directory, { ...encryption, isEncryptionAvailable: () => false }, mutex());
    await expect(unavailable.read()).rejects.toThrow(/^无法读取共享翻译配置。$/);
    await expect(unavailable.save({ revision: 0, base_url: endpoint, model: 'model', api_key: key })).rejects.toThrow(/^无法读取共享翻译配置。$/);
    expect(await readdir(path.dirname(path.dirname(directory)))).toEqual([]);
    const created = await first.save({ revision: 0, base_url: endpoint, model: 'model', api_key: key });
    await expect(unavailable.read()).rejects.toThrow(/^无法读取共享翻译配置。$/);
    const broken = new SharedTranslationStore(directory, { ...encryption, decryptString: () => { throw new Error(key); } }, mutex());
    await expect(broken.read()).rejects.toThrow(/^Unable to read translation credential$/);
    await writeFile(path.join(directory, created.credential_ref!), encryption.encryptString('{"scope":"invalid","api_key":"' + key + '"}'));
    await expect(first.read()).rejects.toThrow(/^无法读取共享翻译配置。$/);
    const linked = path.join(await temporary(), 'linked');
    await symlink(directory, linked, process.platform === 'win32' ? 'junction' : 'dir');
    await expect(new SharedTranslationStore(linked, encryption, mutex()).read()).rejects.toThrow('Unable to read translation credential');
  });

  it('uses only the explicit isolated override and keeps demo and live separate', async () => {
    const root = await temporary();
    process.env.RECEPTION_SHARED_SETTINGS_DIR = root;
    expect(await resolveSharedTranslationDirectory('demo')).toBe(path.join(root, 'demo'));
    expect(await resolveSharedTranslationDirectory('live')).toBe(path.join(root, 'live'));
    const demo = await createSharedTranslationStore(encryption, 'demo');
    expect(await demo.read()).toBeNull();
    process.env.RECEPTION_SHARED_SETTINGS_DIR = path.join(root, 'missing', '..');
    expect(await resolveSharedTranslationDirectory('demo')).toBe(path.join(root, 'demo'));
    process.env.RECEPTION_SHARED_SETTINGS_DIR = 'relative-path';
    await expect(resolveSharedTranslationDirectory('live')).rejects.toThrow('共享翻译配置目录不可用。');
  });

  it('enforces model, endpoint, key and revision limits without persisting rejected input', async () => {
    const { first } = await stores();
    for (const changes of [
      { model: 'bad\nmodel' }, { model: 'x'.repeat(257) }, { base_url: 'http://example.com/v1' },
      { api_key: 'bad\nkey' }, { api_key: 'x'.repeat(7001) }, { revision: -1 },
    ]) {
      await expect(first.save({ revision: 0, base_url: endpoint, model: 'valid', ...changes })).rejects.toThrow();
    }
    expect(await first.read()).toBeNull();
    const longEndpoint = endpoint + '/a'.repeat(700);
    const large = await first.save({ revision: 0, base_url: longEndpoint, model: 'valid', api_key: 'k'.repeat(7000) });
    expect(large.api_key).toHaveLength(7000);
    expect(large.base_url).toBe(longEndpoint);
  });
});
