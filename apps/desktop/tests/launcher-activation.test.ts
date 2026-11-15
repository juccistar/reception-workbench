// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mkdtemp, readdir, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import net from 'node:net';
import { createHash, createHmac } from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import { createLauncherActivation } from '../src/main/launcher-activation';

const encryption = { isEncryptionAvailable: () => true, encryptString: (s: string) => Buffer.from(s), decryptString: (b: Buffer) => b.toString() };
const binding = { seller_id: '123', record_id: '12345678-1234-1234-1234-123456789abc', data_root: path.join(os.tmpdir(), 'business') };
const dirs: string[] = []; const closers: Array<() => Promise<void>> = [];
async function setup(timeoutMs = 300) { const directory = await mkdtemp(path.join(os.tmpdir(), 'activation-')); dirs.push(directory); return { directory, service: createLauncherActivation({ directory, encryption, timeoutMs }) }; }
afterEach(async () => { for (const close of closers.splice(0)) await close(); for (const dir of dirs.splice(0)) await rm(dir, { recursive: true, force: true }); });
async function publish(service: ReturnType<typeof createLauncherActivation>, callback: () => void | Promise<void> = () => {}) { const close = await service.publishVerifiedWindow(binding, callback); closers.push(close); return close; }

describe('launcher activation', () => {
  it('authenticates a live window and binds all identity fields', async () => {
    const { service } = await setup(); const activate = vi.fn(); await publish(service, activate);
    expect(await service.probe(binding)).toBe(true); expect(activate).not.toHaveBeenCalled();
    expect(await service.activate(binding)).toBe(true); expect(activate).toHaveBeenCalledTimes(1);
    expect(await service.probe({ ...binding, data_root: path.join(os.tmpdir(), 'other') })).toBe(false);
    expect(await service.probe({ ...binding, seller_id: '124' })).toBe(false);
  });
  it('reports a verified window unavailable when its identity check fails', async () => {
    const { service } = await setup();
    let valid = true;
    const close = await service.publishVerifiedWindow(binding, () => { if (!valid) throw new Error('identity_changed'); }, () => { if (!valid) throw new Error('identity_changed'); });
    closers.push(close);
    expect(await service.probe(binding)).toBe(true);
    valid = false;
    expect(await service.probe(binding)).toBe(false);
    expect(await service.activate(binding)).toBe(false);
  });
  it('rejects wrong keys and forged request proofs', async () => {
    const { service, directory } = await setup(); const activate = vi.fn(); await publish(service, activate);
    const file = path.join(directory, (await readdir(directory)).find(n => n.endsWith('.bin'))!);
    const original = await readFile(file); const record = JSON.parse(original.toString());
    const socket = net.createConnection(record.endpoint); socket.on('error', () => {});
    socket.write(JSON.stringify({ command: 'activate', nonce: 'a'.repeat(64), proof: 'b'.repeat(64) }) + '\n');
    await new Promise<void>(resolve => socket.once('close', () => resolve())); expect(activate).not.toHaveBeenCalled();
    record.key = 'a'.repeat(64); await writeFile(file, JSON.stringify(record));
    expect(await service.probe(binding)).toBe(false); expect(await service.activate(binding)).toBe(false); expect(activate).not.toHaveBeenCalled();
    await writeFile(file, original);
  });
  it('rejects a response authenticated for a different challenge', async () => {
    const { service, directory } = await setup(); const close = await publish(service);
    const file = path.join(directory, (await readdir(directory)).find(n => n.endsWith('.bin'))!);
    const bytes = await readFile(file); const record = JSON.parse(bytes.toString()); await close(); await writeFile(file, bytes);
    const hash = createHash('sha256').update(JSON.stringify(binding)).digest('hex');
    const server = net.createServer(socket => { socket.on('error', () => {}); socket.once('data', () => {
      const proof = createHmac('sha256', Buffer.from(record.key, 'hex')).update(`response|probe|${'a'.repeat(64)}|${hash}|${record.instance}|true`).digest('hex');
      socket.end(JSON.stringify({ ok: true, proof }) + '\n');
    }); });
    await new Promise<void>(resolve => server.listen(record.endpoint, resolve));
    try { expect(await service.probe(binding)).toBe(false); } finally { await new Promise<void>(resolve => server.close(() => resolve())); }
  });
  it('does not treat stale or oversized records as a live window', async () => {
    const { service, directory } = await setup(); const close = await publish(service);
    const file = path.join(directory, (await readdir(directory)).find(n => n.endsWith('.bin'))!); const old = await readFile(file);
    await close(); await writeFile(file, old); expect(await service.probe(binding)).toBe(false);
    await writeFile(file, Buffer.alloc(9000)); expect(await service.probe(binding)).toBe(false);
  });
  it('closing an old instance preserves a newer instance record', async () => {
    const { service, directory } = await setup(); const first = await publish(service); const callback = vi.fn(); await publish(service, callback);
    await first(); expect((await readdir(directory)).filter(n => n.endsWith('.bin'))).toHaveLength(1);
    expect(await service.activate(binding)).toBe(true); expect(callback).toHaveBeenCalledOnce();
  });
  it('times out a slow activation and never reports success later', async () => {
    const { service } = await setup(80); await publish(service, () => new Promise(resolve => setTimeout(resolve, 200)));
    expect(await service.activate(binding)).toBe(false); expect(await service.probe(binding)).toBe(true);
  });
  it('rejects linked activation directories', async () => {
    const { directory } = await setup(); const link = path.join(directory, 'link');
    try { await symlink(directory, link, process.platform === 'win32' ? 'junction' : 'dir'); } catch (e) { if (process.platform === 'win32') return; throw e; }
    const service = createLauncherActivation({ directory: link, encryption });
    await expect(service.publishVerifiedWindow(binding, () => {})).rejects.toThrow(); expect(await service.probe(binding)).toBe(false);
  });
});
