// @vitest-environment node
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { FeishuCredentialStore } from '../src/main/feishu-credentials';

describe('Feishu credentials', () => {
  it('isolates merchants and binds the encrypted secret to the app ID', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'feishu-test-'));
    try {
      const encryption = { isEncryptionAvailable: () => true, encryptString: (s: string) => Buffer.from(s.split('').reverse().join('')), decryptString: (b: Buffer) => b.toString().split('').reverse().join('') };
      const store = new FeishuCredentialStore(dir, encryption);
      await store.save('seller-a', 'cli_one', 'secret-one');
      await store.save('seller-b', 'cli_two', 'secret-two');
      expect(await store.read('seller-a', 'cli_one')).toBe('secret-one');
      expect(await store.read('seller-a', 'cli_two')).toBeNull();
      expect(await store.read('seller-b', 'cli_two')).toBe('secret-two');
      for (const file of await readdir(dir)) expect((await readFile(path.join(dir, file))).toString()).not.toContain('secret-one');
    } finally { await rm(dir, { recursive: true, force: true }); }
  });
});
