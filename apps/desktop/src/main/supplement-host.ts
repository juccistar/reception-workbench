import { open } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import path from 'node:path';
import type { Supplement, SupplementUpload, SupplementAttachmentBatch } from '../shared/api';
import { SUPPLEMENT_MAX_FILE_BYTES as MAX_FILE, SUPPLEMENT_MAX_TOTAL_BYTES as MAX_BATCH, SUPPLEMENT_MAX_FILES as MAX_FILES } from '../shared/media-limits';

const READ_ERROR = '附件读取失败，请确认文件可访问且大小不超过 20 MiB。';
const UPLOAD_ERROR = '附件保存失败，请检查文件格式、内容及附件数量或容量限制。';

export function validateConversation(value: unknown): asserts value is string {
  if (typeof value !== 'string' || !value || value.length > 500) throw new Error('客户会话无效。');
}

// This check is also used in preload before large IPC payloads are copied.
export function validateUploads(value: unknown): asserts value is SupplementUpload[] {
  if (!Array.isArray(value) || value.length > MAX_FILES) throw new Error('一次最多上传 10 个附件。');
  let total = 0;
  for (const item of value) {
    if (!item || typeof item !== 'object' || typeof item.name !== 'string' || !item.name || item.name.length > 255 || /[\\/\u0000-\u001f]/.test(item.name) || typeof item.content_b64 !== 'string') throw new Error('附件参数无效。');
    const encoded = item.content_b64;
    if (encoded.length > Math.ceil(MAX_FILE / 3) * 4) throw new Error('单个附件不能超过 20 MiB。');
    if (!encoded || encoded.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) throw new Error('附件编码无效。');
    const size = encoded.length / 4 * 3 - (encoded.endsWith('==') ? 2 : encoded.endsWith('=') ? 1 : 0);
    if (size < 1 || size > MAX_FILE) throw new Error('单个附件不能超过 20 MiB。');
    total += size;
    if (total > MAX_BATCH) throw new Error('本批附件总大小不能超过 64 MiB。');
  }
}

async function readPath(filePath: string): Promise<SupplementUpload> {
  const file = await open(filePath, 'r');
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size < 1 || stat.size > MAX_FILE) throw new Error('size');
    const bytes = Buffer.alloc(stat.size + 1);
    let offset = 0;
    while (offset < bytes.length) {
      const part = await file.read(bytes, offset, bytes.length - offset, offset);
      if (!part.bytesRead) break;
      offset += part.bytesRead;
    }
    if (offset !== stat.size) throw new Error('changed');
    return { name: path.basename(filePath), content_b64: bytes.subarray(0, offset).toString('base64') };
  } finally { await file.close().catch(() => {}); }
}

type Upload = (file: SupplementUpload) => Promise<Supplement>;
export async function receiveUploads(files: unknown, upload: Upload, ready: () => void): Promise<SupplementAttachmentBatch> {
  // Validate each item independently so valid attachments survive malformed neighbours.
  if (!Array.isArray(files)) throw new Error('附件参数无效。');
  const result: SupplementAttachmentBatch = { supplement: null, errors: [] };
  let total = 0;
  for (const [index, file] of files.entries()) {
    if (index >= MAX_FILES) { if (index === MAX_FILES) result.errors.push('一次最多上传 10 个附件。'); break; }
    try {
      validateUploads([file]);
      const bytes = Buffer.from(file.content_b64, 'base64');
      if (bytes.toString('base64') !== file.content_b64) throw new Error('附件编码无效。');
      if (total + bytes.length > MAX_BATCH) { result.errors.push('本批附件总大小不能超过 64 MiB。'); continue; }
      total += bytes.length;
      ready();
      result.supplement = await upload(file);
    } catch (error) {
      result.errors.push(error instanceof Error && ['附件参数无效。', '附件编码无效。', '单个附件不能超过 20 MiB。'].includes(error.message) ? error.message : UPLOAD_ERROR);
    }
  }
  return result;
}

export async function receivePaths(paths: string[], upload: Upload, ready: () => void): Promise<SupplementAttachmentBatch> {
  const result: SupplementAttachmentBatch = { supplement: null, errors: [] };
  let total = 0;
  for (const [index, filePath] of paths.entries()) {
    if (index >= MAX_FILES) { result.errors.push('一次最多上传 10 个附件。'); break; }
    try {
      ready();
      const file = await readPath(filePath);
      validateUploads([file]);
      const size = Buffer.byteLength(file.content_b64, 'base64');
      if (total + size > MAX_BATCH) { result.errors.push('本批附件总大小不能超过 64 MiB。'); continue; }
      total += size;
      ready();
      try { result.supplement = await upload(file); } catch { result.errors.push(UPLOAD_ERROR); }
    } catch { result.errors.push(READ_ERROR); }
  }
  return result;
}

// Static command: no renderer input is passed to PowerShell. JSON output and runtime are bounded.
const CLIPBOARD_COMMAND = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); Add-Type -AssemblyName System.Windows.Forms; $f = [System.Windows.Forms.Clipboard]::GetFileDropList(); if ($f.Count -gt 0) { @($f | Select-Object -First 11) | ConvertTo-Json -Compress }";
export function windowsClipboardPaths(): Promise<string[]> {
  if (process.platform !== 'win32') return Promise.resolve([]);
  const executable = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  return new Promise((resolve, reject) => {
    execFile(executable, ['-NoProfile', '-NonInteractive', '-Sta', '-Command', CLIPBOARD_COMMAND], { timeout: 3000, maxBuffer: 65536, windowsHide: true, encoding: 'utf8' }, (error, stdout) => {
      if (error) return reject(new Error('无法读取剪贴板文件。'));
      try {
        if (!stdout.trim()) return resolve([]);
        const parsed: unknown = JSON.parse(stdout.replace(/^\uFEFF/, ''));
        const items = Array.isArray(parsed) ? parsed : [parsed];
        if (items.length > 11 || items.some(item => typeof item !== 'string' || !path.isAbsolute(item) || item.length > 32767)) throw new Error('invalid');
        resolve(items as string[]);
      } catch { reject(new Error('无法读取剪贴板文件。')); }
    });
  });
}
