import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { lstat, mkdir, open, readdir, unlink } from 'node:fs/promises';
import net from 'node:net';
import path from 'node:path';
import { resolveModelSettingsDirectory } from './model-settings-store';
import { createWindowsUserEncryption } from './windows-user-encryption';
import { createMetadataDecrypter } from './metadata-decryption';
import type { CredentialEncryption } from './translation-credentials';

export interface ActivationBinding { seller_id: string; record_id: string; data_root: string }
export interface LauncherActivationOptions { directory: string; encryption: CredentialEncryption; timeoutMs?: number }
type Stored = { version: 1; binding: ActivationBinding; instance: string; key: string; endpoint: string };
const LIMIT = 8192, MAX_CONNECTIONS = 16, MAX_RECORDS = 128;
const hex = (bytes: number) => randomBytes(bytes).toString('hex');
const digest = (value: string) => createHash('sha256').update(value).digest('hex');
function bindingOf(value: ActivationBinding): ActivationBinding {
  if (!value || !/^[1-9][0-9]{0,127}$/.test(value.seller_id) || !/^[a-f0-9-]{36}$/.test(value.record_id) ||
      typeof value.data_root !== 'string' || !path.isAbsolute(value.data_root) || path.resolve(value.data_root) !== value.data_root ||
      /^(?:\\\\|\/\/)/.test(value.data_root) || /[\x00-\x1f\x7f]/.test(value.data_root) || value.data_root === path.parse(value.data_root).root || value.data_root.length > 4096) throw new Error('launcher_activation_invalid_binding');
  return { seller_id: value.seller_id, record_id: value.record_id, data_root: value.data_root };
}
function bindingHash(binding: ActivationBinding): string { return digest(JSON.stringify(binding)); }
function mac(key: string, text: string): string { return createHmac('sha256', Buffer.from(key, 'hex')).update(text).digest('hex'); }
function equal(a: unknown, b: string): boolean { return typeof a === 'string' && /^[a-f0-9]{64}$/.test(a) && timingSafeEqual(Buffer.from(a, 'hex'), Buffer.from(b, 'hex')); }
function endpointFor(directory: string, instance: string): string {
  return process.platform === 'win32' ? `\\\\.\\pipe\\reception-activation-${instance}` : path.join(directory, `${instance}.sock`);
}
async function checkDirectory(directory: string, create: boolean): Promise<void> {
  if (!path.isAbsolute(directory) || path.resolve(directory) !== directory || /^(?:\\\\|\/\/)/.test(directory)) throw new Error('launcher_activation_invalid_directory');
  const root = path.parse(directory).root; let current = root;
  for (const part of directory.slice(root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    try { const st = await lstat(current); if (!st.isDirectory() || st.isSymbolicLink()) throw new Error('launcher_activation_invalid_directory'); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT' || !create) throw error;
      try { await mkdir(current, { mode: 0o700 }); } catch (e) { if ((e as NodeJS.ErrnoException).code !== 'EEXIST') throw e; }
      const st = await lstat(current); if (!st.isDirectory() || st.isSymbolicLink()) throw new Error('launcher_activation_invalid_directory');
    }
  }
}
function message(socket: net.Socket, timeout: number): Promise<string> {
  return new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0), done = false;
    const timer = setTimeout(() => finish(), timeout);
    const finish = (result?: string) => { if (done) return; done = true; clearTimeout(timer); socket.off('data', onData); socket.off('error', onError); socket.off('close', onError); result === undefined ? reject(new Error('launcher_activation_io')) : resolve(result); };
    const onError = () => finish();
    const onData = (chunk: Buffer) => { buffer = Buffer.concat([buffer, chunk]); if (buffer.length > LIMIT) return finish(); const n = buffer.indexOf(10); if (n >= 0) { if (n !== buffer.length - 1) return finish(); finish(buffer.subarray(0, n).toString('utf8')); } };
    socket.on('data', onData); socket.once('error', onError); socket.once('close', onError);
  });
}

