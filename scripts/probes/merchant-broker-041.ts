import { spawn, type ChildProcess } from 'node:child_process';
import { mkdtemp, mkdir, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createWhatsAppMerchantBroker } from '../../apps/desktop/src/main/whatsapp-merchant-broker';
import { createWindowsUserEncryption } from '../../apps/desktop/src/main/windows-user-encryption';

const directory = process.env.PROBE_DIRECTORY!;
const root = process.env.PROBE_ROOT || os.tmpdir();
const bindings = [
  { seller_id: '101010101', record_id: '11111111-1111-1111-1111-111111111111', data_root: path.join(root, 'synthetic-a') },
  { seller_id: '202020202', record_id: '22222222-2222-2222-2222-222222222222', data_root: path.join(root, 'synthetic-b') },
];
const request = (i: number) => ({ request_id: `request-${i}`, account_id: 'synthetic-account', chat_id: `chat-${i}`, merchant_id: bindings[i].record_id, conversation_id: `conversation-${i}`, binding_revision: i + 7 });
const revision = (i: number, updated = false) => (i ? 'b' : updated ? 'c' : 'a').repeat(64);
const context = (i: number) => ({ knowledge: [`synthetic-knowledge-${i}`], guidance: [`synthetic-guidance-${i}`], style: [], collection_rules: {} });
const broker = () => createWhatsAppMerchantBroker({ directory, encryption: createWindowsUserEncryption(), timeoutMs: 10000 });
const send = (v: unknown) => process.send?.(v);

