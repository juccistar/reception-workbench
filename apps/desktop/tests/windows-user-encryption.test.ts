// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { createWindowsUserEncryption } from '../src/main/windows-user-encryption';

// These are synthetic in-memory values; no credentials or business files are read.
describe.skipIf(process.platform !== 'win32')('Windows user DPAPI encryption', () => {
  it('decrypts across independent instances despite redirected Accio profile environment', async () => {
    const oldLocal = process.env.LOCALAPPDATA;
    const oldProfile = process.env.USERPROFILE;
    try {
      process.env.LOCALAPPDATA = 'C:\\synthetic-profile-a\\AppData\\Local';
      process.env.USERPROFILE = 'C:\\synthetic-profile-a';
      const first = createWindowsUserEncryption();
      const secret = 'synthetic Windows user key 中文';
      const ciphertext = await first.encryptString(secret);
      expect(ciphertext.includes(Buffer.from(secret))).toBe(false);
      process.env.LOCALAPPDATA = 'C:\\synthetic-profile-b\\AppData\\Local';
      process.env.USERPROFILE = 'C:\\synthetic-profile-b';
      const second = createWindowsUserEncryption();
      expect(await second.decryptString(ciphertext)).toBe(secret);
      expect(await second.decryptString(ciphertext)).toBe(secret);
    } finally {
      if (oldLocal === undefined) delete process.env.LOCALAPPDATA; else process.env.LOCALAPPDATA = oldLocal;
      if (oldProfile === undefined) delete process.env.USERPROFILE; else process.env.USERPROFILE = oldProfile;
    }
  }, 15000);

  it('rejects invalid ciphertext without disclosing its contents', async () => {
    const encryption = createWindowsUserEncryption();
    await expect(encryption.decryptString(Buffer.from('synthetic-invalid-private-key'))).rejects.toThrow(/^Windows 用户加密不可用。$/);
  }, 15000);
});
