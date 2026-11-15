import { createHash } from 'node:crypto';
import type { ImageReference, OriginalImage } from '../shared/media';

const CHUNK = 256 * 1024;
const MAX = 6 * 1024 * 1024;
type Invoke = <T>(method: string, params: Record<string, unknown>) => Promise<T>;
let active = 0;
let copying = false;

export function validateReference(value: unknown): asserts value is ImageReference {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('图片引用无效。');
  const ref = value as Record<string, unknown>;
  const keys: Record<string, string[]> = {
    message: ['kind', 'conversation_id', 'message_id', 'source_version', 'sha256'],
    requirement: ['kind', 'case_id', 'media_id', 'source_version', 'sha256'],
    product: ['kind', 'local_product_id', 'version', 'sha256'],
    supplement: ['kind', 'conversation_id', 'attachment_id', 'sha256'],
  };
  const allowed = typeof ref.kind === 'string' ? keys[ref.kind] : undefined;
  if (!allowed || Object.keys(ref).length !== allowed.length || !allowed.every(k => Object.hasOwn(ref, k)) ||
      allowed.some(k => k === 'version' ? !Number.isSafeInteger(ref[k]) || (ref[k] as number) < 0 : typeof ref[k] !== 'string' || !(ref[k] as string).length || (ref[k] as string).length > 500) ||
      !/^[0-9a-f]{64}$/.test(ref.sha256 as string) || ('source_version' in ref && !/^[0-9a-f]{64}$/.test(ref.source_version as string))) throw new Error('图片引用无效。');
}

export async function readOriginal(invoke: Invoke, value: unknown, ready: () => void, copy = false, signal?: AbortSignal, consume?: (image: OriginalImage) => Promise<void>): Promise<OriginalImage> {
  validateReference(value);
  if (active >= 2 || (copy && copying)) throw new Error('原图读取繁忙，请稍后重试。');
  if (signal?.aborted) throw new Error('原图读取已取消。');
  active++;
  if (copy) copying = true;
  const deadline = Date.now() + 30000;
  let token: string | undefined;
  let cancelled = false;
  const pending = new Set<Promise<unknown>>();
  const track = <T>(promise: Promise<T>): Promise<T> => {
    pending.add(promise);
    void promise.finally(() => pending.delete(promise)).catch(() => {});
    return promise;
  };
  const cancel = () => {
    cancelled = true;
    if (token) void invoke('_image_cancel', { token }).catch(() => {});
  };
  signal?.addEventListener('abort', cancel, { once: true });
  try {
    const checked = async <T>(method: string, params: Record<string, unknown>, existing?: Promise<T>): Promise<T> => {
      if (cancelled) throw new Error('原图读取已取消。');
      ready();
      const remaining = deadline - Date.now();
      if (remaining <= 0) throw new Error('原图读取超时。');
      let timer: ReturnType<typeof setTimeout> | undefined;
      let abort: (() => void) | undefined;
      try {
        const result = await Promise.race([existing ?? track(invoke<T>(method, params)), new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('原图读取超时。')), remaining); abort = () => reject(new Error('原图读取已取消。')); signal?.addEventListener('abort', abort, { once: true }); })]);
        if (cancelled) throw new Error('原图读取已取消。');
        ready();
        return result;
      } finally { if (timer) clearTimeout(timer); if (abort) signal?.removeEventListener('abort', abort); }
    };
    ready();
    const preparing = track(invoke<{ token: string }>('_image_prepare', { reference: value, copy }));
    // A timed out prepare can still return a token, which must be revoked.
    void preparing.then(result => { if (typeof result?.token === 'string') { token = result.token; if (cancelled) void invoke('_image_cancel', { token }).catch(() => {}); } }, () => {});
    const prepared = await checked('_image_prepare', {}, preparing);
    token = prepared.token;
    if (!/^[A-Za-z0-9_-]{32}$/.test(token)) throw new Error('原图令牌无效。');
    let desc: { status: string; error?: string; size: number; media_type: string; width: number; height: number; sha256: string; chunk_size: number };
    do {
      desc = await checked('_image_status', { token });
      if (desc.status === 'pending') await new Promise(resolve => setTimeout(resolve, 50));
    } while (desc.status === 'pending');
    if (desc.status === 'failed') throw new Error(desc.error === 'image_too_large' ? '原图过大。' : '原图不可用。');
    if (desc.status !== 'ready' || !Number.isSafeInteger(desc.size) || desc.size <= 0 || desc.size > (copy ? 80 * 1024 * 1024 : MAX) || !/^[0-9a-f]{64}$/.test(desc.sha256) || (!copy && desc.sha256 !== value.sha256) || (copy && desc.media_type !== 'image/png') || desc.chunk_size !== CHUNK || !['image/png', 'image/jpeg', 'image/webp'].includes(desc.media_type) || !Number.isSafeInteger(desc.width) || !Number.isSafeInteger(desc.height) || desc.width <= 0 || desc.height <= 0 || desc.width > 8192 || desc.height > 8192 || desc.width * desc.height > 20_000_000) throw new Error('原图描述无效。');
    const bytes = Buffer.alloc(desc.size);
    for (let offset = 0; offset < desc.size; offset += CHUNK) {
      const params: Record<string, unknown> = { token, offset, sha256: desc.sha256 };
      const part = await checked<{ offset: number; sha256: string; data_b64: string }>('_image_transfer_chunk', params);
      const expected = Math.min(CHUNK, desc.size - offset);
      if (!part || part.offset !== offset || part.sha256 !== desc.sha256 || typeof part.data_b64 !== 'string' || part.data_b64.length !== Math.ceil(expected / 3) * 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(part.data_b64)) throw new Error('原图分块无效。');
      const decoded = Buffer.from(part.data_b64, 'base64');
      if (decoded.length !== expected || decoded.toString('base64') !== part.data_b64) throw new Error('原图分块无效。');
      decoded.copy(bytes, offset);
    }
    if (createHash('sha256').update(bytes).digest('hex') !== desc.sha256) throw new Error('原图完整性校验失败。');
    const image = { bytes: new Uint8Array(bytes), media_type: desc.media_type, width: desc.width, height: desc.height, sha256: desc.sha256 };
    if (consume) await consume(image);
    return image;
  } finally {
    cancel();
    signal?.removeEventListener('abort', cancel);
    // The caller may observe cancellation immediately, but the slot remains held
    // until every in-flight host request has actually settled.
    if (pending.size) void Promise.allSettled([...pending]).then(() => { active--; if (copy) copying = false; });
    else { active--; if (copy) copying = false; }
  }
}

export async function productUrl(invoke: Invoke, value: unknown, ready: () => void): Promise<string> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('商品引用无效。');
  const ref = value as Record<string, unknown>;
  const keys = Object.keys(ref);
  if (keys.length !== 1 || !['case_id', 'local_product_id'].includes(keys[0]) || typeof ref[keys[0]] !== 'string' || !(ref[keys[0]] as string).length || (ref[keys[0]] as string).length > 500) throw new Error('商品引用无效。');
  ready();
  const result = await invoke<{ url: string }>('_product_link_for_reference', { reference: ref });
  ready();
  if (!result || typeof result.url !== 'string' || !/^https:\/\/www\.alibaba\.com\/product-detail\/_([1-9][0-9]{0,24})\.html$/.test(result.url)) throw new Error('商品链接无效。');
  return result.url;
}
