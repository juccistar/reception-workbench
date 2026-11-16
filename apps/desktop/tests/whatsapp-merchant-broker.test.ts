// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest';
import { createHash, createHmac } from 'node:crypto';
import { mkdtemp, readdir, readFile, rm } from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { createWhatsAppMerchantBroker } from '../src/main/whatsapp-merchant-broker';

const encryption = { isEncryptionAvailable: () => true, encryptString: (s: string) => Buffer.from(s), decryptString: (b: Buffer) => b.toString() };
const binding = { seller_id: '123', record_id: '12345678-1234-1234-1234-123456789abc', data_root: path.join(os.tmpdir(), 'business') };
const input = { request_id: 'r', account_id: 'account', chat_id: 'chat', merchant_id: binding.record_id, conversation_id: null, binding_revision: 1 };
const dirs: string[] = [], closers: Array<() => Promise<void>> = [];
async function setup() { const directory = await mkdtemp(path.join(os.tmpdir(), 'wa-broker-')); dirs.push(directory); return { directory, broker: createWhatsAppMerchantBroker({ directory, encryption, timeoutMs: 500 }) }; }
const value = () => ({ seller_id: binding.seller_id, data_root: binding.data_root, conversation_id: null, revision: 'a'.repeat(64), context: { knowledge: [], guidance: [], style: [], collection_rules: {} } });
afterEach(async () => { for (const close of closers.splice(0)) await close(); for (const dir of dirs.splice(0)) await rm(dir, { recursive: true, force: true }); });
it('uses fresh verified reads and binds merchant, root, seller and revision', async () => {
  const { broker } = await setup(); let rev = 'a'.repeat(64), valid = true;
  const read = vi.fn(async () => ({ ...value(), revision: rev }));
  closers.push(await broker.publishVerifiedMerchant(binding, read, () => { if (!valid) throw new Error('revoked'); }));
  expect((await broker.request(binding, input)).revision).toBe(rev); rev = 'b'.repeat(64);
  const lease = await broker.request(binding, input); expect(lease.revision).toBe(rev); expect(lease.expires_at_ms - lease.observed_at_ms).toBeLessThanOrEqual(10000); expect(read).toHaveBeenCalledTimes(2);
  await expect(broker.request(binding, { ...input, merchant_id: 'other' })).rejects.toThrow();
  await expect(broker.request({ ...binding, seller_id: '124' }, input)).rejects.toThrow();
  await expect(broker.request({ ...binding, data_root: path.join(os.tmpdir(), 'elsewhere') }, input)).rejects.toThrow();
  valid = false; await expect(broker.request(binding, input)).rejects.toThrow();
});
it('refuses results after revocation or shutdown during the read', async () => {
  const { broker } = await setup(); let release!: () => void; let started!: () => void;
  const entered = new Promise<void>(r => { started = r; }); const wait = new Promise<void>(r => { release = r; }); let valid = true;
  const close = await broker.publishVerifiedMerchant(binding, async () => { started(); await wait; return value(); }, () => { if (!valid) throw new Error(); }); closers.push(close);
  const pending = broker.request(binding, input); await entered; valid = false; release(); await expect(pending).rejects.toThrow();
  await close(); await expect(broker.request(binding, input)).rejects.toThrow();
});
it('refuses an in-flight read when the publisher closes', async () => {
  const { broker } = await setup(); let release!: () => void; let started!: () => void;
  const entered = new Promise<void>(r => { started = r; }); const wait = new Promise<void>(r => { release = r; });
  const close = await broker.publishVerifiedMerchant(binding, async () => { started(); await wait; return value(); }, () => {}); closers.push(close);
  const pending = broker.request(binding, input); const rejected = expect(pending).rejects.toThrow(); await entered; await close(); release(); await rejected;
});
it('rejects bad MAC and replayed nonces', async () => {
  const { broker, directory } = await setup(); const read = vi.fn(async () => value()); closers.push(await broker.publishVerifiedMerchant(binding, read, () => {}));
  const record = JSON.parse((await readFile(path.join(directory, (await readdir(directory))[0]))).toString());
  const body = JSON.stringify(input), nonce = 'a'.repeat(64), issued_at_ms = Date.now(), digest = (s: string) => createHash('sha256').update(s).digest('hex');
  const proof = createHmac('sha256', Buffer.from(record.key, 'hex')).update(`request|${record.instance}|${digest(JSON.stringify(binding))}|${nonce}|${issued_at_ms}|${digest(body)}`).digest('hex');
  async function send(p: string) { return new Promise<string>(resolve => { const s = net.createConnection(record.endpoint); let out = ''; s.on('data', b => { out += b.toString(); }); s.on('error', () => resolve(out)); s.on('close', () => resolve(out)); s.write(JSON.stringify({ nonce, body, issued_at_ms, proof: p }) + '\n'); }); }
  expect(await send('0'.repeat(64))).toBe(''); expect(await send(proof)).not.toBe(''); expect(await send(proof)).toBe(''); expect(read).toHaveBeenCalledTimes(1);
});
it('searches with a verified lease and checks identity before and after reading', async () => {
  const { broker } = await setup(); let valid = true;
  const search = vi.fn(async () => ({ seller_id: binding.seller_id, data_root: binding.data_root, candidates: [{ conversation_id: 'c1', name: 'Alice', buyer_id: 'buyer-1', revision: 'a'.repeat(64) }] }));
  const validate = vi.fn(() => { if (!valid) throw new Error('revoked'); });
  closers.push(await broker.publishVerifiedMerchant(binding, async () => value(), validate, search));
  const req = { request_id: 'search', account_id: 'account', chat_id: 'chat', merchant_id: binding.record_id, binding_revision: 1, query: 'Alice', limit: 25 };
  const result = await broker.searchCustomers(binding, req);
  expect(result.candidates[0].name).toBe('Alice'); expect(result.instance_id).toMatch(/^[a-f0-9]{32}$/); expect(validate).toHaveBeenCalledTimes(2); expect(search).toHaveBeenCalledWith(req);
  expect((await broker.request(binding, input)).revision).toBe('a'.repeat(64));
  await expect(broker.searchCustomers(binding, { ...req, query: 'x'.repeat(81) })).rejects.toThrow();
  await expect(broker.searchCustomers(binding, { ...req, query: '\n' })).rejects.toThrow();
  await expect(broker.searchCustomers(binding, { ...req, limit: 26 })).rejects.toThrow();
  await expect(broker.searchCustomers(binding, { ...req, merchant_id: 'other' })).rejects.toThrow();
  valid = false; await expect(broker.searchCustomers(binding, req)).rejects.toThrow();
});
it('rejects untrusted search response shapes and missing callback', async () => {
  const { broker } = await setup(); const req = { request_id: 'r', account_id: 'a', chat_id: 'c', merchant_id: binding.record_id, binding_revision: 1, query: '', limit: 1 };
  const close = await broker.publishVerifiedMerchant(binding, async () => value(), () => {}); closers.push(close);
  await expect(broker.searchCustomers(binding, req)).rejects.toThrow(); await close();
  let result: any = { seller_id: '999', data_root: binding.data_root, candidates: [] };
  closers.push(await broker.publishVerifiedMerchant(binding, async () => value(), () => {}, async () => result));
  await expect(broker.searchCustomers(binding, req)).rejects.toThrow();
  result = { seller_id: binding.seller_id, data_root: binding.data_root, candidates: [{ conversation_id: 'c', name: 'Alice', buyer_id: 'buyer-1', revision: 'a'.repeat(64), secret: 'x' }] };
  await expect(broker.searchCustomers(binding, req)).rejects.toThrow();
  result = { seller_id: binding.seller_id, data_root: binding.data_root, candidates: Array(2).fill({ conversation_id: 'c', name: 'Alice', buyer_id: 'buyer-1', revision: 'a'.repeat(64) }) };
  await expect(broker.searchCustomers(binding, req)).rejects.toThrow();
});
it('rejects oversized exports and mismatched response identity', async () => {
  const { broker } = await setup(); let wrong = true;
  closers.push(await broker.publishVerifiedMerchant(binding, async () => wrong ? { ...value(), seller_id: '999' } : { ...value(), context: { ...value().context, knowledge: ['x'.repeat(300000)] } }, () => {}));
  await expect(broker.request(binding, input)).rejects.toThrow(); wrong = false; await expect(broker.request(binding, input)).rejects.toThrow();
});
