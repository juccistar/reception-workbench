import { app, BrowserWindow, ipcMain, session } from 'electron';
import path from 'node:path';
import { mkdir, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { isWorkbenchPage } from '../../apps/desktop/src/main/trusted-page';
import { ModelSettingsStore } from '../../apps/desktop/src/main/model-settings-store';
import { LauncherRegistry, listLauncherCandidates, readHistoricalMerchants } from '../../apps/desktop/src/main/launcher-registry';

const root = process.env.HARNESS_ROOT!;
const temporary = process.env.HARNESS_TEMP!;
const assets = process.env.HARNESS_ASSETS!;
const encryption = { isEncryptionAvailable: () => true, encryptString: async (s: string) => Buffer.from(s), decryptString: async (b: Buffer) => b.toString() };
const lock = async () => async () => {};
const store = new ModelSettingsStore(path.join(temporary, 'models'), encryption, lock);
const registry = new LauncherRegistry(path.join(temporary, 'registry'), lock);
const tasks = new Map<string, { cancelled: boolean }>();
const legacy = path.join(temporary, 'legacy');
const profiles: Record<string, unknown> = {};
async function addLegacy(seller: string) {
  const state = path.join(legacy, 'profiles', createHash('sha256').update(seller).digest('hex'), 'state');
  const data = path.join(temporary, 'legacy-data', seller);
  await mkdir(state, { recursive: true }); await mkdir(data, { recursive: true });
  await writeFile(path.join(state, 'storage.json'), JSON.stringify({ version: 1, dataRoot: data }));
  profiles[seller] = { sellerId: seller, stateDirectory: state, initialDataRoot: data, status: 'verified' };
  await writeFile(path.join(legacy, 'merchants.json'), JSON.stringify({ format: 1, profiles }));
}
(globalThis as any).addSyntheticLegacy = addLegacy;
async function start() {
  await app.whenReady();
  session.defaultSession.setPermissionRequestHandler((_w, _p, cb) => cb(false));
  session.defaultSession.webRequest.onBeforeRequest((details, cb) => cb({ cancel: !details.url.startsWith('file:') && !details.url.startsWith('devtools:') }));
  if (process.env.HARNESS_MERCHANTS === '1') {
    await addLegacy('20001');
    for (const [seller_id, name] of [['10001', '合成商家甲'], ['10002', '合成商家乙']]) {
      const directories = { registry_directory: path.join(temporary, 'registry', seller_id), state_directory: path.join(temporary, 'state', seller_id), data_root: path.join(temporary, 'data', seller_id) };
      await Promise.all(Object.values(directories).map(directory => mkdir(directory, { recursive: true })));
      await registry.registerVerified({ seller_id, name, ...directories, verified_at: 1700000000000 });
    }
  }
  const file = path.join(assets, 'renderer', 'launcher.html');
  const win = new BrowserWindow({ width: 656, height: 559, show: true, webPreferences: { preload: path.join(assets, 'launcher-preload', 'index.js'), contextIsolation: true, sandbox: true, nodeIntegration: false } });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (event, url) => { if (!isWorkbenchPage(url, file)) event.preventDefault(); });
  const bind = (name: string, fn: (...args: any[]) => any) => ipcMain.handle(name, (event, ...args) => { if (event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame || !isWorkbenchPage(event.senderFrame.url, file)) throw new Error('invalid sender'); return fn(...args); });
  bind('launcher:merchants', async () => (await listLauncherCandidates(registry, [legacy])).map(m => ({ id: m.record_id, name: m.name, account_tail: m.seller_id.slice(-4), status: 'reauthorize', data_root: m.data_root, verified_at: m.verified_at })));
  bind('launcher:import-merchants', async () => {
    // Synthetic native folder choice: only this isolated fixture directory is imported.
    const source = path.join(temporary, 'isolated-merchant-registry');
    const seller = '4008987';
    const state = path.join(source, 'profiles', createHash('sha256').update(seller).digest('hex'), 'state');
    const data = path.join(temporary, 'isolated-merchant-data');
    await mkdir(state, { recursive: true }); await mkdir(data, { recursive: true });
    await writeFile(path.join(state, 'storage.json'), JSON.stringify({ version: 1, dataRoot: data }));
    await writeFile(path.join(source, 'merchants.json'), JSON.stringify({ format: 1, profiles: { [seller]: { sellerId: seller, stateDirectory: state, initialDataRoot: data, status: 'verified' } } }));
    const items = await readHistoricalMerchants(source);
    for (const item of items) await registry.importHistorical(item);
    return items.length;
  });
  bind('launcher:open', () => { throw new Error('合成宿主不打开商家'); });
  bind('launcher:authorization', () => '合成授权说明');
  bind('launcher:copy-authorization', () => {});
  bind('launcher:models:get', () => store.read());
  bind('launcher:models:save', input => store.save(input));
  bind('launcher:models:cancel', (id, purpose) => { const t = tasks.get(`${id}:${purpose}`); if (t) t.cancelled = true; });
  bind('launcher:models:check', async (id, purpose) => {
    const key = `${id}:${purpose}`; if (tasks.has(key)) throw new Error('正在检查');
    const task = { cancelled: false }; tasks.set(key, task);
    try { const { profile } = await store.checkProfile(id); await new Promise(resolve => setTimeout(resolve, 1200)); if (task.cancelled) throw new Error('模型检查已取消。');
      const names = purpose === 'translation' ? ['text', 'translation'] : ['text', 'structure', 'single_image', 'multi_image', 'combined'];
      const caps = Object.fromEntries(names.map(name => [name, { status: 'passed', checked_at: 1700000000000 }]));
      return await store.recordCapabilities(id, profile.config_revision, profile.credential_revision, caps, () => !task.cancelled);
    } finally { tasks.delete(key); }
  });
  win.on('closed', () => app.quit());
  await win.loadFile(file);
}
void start().catch(error => { console.error(error); app.exit(1); });
