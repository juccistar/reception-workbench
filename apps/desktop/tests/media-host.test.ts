// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { createHash } from 'node:crypto';
import { productUrl, readOriginal, validateReference } from '../src/main/media-host';

const bytes = Buffer.alloc(300_000, 7);
const sha = createHash('sha256').update(bytes).digest('hex');
const reference = { kind: 'product' as const, local_product_id: 'product-1', version: 1, sha256: sha };
function invoke<T>(method: string, params: Record<string, unknown>): Promise<T> {
  if (method === '_image_prepare') return Promise.resolve({ token: 'a'.repeat(32) } as T);
  if (method === '_image_cancel') return Promise.resolve({ cancelled: true } as T);
  if (method === '_image_status') return Promise.resolve({ status: 'ready', size: bytes.length, width: 10, height: 10, media_type: 'image/png', sha256: sha, chunk_size: 262144 } as T);
  const offset = params.offset as number;
  return Promise.resolve({ offset, sha256: sha, data_b64: bytes.subarray(offset, offset + 262144).toString('base64') } as T);
}

describe('private media host', () => {
  it('reads and verifies bounded chunks', async () => {
    const image = await readOriginal(invoke, reference, () => {});
    expect(Buffer.from(image.bytes)).toEqual(bytes);
  });
  it('accepts a 20 MiB GIF and rejects an oversized original descriptor', async () => {
    const large = Buffer.alloc(20 * 1024 * 1024, 3);
    const digest = createHash('sha256').update(large).digest('hex');
    const largeRef = { ...reference, sha256: digest };
    const largeInvoke: typeof invoke = async <T>(method: string, params: Record<string, unknown>) => {
      if (method === '_image_status') return { status: 'ready', size: large.length, width: 32768, height: 1, media_type: 'image/gif', sha256: digest, chunk_size: 262144 } as T;
      if (method === '_image_transfer_chunk') { const offset = params.offset as number; return { offset, sha256: digest, data_b64: large.subarray(offset, offset + 262144).toString('base64') } as T; }
      return invoke<T>(method, params);
    };
    expect((await readOriginal(largeInvoke, largeRef, () => {})).bytes.length).toBe(large.length);
    const oversized: typeof invoke = async <T>(method: string, params: Record<string, unknown>) => method === '_image_status'
      ? { status: 'ready', size: large.length + 1, width: 1, height: 1, media_type: 'image/png', sha256: digest, chunk_size: 262144 } as T
      : largeInvoke<T>(method, params);
    await expect(readOriginal(oversized, largeRef, () => {})).rejects.toThrow('描述无效');
  });
  it('rejects forged references and altered chunks', async () => {
    expect(() => validateReference({ ...reference, path: 'x' })).toThrow();
    const corrupt: typeof invoke = async <T>(method: string, params: Record<string, unknown>) => {
      const result = await invoke<Record<string, unknown>>(method, params);
      if (method === '_image_transfer_chunk') result.data_b64 = Buffer.alloc(Math.min(262144, bytes.length - (params.offset as number)), 8).toString('base64');
      return result as T;
    };
    await expect(readOriginal(corrupt, reference, () => {})).rejects.toThrow('完整性');
  });
  it('reads converted PNG through the private token-bound channel', async () => {
    const token = 'a'.repeat(32);
    const copied: typeof invoke = async <T>(method: string, params: Record<string, unknown>) => {
      if (method === '_image_prepare') expect(params.copy).toBe(true);
      if (method === '_image_transfer_chunk') expect(params.token).toBe(token);
      return invoke<T>(method, params);
    };
    expect(Buffer.from((await readOriginal(copied, reference, () => {}, true)).bytes)).toEqual(bytes);
  });
  it('rejects out of order chunks and wrong chunk hashes', async () => {
    for (const change of ['offset', 'sha256']) {
      const bad: typeof invoke = async <T>(method: string, params: Record<string, unknown>) => {
        const result = await invoke<Record<string, unknown>>(method, params);
        if (method === '_image_transfer_chunk') result[change] = change === 'offset' ? 262144 : '0'.repeat(64);
        return result as T;
      };
      await expect(readOriginal(bad, reference, () => {})).rejects.toThrow();
    }
  });
  it('revokes a token returned after cancellation', async () => {
    let release!: (value: { token: string }) => void;
    const pending = new Promise<{ token: string }>(resolve => { release = resolve; });
    const calls: string[] = [];
    const delayed: typeof invoke = <T>(method: string, params: Record<string, unknown>) => {
      calls.push(method);
      return method === '_image_prepare' ? pending as Promise<T> : invoke<T>(method, params);
    };
    const controller = new AbortController();
    const result = readOriginal(delayed, reference, () => {}, false, controller.signal);
    controller.abort();
    await expect(result).rejects.toThrow('取消');
    release({ token: 'a'.repeat(32) });
    await Promise.resolve();
    expect(calls).toContain('_image_cancel');
  });
  it('holds the copy slot until the actual clipboard consumer finishes', async () => {
    let release!: () => void;
    const writing = new Promise<void>(resolve => { release = resolve; });
    let started!: () => void;
    const began = new Promise<void>(resolve => { started = resolve; });
    const first = readOriginal(invoke, reference, () => {}, true, undefined, async () => { started(); await writing; });
    await began;
    await expect(readOriginal(invoke, reference, () => {}, true)).rejects.toThrow('繁忙');
    release(); await first;
    await expect(readOriginal(invoke, reference, () => {}, true)).resolves.toBeDefined();
  });
  it('only opens canonical derived product links', async () => {
    const external = async <T>() => ({ url: 'https://www.alibaba.com/product-detail/_123.html?bad=1' }) as T;
    await expect(productUrl(external, { case_id: 'c' }, () => {})).rejects.toThrow();
    await expect(productUrl(external, { url: 'https://example.com' }, () => {})).rejects.toThrow();
  });
});