export function createLauncherActivation(options: LauncherActivationOptions) {
  const { directory, encryption } = options;
  const decryptMetadata = createMetadataDecrypter(encryption);
  const timeout = options.timeoutMs ?? 3000;
  if (!Number.isInteger(timeout) || timeout < 10 || timeout > 10000) throw new Error('launcher_activation_invalid_timeout');
  async function readRecord(file: string, binding: ActivationBinding): Promise<Stored> {
    const stat = await lstat(file); if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size < 1 || stat.size > LIMIT) throw new Error('invalid');
    const f = await open(file, 'r'); let bytes: Buffer;
    try { const st = await f.stat(); if (!st.isFile() || st.nlink !== 1 || st.size < 1 || st.size > LIMIT || st.ino !== stat.ino || st.dev !== stat.dev) throw new Error('invalid'); bytes = Buffer.alloc(st.size); const read = await f.read(bytes, 0, bytes.length, 0); if (read.bytesRead !== bytes.length) throw new Error('invalid'); } finally { await f.close(); }
    const record: Stored = JSON.parse(await decryptMetadata(bytes));
    if (!record || record.version !== 1 || JSON.stringify(bindingOf(record.binding)) !== JSON.stringify(binding) || !/^[a-f0-9]{32}$/.test(record.instance) || !/^[a-f0-9]{64}$/.test(record.key) || record.endpoint !== endpointFor(directory, record.instance) || path.basename(file) !== `${bindingHash(binding)}-${record.instance}.bin`) throw new Error('invalid');
    return record;
  }
  async function request(bindingInput: ActivationBinding, command: 'probe' | 'activate'): Promise<boolean> {
    const binding = bindingOf(bindingInput);
    const deadline = Date.now() + timeout;
    try {
      if (!encryption.isEncryptionAvailable()) return false;
      await checkDirectory(directory, false);
      const names = (await readdir(directory)).filter(name => name.startsWith(`${bindingHash(binding)}-`) && /^[a-f0-9]{64}-[a-f0-9]{32}\.bin$/.test(name));
      if (names.length > MAX_RECORDS) return false;
      for (const name of names) {
        try {
          const remaining = deadline - Date.now(); if (remaining <= 0) return false;
          let timer: ReturnType<typeof setTimeout> | undefined;
          const record = await Promise.race([readRecord(path.join(directory, name), binding), new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('timeout')), remaining); })]).finally(() => { if (timer) clearTimeout(timer); });
          if (Date.now() >= deadline) return false;
          const socket = net.createConnection(record.endpoint); socket.on('error', () => {});
          try {
            const nonce = hex(32), body = `request|${command}|${nonce}|${bindingHash(binding)}|${record.instance}`;
            const response = message(socket, Math.max(1, deadline - Date.now()));
            socket.write(JSON.stringify({ command, nonce, proof: mac(record.key, body) }) + '\n');
            const result = JSON.parse(await response);
            if (result.ok === true && equal(result.proof, mac(record.key, `response|${command}|${nonce}|${bindingHash(binding)}|${record.instance}|true`))) return true;
          } finally { socket.destroy(); }
        } catch { /* stale or invalid records do not grant authority */ }
      }
    } catch { return false; }
    return false;
  }
  async function publishVerifiedWindow(bindingInput: ActivationBinding, activate: () => void | Promise<void>, validate?: () => void | Promise<void>): Promise<() => Promise<void>> {
    const binding = bindingOf(bindingInput); if (typeof activate !== 'function' || !encryption.isEncryptionAvailable()) throw new Error('launcher_activation_unavailable');
    await checkDirectory(directory, true);
    const instance = hex(16), key = hex(32), endpoint = endpointFor(directory, instance), record: Stored = { version: 1, binding, instance, key, endpoint };
    const file = path.join(directory, `${bindingHash(binding)}-${instance}.bin`);
    const sockets = new Set<net.Socket>(); let closed = false;
    const server = net.createServer(socket => {
      socket.on('error', () => {});
      if (closed || sockets.size >= MAX_CONNECTIONS) { socket.destroy(); return; }
      sockets.add(socket); socket.once('close', () => sockets.delete(socket));
      void (async () => {
        try {
          const req = JSON.parse(await message(socket, timeout));
          if (closed || (req.command !== 'probe' && req.command !== 'activate') || typeof req.nonce !== 'string' || !/^[a-f0-9]{64}$/.test(req.nonce) || !equal(req.proof, mac(key, `request|${req.command}|${req.nonce}|${bindingHash(binding)}|${instance}`))) return;
          await Promise.race([Promise.resolve().then(() => { if (closed || socket.destroyed) throw new Error(); return req.command === 'activate' ? activate() : validate?.(); }), new Promise<never>((_, reject) => { const timer = setTimeout(() => reject(new Error()), timeout); timer.unref(); })]);
          if (closed || socket.destroyed) return;
          socket.end(JSON.stringify({ ok: true, proof: mac(key, `response|${req.command}|${req.nonce}|${bindingHash(binding)}|${instance}|true`) }) + '\n');
        } catch { /* malformed, expired or failed requests */ } finally { socket.destroySoon(); }
      })();
    });
    await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(endpoint, () => { server.off('error', reject); resolve(); }); });
    server.on('error', () => {});
    try {
      const encrypted = await encryption.encryptString(JSON.stringify(record)); if (!Buffer.isBuffer(encrypted) || encrypted.length < 1 || encrypted.length > LIMIT) throw new Error();
      await checkDirectory(directory, false);
      const f = await open(file, 'wx', 0o600); try { await f.writeFile(encrypted); await f.sync(); } finally { await f.close(); }
    } catch (error) { for (const socket of sockets) socket.destroy(); await new Promise<void>(resolve => server.close(() => resolve())); await unlink(file).catch(() => {}); throw error; }
    return async () => {
      if (closed) return; closed = true;
      for (const socket of sockets) socket.destroy();
      await new Promise<void>(resolve => server.close(() => resolve()));
      try { await checkDirectory(directory, false); const current = await readRecord(file, binding); if (current.instance === instance && current.key === key) await unlink(file); } catch { /* preserve records that no longer belong to this instance */ }
    };
  }
  return { publishVerifiedWindow, probe: (record: ActivationBinding) => request(record, 'probe'), activate: (record: ActivationBinding) => request(record, 'activate') };
}

/** Host only. No renderer-controlled directory or encryption override is used here. */
export async function createWindowsLauncherActivation() {
  const models = await resolveModelSettingsDirectory();
  return createLauncherActivation({ directory: path.join(path.dirname(models), 'launcher-activation'), encryption: createWindowsUserEncryption() });
}
