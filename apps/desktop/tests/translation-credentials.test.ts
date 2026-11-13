// @vitest-environment node
import { afterEach, describe, expect, it } from 'vitest';
import { mkdtemp, mkdir, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { TranslationCredentialStore } from '../src/main/translation-credentials';

const roots: string[] = [];
async function temporary() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'translation-credentials-'));
  roots.push(root);
  return root;
}
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });

const encryption = {
  isEncryptionAvailable: () => true,
  encryptString: (value: string) => Buffer.from(value, 'utf8').map(byte => byte ^ 0xaa),
  decryptString: (value: Buffer) => value.map(byte => byte ^ 0xaa).toString('utf8'),
};

describe('translation credential store', () => {
  it('stores encrypted values under independent references and removes them', async () => {
    const root = await temporary();
    const directory = path.join(root, 'state', 'credentials');
    const store = new TranslationCredentialStore(directory, encryption);
    const secret = 'synthetic-api-key-123';
    const first = await store.put(secret);
    const second = await store.put('another-synthetic-key');
    expect(first).toMatch(/^[a-f0-9]{64}$/);
    expect(second).not.toBe(first);
    expect(await readdir(directory)).toHaveLength(2);
    expect((await readFile(path.join(directory, first))).includes(Buffer.from(secret))).toBe(false);
    expect(await store.get(first)).toBe(secret);
    expect(await store.get(second)).toBe('another-synthetic-key');
    await store.remove(first);
    await store.remove(first);
    expect(await store.get(first)).toBeNull();
    expect(await store.get(second)).toBe('another-synthetic-key');
    expect(await store.get(null)).toBeNull();
    expect(await store.get(undefined)).toBeNull();
  });

  it('atomically replaces a fixed reference while retaining the default size limit', async () => {
    const root = await temporary();
    const directory = path.join(root, 'state');
    const ref = 'd'.repeat(64);
    const store = new TranslationCredentialStore(directory, encryption);
    await store.replace(ref, 'old-private-value');
    await store.replace(ref, 'new-private-value');
    expect(await store.get(ref)).toBe('new-private-value');
    expect(await readdir(directory)).toEqual([ref]);
    expect((await readFile(path.join(directory, ref))).includes(Buffer.from('new-private-value'))).toBe(false);
    await expect(store.replace(ref, 'x'.repeat(8193))).rejects.toThrow('Invalid translation credential');
    expect(await store.get(ref)).toBe('new-private-value');
    const larger = new TranslationCredentialStore(directory, encryption, 16384);
    await larger.replace(ref, 'x'.repeat(11000));
    expect(await larger.get(ref)).toHaveLength(11000);
    await expect(store.get(ref)).rejects.toThrow('Unable to read translation credential');
  });

  it('rejects malformed references and invalid secrets', async () => {
    const store = new TranslationCredentialStore(await temporary(), encryption);
    for (const ref of ['../secret', '/tmp/secret', 'a'.repeat(63), 'A'.repeat(64), `${'a'.repeat(64)}/x`]) {
      await expect(store.get(ref)).rejects.toThrow('reference');
      await expect(store.remove(ref)).rejects.toThrow('reference');
    }
    for (const secret of ['', '  ', 'key\0value', 'key\nvalue', 'key\rvalue', 'x'.repeat(8193)]) {
      await expect(store.put(secret)).rejects.toThrow('Invalid translation credential');
    }
  });

  it('rejects linked directories and non-regular credential entries', async () => {
    const root = await temporary();
    const real = path.join(root, 'real');
    const linked = path.join(root, 'linked');
    await mkdir(real);
    await symlink(real, linked, process.platform === 'win32' ? 'junction' : 'dir');
    const linkedStore = new TranslationCredentialStore(linked, encryption);
    await expect(linkedStore.put('synthetic-key')).rejects.toThrow('Unable to store');
    await expect(linkedStore.get('a'.repeat(64))).rejects.toThrow('Unable to read');
    const store = new TranslationCredentialStore(real, encryption);
    const ref = 'b'.repeat(64);
    await symlink(root, path.join(real, ref), process.platform === 'win32' ? 'junction' : 'dir');
    await expect(store.get(ref)).rejects.toThrow('Unable to read');
    await expect(store.replace(ref, 'synthetic-key')).rejects.toThrow('Unable to store');
    await expect(store.remove(ref)).rejects.toThrow('Unable to remove');
    expect(await readdir(root)).toContain('real');
    await mkdir(path.join(real, 'c'.repeat(64)));
    await expect(store.get('c'.repeat(64))).rejects.toThrow('Unable to read');
  });

  it('hides encryption and filesystem errors without falling back to plaintext', async () => {
    const root = await temporary();
    const secret = 'synthetic-secret-private';
    const unavailable = new TranslationCredentialStore(path.join(root, 'unavailable'), { ...encryption, isEncryptionAvailable: () => false });
    await expect(unavailable.put(secret)).rejects.toThrow('Unable to store translation credential');
    expect(await readdir(root)).toEqual([]);
    const store = new TranslationCredentialStore(path.join(root, 'state'), encryption);
    const ref = await store.put(secret);
    const broken = new TranslationCredentialStore(path.join(root, 'state'), {
      ...encryption, decryptString: () => { throw new Error(secret); },
    });
    await expect(broken.get(ref)).rejects.toThrow(/^Unable to read translation credential$/);
    const noEncryption = new TranslationCredentialStore(path.join(root, 'state'), { ...encryption, isEncryptionAvailable: () => false });
    await expect(noEncryption.get(ref)).rejects.toThrow('Unable to read translation credential');
    const badDirectory = path.join(root, 'not-a-directory');
    await writeFile(badDirectory, 'synthetic');
    await expect(new TranslationCredentialStore(badDirectory, encryption).put(secret)).rejects.toThrow(/^Unable to store translation credential$/);
  });
});
