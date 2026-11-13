import { createHash } from 'node:crypto';
import type { ImageReference, OriginalImage } from '../shared/media';

const CHUNK = 256 * 1024;
const MAX = 6 * 1024 * 1024;
type Invoke = <T>(method: string, params: Record<string, unknown>) => Promise<T>;
let active = 0;

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

export async function readOriginal(invoke: Invoke, value: unknown, ready: () => void, copy = false): Promise<OriginalImage> {
  validateReference(value);
  if (active >= 2) throw new Error('原图读取繁忙，请稍后重试。');
  active++;
  const deadline = Date.now() + 30000;
  try {
    const checked = async <T>(method: string, params: Record<string, unknown>): Promise<T> => {
      ready();
      const remaining = deadline - Date.now();
      if (remaining <= 0) throw new Error('原图读取超时。');
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        const result = await Promise.race([invoke<T>(method, params), new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('原图读取超时。')), remaining); })]);
        ready();
        return result;
      } finally { if (timer) clearTimeout(timer); }
    };
    const desc = await checked<{ size: number; media_type: string; width: number; height: number; sha256: string; chunk_size: number; token?: string }>(copy ? '_image_copy_describe' : '_image_original_describe', { reference: value });
    if (!desc || !Number.isSafeInteger(desc.size) || desc.size <= 0 || desc.size > (copy ? 80 * 1024 * 1024 : MAX) || !/^[0-9a-f]{64}$/.test(desc.sha256) || (!copy && desc.sha256 !== value.sha256) || (copy && (desc.media_type !== 'image/png' || typeof desc.token !== 'string' || !/^[A-Za-z0-9_-]{32}$/.test(desc.token))) || desc.chunk_size !== CHUNK || !['image/png', 'image/jpeg', 'image/webp'].includes(desc.media_type) || !Number.isSafeInteger(desc.width) || !Number.isSafeInteger(desc.height) || desc.width <= 0 || desc.height <= 0 || desc.width > 8192 || desc.height > 8192 || desc.width * desc.height > 20_000_000) throw new Error('原图描述无效。');
    const bytes = Buffer.alloc(desc.size);
    for (let offset = 0; offset < desc.size; offset += CHUNK) {
      const params: Record<string, unknown> = { reference: value, offset, sha256: desc.sha256 };
      if (copy) params.token = desc.token;
      const part = await checked<{ offset: number; sha256: string; data_b64: string }>(copy ? '_image_copy_chunk' : '_image_original_chunk', params);
      const expected = Math.min(CHUNK, desc.size - offset);
      if (!part || part.offset !== offset || part.sha256 !== desc.sha256 || typeof part.data_b64 !== 'string' || part.data_b64.length !== Math.ceil(expected / 3) * 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(part.data_b64)) throw new Error('原图分块无效。');
      const decoded = Buffer.from(part.data_b64, 'base64');
      if (decoded.length !== expected || decoded.toString('base64') !== part.data_b64) throw new Error('原图分块无效。');
      decoded.copy(bytes, offset);
    }
    if (createHash('sha256').update(bytes).digest('hex') !== desc.sha256) throw new Error('原图完整性校验失败。');
    return { bytes: new Uint8Array(bytes), media_type: desc.media_type, width: desc.width, height: desc.height, sha256: desc.sha256 };
  } finally { active--; }
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
