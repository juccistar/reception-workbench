import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { lstat, mkdir, open, readdir, unlink } from 'node:fs/promises';
import net from 'node:net';
import path from 'node:path';
import type { WhatsAppContextRequest, WhatsAppMerchantContext } from '../shared/whatsapp-account';
import type { ActivationBinding } from './launcher-activation';
import type { CredentialEncryption } from './translation-credentials';
import { resolveModelSettingsDirectory } from './model-settings-store';
import { createWindowsUserEncryption } from './windows-user-encryption';

type Exported = Pick<WhatsAppMerchantContext, 'seller_id' | 'data_root' | 'conversation_id' | 'revision' | 'context'>;
type Stored = { version: 1; binding: ActivationBinding; instance: string; key: string; endpoint: string };
type Options = { directory: string; encryption: CredentialEncryption; timeoutMs?: number };
const LIMIT = 256 * 1024, RECORD_LIMIT = 8192, MAX_CONNECTIONS = 16, MAX_RECORDS = 128, MAX_NONCES = 4096, LEASE_MS = 5000;
const hex = (n: number) => randomBytes(n).toString('hex');
const hash = (s: string) => createHash('sha256').update(s).digest('hex');
const mac = (key: string, s: string) => createHmac('sha256', Buffer.from(key, 'hex')).update(s).digest('hex');
const equal = (a: unknown, b: string) => typeof a === 'string' && /^[a-f0-9]{64}$/.test(a) && timingSafeEqual(Buffer.from(a, 'hex'), Buffer.from(b, 'hex'));
function bindingOf(v: ActivationBinding): ActivationBinding {
  if (!v || !/^[1-9][0-9]{0,127}$/.test(v.seller_id) || !/^[a-f0-9-]{36}$/.test(v.record_id) || typeof v.data_root !== 'string' || !path.isAbsolute(v.data_root) || path.resolve(v.data_root) !== v.data_root || /^(?:\\\\|\/\/)/.test(v.data_root) || /[\x00-\x1f\x7f]/.test(v.data_root) || v.data_root === path.parse(v.data_root).root || v.data_root.length > 4096) throw new Error('wa_broker_invalid_binding');
  return { seller_id: v.seller_id, record_id: v.record_id, data_root: v.data_root };
}
function requestOf(v: WhatsAppContextRequest, binding: ActivationBinding): WhatsAppContextRequest {
  const id = (s: unknown) => typeof s === 'string' && s.trim().length > 0 && s.length <= 300 && !/[\x00-\x1f\x7f]/.test(s);
  if (!v || Object.keys(v).sort().join(',') !== 'account_id,binding_revision,chat_id,conversation_id,merchant_id,request_id' || !id(v.request_id) || !id(v.account_id) || !id(v.chat_id) || v.merchant_id !== binding.record_id || !(v.conversation_id === null || id(v.conversation_id)) || !Number.isSafeInteger(v.binding_revision) || v.binding_revision < 0) throw new Error('wa_broker_invalid_request');
  return { request_id: v.request_id, account_id: v.account_id, chat_id: v.chat_id, merchant_id: v.merchant_id, conversation_id: v.conversation_id, binding_revision: v.binding_revision };
}
const bindingHash = (b: ActivationBinding) => hash(JSON.stringify(b));
const endpointFor = (directory: string, instance: string) => process.platform === 'win32' ? `\\\\.\\pipe\\reception-wa-merchant-${instance}` : path.join(directory, `${instance}.sock`);
async function directoryCheck(directory: string, create: boolean) {
  if (!path.isAbsolute(directory) || path.resolve(directory) !== directory || /^(?:\\\\|\/\/)/.test(directory)) throw new Error('wa_broker_directory');
  const root = path.parse(directory).root; let current = root;
  for (const part of directory.slice(root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    try { const st = await lstat(current); if (!st.isDirectory() || st.isSymbolicLink()) throw new Error('wa_broker_directory'); }
    catch (e) { if ((e as NodeJS.ErrnoException).code !== 'ENOENT' || !create) throw e; try { await mkdir(current, { mode: 0o700 }); } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error; } const st = await lstat(current); if (!st.isDirectory() || st.isSymbolicLink()) throw new Error('wa_broker_directory'); }
  }
}
function message(socket: net.Socket, timeout: number): Promise<string> {
  return new Promise((resolve, reject) => {
    let chunks = Buffer.alloc(0), done = false;
    const timer = setTimeout(() => finish(), timeout);
    const finish = (value?: string) => { if (done) return; done = true; clearTimeout(timer); socket.off('data', data); socket.off('error', error); socket.off('close', error); value === undefined ? reject(new Error('wa_broker_io')) : resolve(value); };
    const error = () => finish();
    const data = (chunk: Buffer) => { chunks = Buffer.concat([chunks, chunk]); if (chunks.length > LIMIT) return finish(); const pos = chunks.indexOf(10); if (pos >= 0) { if (pos !== chunks.length - 1) return finish(); finish(chunks.subarray(0, pos).toString('utf8')); } };
    socket.on('data', data); socket.once('error', error); socket.once('close', error);
  });
}
function deadline<T>(fn: () => Promise<T> | T, timeout: number): Promise<T> {
  return new Promise((resolve, reject) => { const timer = setTimeout(() => reject(new Error('wa_broker_timeout')), timeout); Promise.resolve().then(fn).then(resolve, reject).finally(() => clearTimeout(timer)); });
}
function exported(value: Exported, binding: ActivationBinding, req: WhatsAppContextRequest): Exported {
  if (!value || value.seller_id !== binding.seller_id || value.data_root !== binding.data_root || value.conversation_id !== req.conversation_id || !/^[a-f0-9]{64}$/.test(value.revision) || !value.context || !Array.isArray(value.context.knowledge) || !Array.isArray(value.context.guidance) || !Array.isArray(value.context.style) || !('collection_rules' in value.context)) throw new Error('wa_broker_identity');
  return { seller_id: value.seller_id, data_root: value.data_root, conversation_id: value.conversation_id, revision: value.revision, context: value.context };
}

