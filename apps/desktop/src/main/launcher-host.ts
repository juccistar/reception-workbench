import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, type IpcMainInvokeEvent } from 'electron';
import { existsSync } from 'node:fs';
import { ModelCheckHost } from './model-check-host';
import { MODEL_CAPABILITIES, type ModelCapability, type ModelCapabilityState } from '../shared/model-settings';
import path from 'node:path';
import { isWorkbenchPage } from './trusted-page';
import { createLauncherRegistry } from './launcher-registry';
import { createWindowsLauncherActivation } from './launcher-activation';
import { createModelSettingsStore } from './model-settings-store';
import { buildAccioLaunchInfo } from './launch-command';
import type { ModelSettingsSaveInput } from '../shared/model-settings';
import { version as programVersion } from '../../../../package.json';

/** Launcher runs before any business service or business storage is initialized. */
export async function runLauncher(): Promise<void> {
  await app.whenReady();
  const file = path.join(__dirname, '../renderer/launcher.html');
  const window = new BrowserWindow({ width: 656, height: 559, minWidth: 440, minHeight: 400, title: '接待工作台', backgroundColor: '#ffffff', show: false, autoHideMenuBar: true, webPreferences: { preload: path.join(__dirname, '../launcher-preload/index.js'), contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true } });
  const valid = (event: IpcMainInvokeEvent) => { if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame || !isWorkbenchPage(event.senderFrame.url, file)) throw new Error('无效的操作来源。'); };
  const info = () => buildAccioLaunchInfo({ version: programVersion, distribution: app.isPackaged ? 'installed' : 'development', executable: process.execPath, developmentEntry: app.isPackaged ? undefined : path.resolve(process.argv[1]) });
  let registryPromise: ReturnType<typeof createLauncherRegistry> | undefined;
  let storePromise: ReturnType<typeof createModelSettingsStore> | undefined;
  const registry = () => registryPromise ??= createLauncherRegistry();
  const store = () => storePromise ??= createModelSettingsStore();
  ipcMain.handle('launcher:merchants', async event => {
    valid(event);
    const items = await (await registry()).list();
    const activation = await createWindowsLauncherActivation().catch(() => null);
    return Promise.all(items.map(async item => ({ id: item.record_id, name: item.name, account_tail: item.seller_id.slice(-4), status: activation && await activation.probe(item).catch(() => false) ? 'opened' as const : 'reauthorize' as const, data_root: item.data_root, verified_at: item.verified_at })));
  });
  ipcMain.handle('launcher:open', async (event, id: unknown) => {
    valid(event);
    const item = typeof id === 'string' ? (await (await registry()).list()).find(entry => entry.record_id === id) : undefined;
    if (!item) throw new Error('商家记录不存在。');
    const activation = await createWindowsLauncherActivation().catch(() => null);
    if (!activation || !await activation.activate(item).catch(() => false)) throw new Error('当前窗口不可用，请从该商家的 Accio Work 授权会话重新启动。');
  });
  ipcMain.handle('launcher:authorization', event => { valid(event); return info().instructions; });
  ipcMain.handle('launcher:copy-authorization', event => { valid(event); clipboard.writeText(info().instructions); });
  ipcMain.handle('launcher:models:get', async event => { valid(event); return (await store()).read(); });
  ipcMain.handle('launcher:models:save', async (event, input: ModelSettingsSaveInput) => { valid(event); if (!input || typeof input !== 'object' || JSON.stringify(input).length > 262144) throw new Error('配置无效。'); return (await store()).save(input); });
  const root = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '../../..');
  const executable = app.isPackaged ? path.join(process.resourcesPath, 'backend', 'reception-backend.exe') : [process.env.RECEPTION_PYTHON, path.join(root, 'services/reception/.venv/Scripts/python.exe'), path.join(root, 'services/reception/.venv/bin/python')].find(v => v && existsSync(v)) || '';
  const checker = new ModelCheckHost({ executable, prefix: app.isPackaged ? [] : ['-I', '-u', path.join(root, 'services/reception/run.py')], cwd: root, env: process.env, timeoutMs: 180000 });
  const checks = new Map<string, { cancelled: boolean }>();
  const checkKey = (id: string, purpose: string) => `${id}:${purpose}`;
  ipcMain.handle('launcher:models:cancel', (event, id: unknown, purpose: unknown) => {
    valid(event);
    if (typeof id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(id) || (purpose !== 'reception' && purpose !== 'translation')) throw new Error('模型检查参数无效。');
    const task = checks.get(checkKey(id, purpose));
    if (task) task.cancelled = true;
    checker.cancel(id, purpose);
  });
  ipcMain.handle('launcher:models:check', async (event, id: unknown, purpose: unknown) => {
    valid(event);
    if (typeof id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(id) || (purpose !== 'reception' && purpose !== 'translation')) throw new Error('模型检查参数无效。');
    const key = checkKey(id, purpose);
    if (checks.has(key)) throw new Error('该用途的检查正在进行。');
    const task = { cancelled: false }; checks.set(key, task);
    try {
    const answer = await dialog.showMessageBox(window, { type: 'warning', title: '模型能力检查', message: '检查会向所选模型发送合成内容，可能产生 API 费用。', buttons: ['取消', '开始检查'], defaultId: 0, cancelId: 0, noLink: true });
    if (answer.response !== 1 || task.cancelled) throw new Error('已取消模型检查。');
    try {
      const settings = await store();
      const { profile, api_key } = await settings.checkProfile(id);
      if (task.cancelled) throw new Error('model_check_cancelled');
      const identity = { profile_id: id, config_revision: profile.config_revision, credential_revision: profile.credential_revision, purpose: purpose as 'reception' | 'translation' };
      const result = await checker.check(identity, { format: 'model-check-v1', purpose, profile: { profile_id: id, protocol: profile.protocol, base_url: profile.base_url, model: profile.model, config_revision: profile.config_revision, credential_revision: profile.credential_revision }, api_key }) as Record<string, unknown>;
      if (!result || result.format !== 'model-check-result-v1' || !['passed', 'failed', 'cancelled'].includes(String(result.status)) || typeof result.code !== 'string' || !/^[a-z0-9_]{1,80}$/.test(result.code) || result.purpose !== purpose || result.profile_id !== id || result.config_revision !== profile.config_revision || result.credential_revision !== profile.credential_revision || !result.capabilities || typeof result.capabilities !== 'object' || Array.isArray(result.capabilities)) throw new Error();
      if (result.status === 'cancelled') throw new Error();
      const caps = result.capabilities as Record<string, unknown>;
      if (Object.keys(caps).sort().join(',') !== [...MODEL_CAPABILITIES].sort().join(',')) throw new Error();
      const update: Partial<Record<ModelCapability, ModelCapabilityState>> = {};
      for (const key of MODEL_CAPABILITIES) {
        const item = caps[key] as ModelCapabilityState;
        if (!item || !['passed', 'failed', 'not_checked'].includes(item.status) || (item.status === 'not_checked' ? item.checked_at !== null : !Number.isSafeInteger(item.checked_at) || Number(item.checked_at) < 0)) throw new Error();
        if ((purpose === 'translation' ? ['text', 'translation'] : ['text', 'structure', 'single_image', 'multi_image', 'combined']).includes(key)) update[key] = { status: item.status, checked_at: item.checked_at };
      }
      return await settings.recordCapabilities(id, profile.config_revision, profile.credential_revision, update, () => !task.cancelled);
    } catch { throw new Error(task.cancelled ? '模型检查已取消。' : '模型检查未完成或配置已变化，请重新检查。'); }
    } finally { if (checks.get(key) === task) checks.delete(key); }
  });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event, url) => { if (!isWorkbenchPage(url, file)) event.preventDefault(); });
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  Menu.setApplicationMenu(null);
  window.once('ready-to-show', () => window.show());
  window.on('closed', () => { for (const task of checks.values()) task.cancelled = true; checker.close(); app.quit(); });
  await window.loadFile(file);
}
