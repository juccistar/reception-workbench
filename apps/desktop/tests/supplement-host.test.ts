// @vitest-environment node
import { describe, expect, it, vi } from 'vitest';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { receivePaths, receiveUploads, validateUploads } from '../src/main/supplement-host';

const uploadFile = (name: string, content = 'hello') => ({ name, content_b64: Buffer.from(content).toString('base64') });
const saved = (n: number) => ({ conversation_id: 'synthetic', revision: n, attachments: [] }) as any;

describe('supplement attachment host', () => {
  it('keeps earlier success when a later backend upload fails, without disclosing the error', async () => {
    const upload = vi.fn().mockResolvedValueOnce(saved(1)).mockRejectedValueOnce(new Error('private path or backend details'));
    const result = await receiveUploads([uploadFile('one.txt'), uploadFile('two.txt')], upload, () => {});
    expect(result.supplement).toEqual(saved(1));
    expect(result.errors).toEqual(['附件保存失败，请检查文件格式、内容及附件数量或容量限制。']);
  });
  it('rejects noncanonical base64 independently while retaining valid neighbours', async () => {
    const upload = vi.fn(async () => saved(1));
    const result = await receiveUploads([uploadFile('one.txt'), { name: 'bad.txt', content_b64: 'Zh==' }, uploadFile('two.txt')], upload, () => {});
    expect(upload).toHaveBeenCalledTimes(2);
    expect(result.errors).toEqual(['附件编码无效。']);
  });
  it('enforces individual, total and count limits', async () => {
    const twenty = { name: 'a.png', content_b64: Buffer.alloc(20 * 1024 * 1024, 1).toString('base64') };
    const upload = vi.fn(async () => saved(1));
    expect(() => validateUploads([twenty])).not.toThrow();
    const result = await receiveUploads([twenty, twenty, twenty, twenty, uploadFile('ok.txt')], upload, () => {});
    expect(upload).toHaveBeenCalledTimes(4);
    expect(result.errors).toEqual(['本批附件总大小不能超过 64 MiB。']);
    expect(() => validateUploads([{ name: 'large', content_b64: Buffer.alloc(20 * 1024 * 1024 + 1).toString('base64') }])).toThrow();
    expect(() => validateUploads(Array(11).fill(uploadFile('small.txt')))).toThrow('10 个');
  });
  it('reads only selected synthetic files, retaining successful files around unreadable paths', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'supplement-test-'));
    try {
      const file = path.join(dir, '中文.txt'); await writeFile(file, 'synthetic');
      const upload = vi.fn(async () => saved(1));
      const result = await receivePaths([file, path.join(dir, 'missing.txt')], upload, () => {});
      expect(upload).toHaveBeenCalledWith(uploadFile('中文.txt', 'synthetic'));
      expect(result.supplement).toEqual(saved(1));
      expect(result.errors).toHaveLength(1);
      expect(result.errors[0]).not.toContain(dir);
    } finally { await rm(dir, { recursive: true, force: true }); }
  });
  it('checks availability before every upload', async () => {
    const ready = vi.fn().mockImplementationOnce(() => {}).mockImplementationOnce(() => { throw new Error('private'); });
    const upload = vi.fn(async () => saved(1));
    const result = await receiveUploads([uploadFile('one'), uploadFile('two')], upload, ready);
    expect(upload).toHaveBeenCalledTimes(1);
    expect(result.supplement).toEqual(saved(1));
    expect(result.errors).toHaveLength(1);
  });
});