async function publisher() {
  const b = broker(); let updated = false;
  const closers: Array<() => Promise<void>> = [];
  try {
    for (let i = 0; i < 2; i++) closers.push(await b.publishVerifiedMerchant(bindings[i], async req => ({ seller_id: bindings[i].seller_id, data_root: bindings[i].data_root, conversation_id: req.conversation_id, revision: revision(i, updated), context: context(i) }), req => { if (JSON.stringify(req) !== JSON.stringify(request(i))) throw Error('binding mismatch'); }));
    send({ event: 'ready' });
    process.on('message', async (message: string) => {
      if (message === 'update') { updated = true; send({ event: 'updated' }); }
      if (message === 'close') { for (const close of closers) await close(); send({ event: 'closed' }); process.disconnect?.(); }
    });
  } catch { for (const close of closers) await close(); send({ event: 'error', stage: 'publish' }); process.exitCode = 1; process.disconnect?.(); }
}
async function requester() {
  const b = broker();
  process.on('message', async (message: string) => {
    try {
      if (message === 'verify' || message === 'verify-updated') {
        for (let i = 0; i < 2; i++) {
          const got = await b.request(bindings[i], request(i));
          if (got.seller_id !== bindings[i].seller_id || got.data_root !== bindings[i].data_root || got.merchant_id !== bindings[i].record_id || got.conversation_id !== request(i).conversation_id || got.revision !== revision(i, message === 'verify-updated') || JSON.stringify(got.context) !== JSON.stringify(context(i))) throw Error('result mismatch');
        }
        for (const altered of [{ ...bindings[0], seller_id: bindings[1].seller_id }, { ...bindings[0], data_root: bindings[1].data_root }, { ...bindings[0], record_id: bindings[1].record_id }]) {
          let rejected = false; try { await b.request(altered, request(0)); } catch { rejected = true; } if (!rejected) throw Error('mixed binding accepted');
        }
        let rejected = false; try { await b.request(bindings[0], { ...request(0), binding_revision: 999 }); } catch { rejected = true; } if (!rejected) throw Error('wrong revision accepted');
        send({ event: 'verified' });
      } else if (message === 'verify-closed') {
        for (let i = 0; i < 2; i++) { let rejected = false; try { await b.request(bindings[i], request(i)); } catch { rejected = true; } if (!rejected) throw Error('closed accepted'); }
        send({ event: 'closed-refused' }); process.disconnect?.();
      }
    } catch { send({ event: 'error', stage: 'request' }); process.exitCode = 1; process.disconnect?.(); }
  });
  send({ event: 'ready' });
}
async function main() {
  const output = path.resolve(process.argv[2]); await mkdir(output, { recursive: true });
  const temp = await mkdtemp(path.join(os.tmpdir(), 'merchant-broker-041-'));
  const env: NodeJS.ProcessEnv = {};
  for (const key of ['SystemRoot', 'WINDIR', 'SystemDrive', 'PATH', 'PATHEXT', 'ComSpec', 'USERPROFILE', 'TEMP', 'TMP']) if (process.env[key]) env[key] = process.env[key];
  env.LOCALAPPDATA = path.join(temp, 'redirected-local'); env.PROBE_ROOT = temp; env.PROBE_DIRECTORY = path.join(temp, 'broker');
  const children: ChildProcess[] = []; const checks: Record<string, boolean> = {};
  const child = (role: string) => { const p = spawn(process.execPath, [__filename, role], { env, windowsHide: true, stdio: ['ignore', 'ignore', 'ignore', 'ipc'] }); children.push(p); return p; };
  const wait = (p: ChildProcess, expected: string) => new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error('timeout')), 60000);
    const message = (v: any) => finish(v?.event === expected ? undefined : new Error(`unexpected event at ${expected}`));
    const exit = () => finish(new Error(`child exited at ${expected}`));
    function finish(error?: Error) { clearTimeout(timer); p.off('message', message); p.off('exit', exit); p.off('error', exit); error ? reject(error) : resolve(); }
    p.once('message', message); p.once('exit', exit); p.once('error', exit);
  });
  let failure: string | undefined;
  try {
    if (process.platform !== 'win32') throw Error('Windows required');
    const pub = child('publisher'); await wait(pub, 'ready');
    const files = await readdir(env.PROBE_DIRECTORY); if (files.length !== 2 || files.some(f => !f.endsWith('.bin'))) throw Error('unexpected metadata');
    for (const file of files) { const bytes = await readFile(path.join(env.PROBE_DIRECTORY, file)); const text = bytes.toString('utf8'); if (bindings.some(b => text.includes(b.seller_id) || text.includes(b.record_id) || text.includes(b.data_root)) || text.includes('"key"') || text.includes('"endpoint"')) throw Error('metadata plaintext'); }
    checks.encrypted_metadata_no_plaintext_markers = true;
    const req = child('requester'); await wait(req, 'ready');
    let pending = wait(req, 'verified'); req.send!('verify'); await pending; checks.cross_process_identity_binding_context_isolation = true;
    pending = wait(pub, 'updated'); pub.send!('update'); await pending;
    pending = wait(req, 'verified'); req.send!('verify-updated'); await pending; checks.cross_process_semantic_revision_update = true;
    pending = wait(pub, 'closed'); pub.send!('close'); await pending;
    pending = wait(req, 'closed-refused'); req.send!('verify-closed'); await pending; checks.publisher_close_refused = true;
  } catch (e) { failure = e instanceof Error ? e.message : 'unknown failure'; }
  finally { for (const p of children) if (p.exitCode === null) p.kill(); await rm(temp, { recursive: true, force: true }); }
  const report = { success: !failure, platform: process.platform, encryption: 'Windows DPAPI CurrentUser via createWindowsUserEncryption', processes: 'separate publisher and requester Node processes under same Windows user', checks, failure: failure ?? null, limitations: ['Synthetic data only; no real merchant service, settings, credentials, accounts or network.', 'No images or history messages were injected or tested.', 'Encrypted metadata checked for selected plaintext markers; no cryptographic audit.'] };
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  process.stdout.write(JSON.stringify({ success: report.success, report: path.join(output, 'report.json'), checks, failure: report.failure }) + '\n'); if (failure) process.exitCode = 1;
}
if (process.argv[2] === 'publisher') void publisher(); else if (process.argv[2] === 'requester') void requester(); else void main();
