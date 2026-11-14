// @vitest-environment node
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { afterEach, expect, it, vi } from 'vitest';
import { WhatsAppDiagnostics } from '../src/main/whatsapp/diagnostics';
const directories: string[] = [];
function folder() { const path = mkdtempSync(join(tmpdir(), 'arw-wa-diagnostics-')); directories.push(path); return path; }
afterEach(() => { vi.restoreAllMocks(); for (const path of directories.splice(0)) rmSync(path, { recursive: true, force: true }); });
it('records an actionable relative frame while omitting source text, credentials and outside paths', () => {
  const path = folder(), log = new WhatsAppDiagnostics(path, '0.3.19');
  const error = new Error('customer-body cookie=private-token QR-private-value https://private.invalid/?secret=private-token');
  error.stack = error.message + '\n    at initialize (E:/ReceptionWorkbench/apps/desktop/src/main/whatsapp/host.ts:12:4)\n    at private_token (C:/Users/private-user/secret.js:1:2)';
  const id = log.failure(1, 'sdk_inject', error, 'sdk_error');
  expect(id).toMatch(/^wa_diag_[0-9a-f-]{36}$/);
  const text = readFileSync(join(path, 'whatsapp-host.jsonl'), 'utf8');
  expect(text).not.toMatch(/private|customer-body|cookie|QR-|https:|E:\/|C:\//);
  expect(JSON.parse(text)).toMatchObject({ diagnostic_id: id, phase: 'sdk_inject', exception_type: 'Error', frames: [{ file: 'apps/desktop/src/main/whatsapp/host.ts', line: 12, column: 4, function: 'initialize' }] });
});
it('limits repeated notices but keeps a distinct failure identity for a new connection', () => {
  const path = folder(), log = new WhatsAppDiagnostics(path, '0.3.19');
  const first = log.failure(1, 'sdk_callbacks', new TypeError('private'), 'sdk_error');
  expect(log.failure(1, 'sdk_callbacks', new TypeError('different private'), 'sdk_error')).toBe(first);
  expect(log.failure(2, 'sdk_callbacks', new TypeError('private'), 'sdk_error')).not.toBe(first);
  expect(readFileSync(join(path, 'whatsapp-host.jsonl'), 'utf8').trim().split('\n')).toHaveLength(2);
});
it('rotates to three bounded files without adding arbitrary diagnostic content', () => {
  const path = folder(), file = join(path, 'whatsapp-host.jsonl');
  writeFileSync(file, 'x'.repeat(2 * 1024 * 1024));
  writeFileSync(file + '.1', 'previous'); writeFileSync(file + '.2', 'oldest');
  new WhatsAppDiagnostics(path, '0.3.19').event(1, 'cdp_connect', 'completed', 12.6);
  expect(readdirSync(path).sort()).toEqual(['whatsapp-host.jsonl', 'whatsapp-host.jsonl.1', 'whatsapp-host.jsonl.2']);
  expect(statSync(file).size).toBeLessThan(4096);
  expect(readFileSync(file + '.2', 'utf8')).toBe('previous');
  expect(JSON.parse(readFileSync(file, 'utf8'))).toMatchObject({ phase: 'cdp_connect', event: 'completed', elapsed_ms: 13 });
});
it('ignores unavailable storage and never throws during connection cleanup', () => {
  const path = folder(), blocked = join(path, 'not-a-directory'); writeFileSync(blocked, 'occupied');
  const log = new WhatsAppDiagnostics(blocked, '0.3.19');
  expect(() => log.event(1, 'view_setup', 'started')).not.toThrow();
  expect(log.failure(1, 'view_setup', new Error('private'), 'sdk_error')).toBeUndefined();
  expect(new WhatsAppDiagnostics(undefined, '0.3.19').failure(1, 'view_setup', {}, 'sdk_error')).toBeUndefined();
});
it('rejects arbitrary exception names and invalid version metadata', () => {
  const path = folder(), log = new WhatsAppDiagnostics(path, 'private customer name');
  const error = new Error('private'); error.name = 'PrivateToken';
  log.failure(1, 'app_modules', error, 'app_module_error');
  const text = readFileSync(join(path, 'whatsapp-host.jsonl'), 'utf8');
  expect(text).not.toMatch(/PrivateToken|private customer name/);
  expect(JSON.parse(text)).toMatchObject({ exception_type: 'Error', versions: { app: 'unknown' } });
});
