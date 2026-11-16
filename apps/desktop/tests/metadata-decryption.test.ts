// @vitest-environment node
import { expect, it, vi } from 'vitest';
import { createMetadataDecrypter } from '../src/main/metadata-decryption';
it('reuses exact ciphertext across alternating merchant records and rereads changed data', async () => {
  const decryptString = vi.fn(async (bytes: Buffer) => bytes.toString('utf8'));
  const decrypt = createMetadataDecrypter({ decryptString } as any);
  const a = Buffer.from('record-a'), b = Buffer.from('record-b');
  expect(await Promise.all([decrypt(a), decrypt(b), decrypt(a)])).toEqual(['record-a', 'record-b', 'record-a']);
  expect(decryptString).toHaveBeenCalledTimes(2);
  expect(await decrypt(Buffer.from('record-a-replaced'))).toBe('record-a-replaced');
  expect(decryptString).toHaveBeenCalledTimes(3);
});
it('does not preserve failed decryptions and bounds retained metadata', async () => {
  const decryptString = vi.fn(async (bytes: Buffer) => bytes.toString('utf8'));
  const decrypt = createMetadataDecrypter({ decryptString } as any);
  decryptString.mockRejectedValueOnce(new Error('synthetic unavailable'));
  await expect(decrypt(Buffer.from('retry'))).rejects.toThrow('synthetic unavailable');
  await expect(decrypt(Buffer.from('retry'))).resolves.toBe('retry');
  for (let i = 0; i < 129; i++) await decrypt(Buffer.from(String(i)));
  const calls = decryptString.mock.calls.length;
  await decrypt(Buffer.from('retry'));
  expect(decryptString).toHaveBeenCalledTimes(calls + 1);
});