export function createWhatsAppMerchantBroker({ directory, encryption, timeoutMs = 3000 }: Options) {
  if (!Number.isInteger(timeoutMs) || timeoutMs < 10 || timeoutMs > 10000) throw new Error('wa_broker_timeout');
  async function readRecord(file: string, binding: ActivationBinding): Promise<Stored> {
    const stat = await lstat(file); if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size < 1 || stat.size > RECORD_LIMIT) throw new Error('wa_broker_record');
    const f = await open(file, 'r'); let bytes: Buffer;
    try { const st = await f.stat(); if (!st.isFile() || st.nlink !== 1 || st.size !== stat.size || st.ino !== stat.ino || st.dev !== stat.dev) throw new Error('wa_broker_record'); bytes = Buffer.alloc(st.size); if ((await f.read(bytes, 0, bytes.length, 0)).bytesRead !== bytes.length) throw new Error('wa_broker_record'); } finally { await f.close(); }
    const v: Stored = JSON.parse(await encryption.decryptString(bytes));
    if (!v || v.version !== 1 || JSON.stringify(bindingOf(v.binding)) !== JSON.stringify(binding) || !/^[a-f0-9]{32}$/.test(v.instance) || !/^[a-f0-9]{64}$/.test(v.key) || v.endpoint !== endpointFor(directory, v.instance) || path.basename(file) !== `${bindingHash(binding)}-${v.instance}.bin`) throw new Error('wa_broker_record');
    return v;
  }
  // validate must check the live merchant service and the authoritative account/chat binding,
  // including its conversation and revision. It runs both before and after every read.
  async function publishVerifiedMerchant(input: ActivationBinding, readContext: (request: WhatsAppContextRequest) => Promise<Exported>, validate: (request: WhatsAppContextRequest) => void | Promise<void>): Promise<() => Promise<void>> {
    const binding = bindingOf(input); if (!encryption.isEncryptionAvailable() || typeof readContext !== 'function' || typeof validate !== 'function') throw new Error('wa_broker_unavailable');
    await directoryCheck(directory, true);
    const instance = hex(16), key = hex(32), endpoint = endpointFor(directory, instance), file = path.join(directory, `${bindingHash(binding)}-${instance}.bin`);
    const record: Stored = { version: 1, binding, instance, key, endpoint }, sockets = new Set<net.Socket>(), nonces = new Map<string, number>(); let closed = false;
    const server = net.createServer(socket => {
      socket.on('error', () => {}); if (closed || sockets.size >= MAX_CONNECTIONS) { socket.destroy(); return; }
      sockets.add(socket); socket.once('close', () => sockets.delete(socket));
      void (async () => {
        const until = Date.now() + timeoutMs;
        try {
          const envelope = JSON.parse(await message(socket, timeoutMs));
          const now = Date.now();
          if (!envelope || typeof envelope.body !== 'string' || !/^[a-f0-9]{64}$/.test(envelope.nonce) || !Number.isSafeInteger(envelope.issued_at_ms) || envelope.issued_at_ms > now || now - envelope.issued_at_ms > 30000 || !equal(envelope.proof, mac(key, `request|${instance}|${bindingHash(binding)}|${envelope.nonce}|${envelope.issued_at_ms}|${hash(envelope.body)}`))) return;
          for (const [nonce, expiry] of nonces) if (expiry < now) nonces.delete(nonce);
          if (nonces.has(envelope.nonce) || nonces.size >= MAX_NONCES) return;
          nonces.set(envelope.nonce, envelope.issued_at_ms + 30000);
          const req = requestOf(JSON.parse(envelope.body), binding);
          const check = () => { if (closed || socket.destroyed || Date.now() >= until) throw new Error('wa_broker_expired'); };
          check(); await deadline(() => validate(req), Math.max(1, until - Date.now())); check();
          const result = await deadline(() => readContext(req), Math.max(1, until - Date.now())); check();
          const value = exported(result, binding, req);
          await deadline(() => validate(req), Math.max(1, until - Date.now())); check();
          const observed = Date.now();
          const lease: WhatsAppMerchantContext = { format: 'wa-merchant-context-v1', merchant_id: binding.record_id, ...value, instance_id: instance, observed_at_ms: observed, expires_at_ms: observed + LEASE_MS };
          const body = JSON.stringify(lease), proof = mac(key, `response|${instance}|${bindingHash(binding)}|${envelope.nonce}|${hash(body)}`), wire = JSON.stringify({ body, proof }) + '\n';
          if (Buffer.byteLength(wire) <= LIMIT) socket.end(wire);
        } catch { /* fail closed */ } finally { socket.destroySoon(); }
      })();
    });
    await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(endpoint, () => { server.off('error', reject); resolve(); }); }); server.on('error', () => {});
    try { const bytes = await encryption.encryptString(JSON.stringify(record)); if (!Buffer.isBuffer(bytes) || !bytes.length || bytes.length > RECORD_LIMIT) throw new Error('wa_broker_record'); await directoryCheck(directory, false); const f = await open(file, 'wx', 0o600); try { await f.writeFile(bytes); await f.sync(); } finally { await f.close(); } }
    catch (e) { for (const s of sockets) s.destroy(); await new Promise<void>(resolve => server.close(() => resolve())); await unlink(file).catch(() => {}); throw e; }
    return async () => { if (closed) return; closed = true; for (const s of sockets) s.destroy(); await new Promise<void>(resolve => server.close(() => resolve())); try { await directoryCheck(directory, false); const v = await readRecord(file, binding); if (v.key === key && v.instance === instance) await unlink(file); } catch { /* preserve replaced records */ } };
  }
  async function request(input: ActivationBinding, requestInput: WhatsAppContextRequest): Promise<WhatsAppMerchantContext> {
    const binding = bindingOf(input), req = requestOf(requestInput, binding), until = Date.now() + timeoutMs;
    if (!encryption.isEncryptionAvailable()) throw new Error('wa_broker_unavailable');
    await deadline(() => directoryCheck(directory, false), Math.max(1, until - Date.now()));
    const names = (await deadline(() => readdir(directory), Math.max(1, until - Date.now()))).filter(n => n.startsWith(`${bindingHash(binding)}-`) && /^[a-f0-9]{64}-[a-f0-9]{32}\.bin$/.test(n));
    if (names.length > MAX_RECORDS) throw new Error('wa_broker_unavailable');
    for (const name of names) {
      try {
        if (Date.now() >= until) break;
        const v = await deadline(() => readRecord(path.join(directory, name), binding), Math.max(1, until - Date.now()));
        if (Date.now() >= until) break;
        const socket = net.createConnection(v.endpoint); socket.on('error', () => {});
        try {
          const nonce = hex(32), body = JSON.stringify(req), issued_at_ms = Date.now(), proof = mac(v.key, `request|${v.instance}|${bindingHash(binding)}|${nonce}|${issued_at_ms}|${hash(body)}`);
          const pending = message(socket, Math.max(1, until - Date.now())); socket.write(JSON.stringify({ nonce, body, issued_at_ms, proof }) + '\n');
          const response = JSON.parse(await pending);
          if (typeof response.body !== 'string' || !equal(response.proof, mac(v.key, `response|${v.instance}|${bindingHash(binding)}|${nonce}|${hash(response.body)}`))) continue;
          const lease: WhatsAppMerchantContext = JSON.parse(response.body); exported(lease, binding, req);
          const now = Date.now(); if (lease.format !== 'wa-merchant-context-v1' || lease.merchant_id !== binding.record_id || lease.instance_id !== v.instance || !Number.isSafeInteger(lease.observed_at_ms) || !Number.isSafeInteger(lease.expires_at_ms) || lease.observed_at_ms > now || lease.expires_at_ms <= now || lease.expires_at_ms - lease.observed_at_ms > 10000 || now >= until) continue;
          return lease;
        } finally { socket.destroy(); }
      } catch { /* invalid or stale instance */ }
    }
    throw new Error('wa_broker_unavailable');
  }
  return { publishVerifiedMerchant, request };
}

export async function createWindowsWhatsAppMerchantBroker() {
  const models = await resolveModelSettingsDirectory();
  return createWhatsAppMerchantBroker({ directory: path.join(path.dirname(models), 'whatsapp-merchant-broker'), encryption: createWindowsUserEncryption() });
}
