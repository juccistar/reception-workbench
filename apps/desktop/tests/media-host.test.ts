// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { createHash } from 'node:crypto';
import { productUrl, readOriginal, validateReference } from '../src/main/media-host';

const bytes = Buffer.alloc(300_000, 7);
const sha = createHash('sha256').update(bytes).digest('hex');
const reference = { kind: 'product' as const, local_product_id: 'product-1', version: 1, sha256: sha };
function invoke<T>(method: string, params: Record<string, unknown>): Promise<T> {
  if (method === '_image_original_describe') return Promise.resolve({ size: bytes.length, width: 10, height: 10, media_type: 'image/png', sha256: sha, chunk_size: 262144 } as T);
  const offset = params.offset as number;
  return Promise.resolve({ offset, sha256: sha, data_b64: bytes.subarray(offset, offset + 262144).toString('base64') } as T);
}

describe('private media host', () => {
  it('reads and verifies bounded chunks', async () => {
    const image = await readOriginal(invoke, reference, () => {});
    expect(Buffer.from(image.bytes)).toEqual(bytes);
  });
  it('rejects forged references and altered chunks', async () => {
    expect(() => validateReference({ ...reference, path: 'x' })).toThrow();
    const corrupt: typeof invoke = async <T>(method: string, params: Record<string, unknown>) => {
      const result = await invoke<Record<string, unknown>>(method, params);
      if (method === '_image_original_chunk') result.data_b64 = Buffer.alloc(Math.min(262144, bytes.length - (params.offset as number)), 8).toString('base64');
      return result as T;
    };
    await expect(readOriginal(corrupt, reference, () => {})).rejects.toThrow('完整性');
  });
  it('reads converted PNG through the private token-bound channel', async () => {
    const token = 'a'.repeat(32);
    const copied: typeof invoke = async <T>(method: string, params: Record<string, unknown>) => {
      if (method === '_image_copy_describe') return { size: bytes.length, width: 10, height: 10, media_type: 'image/png', sha256: sha, chunk_size: 262144, token } as T;
      expect(method).toBe('_image_copy_chunk');
      expect(params.token).toBe(token);
      return invoke<T>('_image_original_chunk', params);
    };
    expect(Buffer.from((await readOriginal(copied, reference, () => {}, true)).bytes)).toEqual(bytes);
  });
  it('rejects out of order chunks and wrong chunk hashes', async () => {
    for (const change of ['offset', 'sha256']) {
      const bad: typeof invoke = async <T>(method: string, params: Record<string, unknown>) => {
        const result = await invoke<Record<string, unknown>>(method, params);
        if (method === '_image_original_chunk') result[change] = change === 'offset' ? 262144 : '0'.repeat(64);
        return result as T;
      };
      await expect(readOriginal(bad, reference, () => {})).rejects.toThrow();
    }
  });
  it('only opens canonical derived product links', async () => {
    const external = async <T>() => ({ url: 'https://www.alibaba.com/product-detail/_123.html?bad=1' }) as T;
    await expect(productUrl(external, { case_id: 'c' }, () => {})).rejects.toThrow();
    await expect(productUrl(external, { url: 'https://example.com' }, () => {})).rejects.toThrow();
  });
});
