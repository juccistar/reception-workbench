import { app, session, clipboard } from 'electron';
// Record the production copy handler's complete payload without touching the user's clipboard.
clipboard.writeText = (text: string) => { (globalThis as any).copiedInstructions = text; };
import path from 'node:path';
import { mkdir } from 'node:fs/promises';
import { runLauncher } from '../../apps/desktop/src/main/launcher-host';
import { createLauncherRegistry } from '../../apps/desktop/src/main/launcher-registry';
import { createWhatsAppAccountStore } from '../../apps/desktop/src/main/whatsapp-account-store';
import { createWindowsLauncherActivation } from '../../apps/desktop/src/main/launcher-activation';
import { createWindowsWhatsAppMerchantBroker } from '../../apps/desktop/src/main/whatsapp-merchant-broker';
const temporary = process.env.HARNESS_TEMP!;
if (!temporary || !path.isAbsolute(temporary)) throw new Error('Isolated fixture directory required');
if (process.env.HARNESS_CANDIDATE_RESOURCES) {
  Object.defineProperty(app, 'isPackaged', { value: true });
  Object.defineProperty(process, 'resourcesPath', { value: process.env.HARNESS_CANDIDATE_RESOURCES });
}
const closures: Array<() => Promise<void>> = [];
let closing = false;
(globalThis as any).seedAccounts = async () => {
  const registry = await createLauncherRegistry(), activation = await createWindowsLauncherActivation(), broker = await createWindowsWhatsAppMerchantBroker();
  const records = [];
  for (const [seller_id, name] of [['10001', '合成公司甲'], ['10002', '合成公司乙' + '长名称'.repeat(50)]]) {
    const directories = { registry_directory: path.join(temporary, 'merchants', seller_id), state_directory: path.join(temporary, 'states', seller_id), data_root: path.join(temporary, 'data', seller_id) };
    await Promise.all(Object.values(directories).map(directory => mkdir(directory, { recursive: true })));
    const record = await registry.registerVerified({ seller_id, name, ...directories, verified_at: 1700000000000 });
    records.push(record);
    const validate = () => { if (closing) throw new Error('Fixture closed'); };
    closures.push(await activation.publishVerifiedWindow(record, async () => { validate(); }, validate));
    closures.push(await broker.publishVerifiedMerchant(record, async request => ({ seller_id, data_root: record.data_root, conversation_id: request.conversation_id, revision: 'a'.repeat(64), context: { knowledge: [], guidance: [], style: [], collection_rules: {}, ...(request.conversation_id ? { international_background: { messages: [], summary: { body: '合成国际站客户背景' }, requirements: { product: { value: '合成收纳盒', evidence_ids: ['synthetic-only'] }, quantity: { value: '100 件', evidence_ids: [] } }, supplement: { body: '' } } } : {}) } }), validate, async request => ({ seller_id, data_root: record.data_root, candidates: request.query.includes('迟到') ? await new Promise(resolve => setTimeout(() => resolve([{ conversation_id: 'synthetic-late', name: '迟到客户', buyer_id: 'synthetic-late-buyer', revision: 'b'.repeat(64) }]), 900)) : [{ conversation_id: 'synthetic-customer', name: '合成国际站客户', buyer_id: 'synthetic-buyer', revision: 'b'.repeat(64) }] })));
  }
  const store = await createWhatsAppAccountStore();
  const accounts = [await store.create('商务账号甲'), await store.create('商务账号乙')];
  return { merchants: records.map(record => ({ id: record.record_id, name: record.name })), accounts: accounts.map(record => record.id) };
};
(globalThis as any).quitProbe = async () => {
  closing = true;
  for (const close of closures.splice(0)) await close();
  app.quit();
};
void app.whenReady().then(() => session.defaultSession.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !details.url.startsWith('file:') && !details.url.startsWith('devtools:') })));
void runLauncher().catch(error => { console.error(error instanceof Error ? error.message : 'Isolated launcher failed'); app.exit(1); });
