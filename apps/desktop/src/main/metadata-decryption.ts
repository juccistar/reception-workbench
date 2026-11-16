import { createHash } from 'node:crypto';
import type { CredentialEncryption } from './translation-credentials';

/** Bounded reuse for encrypted activation/context records, never business or model data.
 * Callers still reread and validate the exact file and perform a fresh live handshake.
 */
export function createMetadataDecrypter(encryption: CredentialEncryption): (bytes: Buffer) => Promise<string> {
  const cache = new Map<string, Promise<string>>();
  return bytes => {
    const digest = createHash('sha256').update(bytes).digest('hex');
    const previous = cache.get(digest);
    if (previous) { cache.delete(digest); cache.set(digest, previous); return previous; }
    const pending = Promise.resolve().then(() => encryption.decryptString(bytes)).catch(error => {
      if (cache.get(digest) === pending) cache.delete(digest);
      throw error;
    });
    cache.set(digest, pending);
    if (cache.size > 128) cache.delete(cache.keys().next().value!);
    return pending;
  };
}
