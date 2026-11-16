import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, Tray, type IpcMainInvokeEvent, type MenuItemConstructorOptions } from 'electron';
import { randomUUID } from 'node:crypto';
import { createLauncherPreferences } from './launcher-preferences';
import { modelBudgetEnvironment } from './model-budget-environment';
import { ChromeWhatsAppHost } from './whatsapp/chrome-host';
import { CustomerPanelController } from './whatsapp/customer-panel';
import type { WhatsAppContext } from '../shared/whatsapp';
import { mkdir } from 'node:fs/promises';
import { resolveModelSettingsDirectory } from './model-settings-store';
import { WhatsAppAccountManager } from './whatsapp-account-manager';
import { createWhatsAppAccountStore } from './whatsapp-account-store';
import { createWindowsWhatsAppMerchantBroker } from './whatsapp-merchant-broker';
import { ServiceClient } from './service-client';
import { ModelConfigHost } from './model-config-host';
import { existsSync } from 'node:fs';
import { ModelCheckHost } from './model-check-host';
import { MODEL_CAPABILITIES, type ModelCapability, type ModelCapabilityState } from '../shared/model-settings';
import path from 'node:path';
import { isWorkbenchPage } from './trusted-page';
import { createLauncherRegistry, historicalRegistryDirectories, listLauncherCandidates, readHistoricalMerchants, assertCompleteLauncherSources } from './launcher-registry';
import { createWindowsLauncherActivation } from './launcher-activation';
import { launcherCompanyName } from './launcher-company-name';
import { createModelSettingsStore } from './model-settings-store';
import { buildAccioLaunchInfo } from './launch-command';
import type { ModelSettingsSaveInput } from '../shared/model-settings';
import { version as programVersion } from '../../../../package.json';

/** Launcher runs before any business service or business storage is initialized. */
export async function runLauncher(): Promise<void> {
  const modelsDirectory = await resolveModelSettingsDirectory();
  const budgetEnvironment = await modelBudgetEnvironment(process.env);
  const preferences = await createLauncherPreferences();
  const userData = path.join(path.dirname(modelsDirectory), 'launcher-host');
  await mkdir(userData, { recursive: true });
  app.setName('客户接待工作台');
  app.setPath('userData', userData);
  if (!app.requestSingleInstanceLock()) { app.quit(); return; }
  await app.whenReady();
  const file = path.join(__dirname, '../renderer/launcher.html');
  const window = new BrowserWindow({ width: 900, height: 680, minWidth: 440, minHeight: 400, title: '接待工作台', backgroundColor: '#ffffff', show: false, autoHideMenuBar: true, webPreferences: { preload: path.join(__dirname, '../launcher-preload/index.js'), contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true } });
  const valid = (event: IpcMainInvokeEvent) => { if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame || !isWorkbenchPage(event.senderFrame.url, file)) throw new Error('无效的操作来源。'); };
  const info = () => buildAccioLaunchInfo({ version: programVersion, distribution: app.isPackaged ? 'installed' : 'development', executable: process.execPath, developmentEntry: app.isPackaged ? undefined : path.resolve(process.argv[1]) });
  let registryPromise: ReturnType<typeof createLauncherRegistry> | undefined;
  let storePromise: ReturnType<typeof createModelSettingsStore> | undefined;
  const registry = () => registryPromise ??= createLauncherRegistry();
  const store = () => storePromise ??= createModelSettingsStore();
  let historicalDirectories: ReturnType<typeof historicalRegistryDirectories> | undefined;
  const candidates = async () => listLauncherCandidates(await registry(), await (historicalDirectories ??= historicalRegistryDirectories()));
  let activationPromise: ReturnType<typeof createWindowsLauncherActivation> | undefined;
  const activationHost = () => activationPromise ??= createWindowsLauncherActivation().catch(error => { activationPromise = undefined; throw error; });
  let accountManager: WhatsAppAccountManager | undefined;
  let accountPromise: Promise<WhatsAppAccountManager> | undefined;
  let tray: Tray | undefined;
  let exiting = false;
  let quitting: Promise<void> | undefined;
  const show = () => { if (!window.isDestroyed()) { if (window.isMinimized()) window.restore(); window.show(); window.focus(); } };
  app.on('second-instance', show);
  const quit = () => quitting ??= (async () => {
    try {
      await accountPromise?.catch(() => {});
      await accountManager?.close();
      for (const task of checks.values()) task.cancelled = true;
      checker.close();
      exiting = true;
      tray?.destroy(); tray = undefined;
      window.destroy(); app.quit();
    } catch {
      quitting = undefined;
      show();
      await dialog.showMessageBox(window, { type: 'warning', title: '正在保留发送记录', message: 'WhatsApp 尚未完成退出，请保留当前环境并检查连接后重试。', buttons: ['返回工作台'] });
    }
  })();
  const panels = new Map<string, CustomerPanelController>();
  const refreshTray = async (manager: WhatsAppAccountManager) => {
    const target = tray;
    if (!target || exiting) return;
    const snapshot = await manager.snapshot();
    if (tray !== target || exiting) return;
    const items: MenuItemConstructorOptions[] = [{ label: '打开启动台', click: show }];
    for (const account of snapshot.accounts) items.push({ label: account.label, submenu: [
      { label: '打开窗口', enabled: account.host.page_state === 'available' || account.host.page_state === 'loading', click: () => { void manager.open(account.workspace_id).catch(show); } },
      { label: '断开连接', enabled: manager.isRunning(account.workspace_id), click: () => { void manager.disconnect(account.workspace_id).then(() => refreshTray(manager)).catch(show); } },
    ] });
    items.push({ type: 'separator' }, { label: '退出并暂停接待', click: () => { void quit(); } });
    target.setContextMenu(Menu.buildFromTemplate(items));
  };
  const accounts = () => accountPromise ??= (async () => {
    const broker = await createWindowsWhatsAppMerchantBroker();
    const manager = new WhatsAppAccountManager({ store: await createWhatsAppAccountStore(), broker, merchants: candidates,
      createChrome: (options, runtime) => {
        let chrome: ChromeWhatsAppHost;
        let controller: CustomerPanelController | undefined;
        chrome = new ChromeWhatsAppHost({ ...options,
          extensionDirectory: app.isPackaged ? path.join(process.resourcesPath, 'customer-panel') : path.join(root, 'apps/chrome-extension/customer-panel'),
          onBrowserReady: async (browser, page, extensionId) => {
            const panel = new CustomerPanelController({
              workspaceId: runtime.workspaceId, runtimeInstanceId: runtime.runtimeInstanceId,
              getStatus: () => chrome.getStatus(), resolveCurrentChat: () => chrome.resolveCurrentChat(),
              readState: () => manager.detail(runtime.workspaceId),
              getContext: async chatId => {
                const context = await manager.context({ workspace_id: runtime.workspaceId, chat_id: chatId });
                const snapshot = await manager.detail(runtime.workspaceId);
                const state = snapshot.state;
                const binding = state?.bindings.find(item => item.chat_id === chatId);
                const merchantId = binding ? binding.merchant_id : state?.default_merchant_id;
                if (!state?.connected || !state.account_id || !merchantId || !binding?.conversation_id) return context;
                const merchant = (await candidates()).find(item => item.record_id === merchantId && item.verified_at > 0);
                if (!merchant) return context;
                try {
                  const lease = await broker.request(merchant, { request_id: randomUUID(), account_id: state.account_id, chat_id: chatId, merchant_id: merchantId, conversation_id: binding.conversation_id, binding_revision: binding.revision });
                  return { ...context, international_background: lease.context.international_background } satisfies WhatsAppContext;
                } catch { return context; }
              },
              saveBinding: (input, assertCurrent) => manager.binding(input, assertCurrent), saveSupplement: (input, assertCurrent) => manager.saveSupplement(input, assertCurrent), takeover: input => manager.takeover(input),
              reconcile: operationId => manager.reconcile({ workspace_id: runtime.workspaceId, operation_id: operationId }),
              searchCustomers: async input => {
                const snapshot = await manager.detail(runtime.workspaceId);
                const state = snapshot.state, host = chrome.getStatus();
                const binding = state?.bindings.find(item => item.chat_id === input.chat_id);
                if (!state?.connected || !state.account_id || host.status !== 'ready' || host.account_id !== state.account_id || host.connection_epoch !== state.connection_epoch || (binding?.revision ?? 0) !== input.binding_revision || snapshot.runtime_instance_id !== runtime.runtimeInstanceId) throw new Error('客户或连接已变更。');
                const merchant = (await candidates()).find(item => item.record_id === input.merchant_id && item.verified_at > 0);
                if (!merchant) throw new Error('请先连接该商家。');
                const result = await broker.searchCustomers(merchant, { request_id: randomUUID(), account_id: state.account_id, chat_id: input.chat_id, merchant_id: input.merchant_id, binding_revision: input.binding_revision, query: input.query, limit: 25 });
                return result.candidates;
              },
            });
            controller = panel;
            panels.set(runtime.workspaceId, panel);
            await panel.attach(browser, page, extensionId);
            // A delayed first action must not discard the attached customer bridge.
            await panel.open().catch(() => {});
          },
          onOpen: async () => { await controller?.open(); },
          onBrowserClosed: async () => {
            const panel = controller; controller = undefined;
            await panel?.close();
            if (panels.get(runtime.workspaceId) === panel) panels.delete(runtime.workspaceId);
          },
        });
        return chrome;
      },
      verifyDeclaredScope: async () => assertCompleteLauncherSources(await registry(), await (historicalDirectories ??= historicalRegistryDirectories())),
      available: async record => (await activationHost()).probe(record), version: programVersion,
      service: (workspaceId, directory) => {
        if (!executable) throw new Error('WhatsApp 后台组件缺失，请重新安装完整程序。');
        const env: NodeJS.ProcessEnv = {};
        for (const [key, value] of Object.entries(process.env)) if (/^(SystemRoot|WINDIR|SystemDrive|PATH|PATHEXT|ComSpec|TEMP|TMP|USERPROFILE|LOCALAPPDATA|APPDATA|ProgramFiles|ProgramFiles\(x86\)|ProgramW6432)$/i.test(key)) env[key] = value;
        env.PYTHONIOENCODING = 'utf-8'; env.PYTHONDONTWRITEBYTECODE = '1';
        env.RECEPTION_MODEL_BUDGET_DIRECTORY = budgetEnvironment.RECEPTION_MODEL_BUDGET_DIRECTORY;
        return new ServiceClient(executable, [...(app.isPackaged ? [] : ['-I', '-u', path.join(root, 'services/reception/run.py')]), '--whatsapp-account', '--workspace-id', workspaceId, '--data-dir', directory], root, env);
      },
      models: async invoke => new ModelConfigHost(await store(), invoke),
      confirm: async (message, detail, action = '确认开启') => (await dialog.showMessageBox(window, { type: 'question', title: 'WhatsApp', message, detail, buttons: ['取消', action], defaultId: 0, cancelId: 0, noLink: true })).response === 1,
    });
    const icon = await app.getFileIcon(process.execPath, { size: 'small' });
    if (icon.isEmpty()) throw new Error('系统托盘暂不可用，请稍后重试。');
    tray = new Tray(icon); tray.setToolTip('客户接待工作台 · WhatsApp');
    await refreshTray(manager);
    tray.on('double-click', show);
    accountManager = manager;
    return manager;
  })().catch(error => { accountPromise = undefined; throw error; });
  const changedAccount = async <T>(action: (manager: WhatsAppAccountManager) => Promise<T>): Promise<T> => {
    const manager = await accounts();
    try { return await action(manager); } finally { void refreshTray(manager).catch(() => {}); }
  };
  ipcMain.handle('launcher:wa:get', async event => { valid(event); return (await accounts()).snapshot(); });
  ipcMain.handle('launcher:wa:detail', async (event, workspaceId) => { valid(event); return (await accounts()).detail(workspaceId); });
  ipcMain.handle('launcher:wa:create', async (event, label: unknown) => { valid(event); return changedAccount(manager => manager.create(label)); });
  ipcMain.handle('launcher:wa:rename', async (event, input) => { valid(event); return changedAccount(manager => manager.rename(input)); });
  ipcMain.handle('launcher:wa:connect', async (event, workspaceId) => { valid(event); return changedAccount(manager => manager.connect(workspaceId)); });
  ipcMain.handle('launcher:wa:open', async (event, workspaceId) => { valid(event); return (await accounts()).open(workspaceId); });
  ipcMain.handle('launcher:wa:disconnect', async (event, workspaceId) => { valid(event); return changedAccount(manager => manager.disconnect(workspaceId)); });
  ipcMain.handle('launcher:wa:contacts', async (event, workspaceId) => { valid(event); return (await accounts()).contacts(workspaceId); });
  ipcMain.handle('launcher:wa:verify-legacy', async (event, workspaceId) => { valid(event); return (await accounts()).verifyLegacy(workspaceId); });
  ipcMain.handle('launcher:wa:binding', async (event, input) => { valid(event); return (await accounts()).binding(input); });
  ipcMain.handle('launcher:wa:default', async (event, input) => { valid(event); return (await accounts()).defaultMerchant(input); });
  ipcMain.handle('launcher:wa:reception', async (event, input) => { valid(event); return (await accounts()).reception(input); });
  ipcMain.handle('launcher:wa:takeover', async (event, input) => { valid(event); return (await accounts()).takeover(input); });
  ipcMain.handle('launcher:wa:reconcile', async (event, id) => { valid(event); return (await accounts()).reconcile(id); });
  ipcMain.handle('launcher:wa:context', async (event, id) => { valid(event); return (await accounts()).context(id); });
  ipcMain.handle('launcher:wa:writeback-capabilities', async event => { valid(event); return (await accounts()).writebackCapabilities(); });
  ipcMain.handle('launcher:merchants', async event => {
    valid(event);
    const items = await candidates();
    const activation = await activationHost().catch(() => null);
    const aliases = await preferences.list();
    return Promise.all(items.map(async item => {
      const company = launcherCompanyName(item.name, item.seller_id, item.verified_at > 0);
      const alias = aliases.find(entry => entry.merchant_id === item.record_id);
      return { id: item.record_id, name: alias?.name || company, company_name: company, display_revision: alias?.revision ?? 0, account_tail: item.seller_id.slice(-4), status: item.verified_at > 0 && activation && await activation.probe(item).catch(() => false) ? 'opened' as const : 'reauthorize' as const, data_root: item.data_root, verified_at: item.verified_at };
    }));
  });
  ipcMain.handle('launcher:import-merchants', async event => {
    valid(event);
    try {
      const selected = await dialog.showOpenDialog(window, { title: '导入所选登记目录中的商家', properties: ['openDirectory'] });
      if (selected.canceled || selected.filePaths.length !== 1) return 0;
      const items = await readHistoricalMerchants(selected.filePaths[0]);
      if (!items.length) throw new Error('no_valid_merchant_registry');
      const target = await registry();
      for (const item of items) await target.importHistorical(item);
      return items.length;
    } catch (error) {
      if (error instanceof Error && error.message === 'no_valid_merchant_registry') throw new Error('该目录没有可用的商家登记，请选择 AlibabaReceptionWorkbenchHost 目录。');
      throw new Error('商家登记导入未完成，请检查所选目录和本机写入权限。原业务数据未移动。');
    }
  });
  ipcMain.handle('launcher:open', async (event, id: unknown) => {
    valid(event);
    const item = typeof id === 'string' ? (await candidates()).find(entry => entry.record_id === id) : undefined;
    if (!item || item.verified_at === 0) throw new Error('商家需要从 Accio Work 重新授权。');
    const activation = await activationHost().catch(() => null);
    if (!activation || !await activation.activate(item).catch(() => false)) throw new Error('当前窗口不可用，请从该商家的 Accio Work 授权会话重新启动。');
  });
  ipcMain.handle('launcher:rename', async (event, input: unknown) => {
    valid(event);
    const id = input && typeof input === 'object' && !Array.isArray(input) ? (input as Record<string, unknown>).merchant_id : undefined;
    if (typeof id !== 'string' || !(await candidates()).some(item => item.record_id === id)) throw new Error('商家登记已变化，请刷新后重试。');
    await preferences.rename(input);
  });
  ipcMain.handle('launcher:authorization', event => { valid(event); return info().instructions; });
  ipcMain.handle('launcher:copy-authorization', event => { valid(event); clipboard.writeText(info().instructions); });
  ipcMain.handle('launcher:models:get', async event => { valid(event); return (await store()).read(); });
  ipcMain.handle('launcher:models:save', async (event, input: ModelSettingsSaveInput) => { valid(event); if (!input || typeof input !== 'object' || JSON.stringify(input).length > 262144) throw new Error('配置无效。'); const value = await (await store()).save(input); await accountManager?.modelsChanged(); return value; });
  const root = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '../../..');
  const executable = app.isPackaged ? path.join(process.resourcesPath, 'backend', 'reception-backend.exe') : [process.env.RECEPTION_PYTHON, path.join(root, 'services/reception/.venv/Scripts/python.exe'), path.join(root, 'services/reception/.venv/bin/python')].find(v => v && existsSync(v)) || '';
  const checker = new ModelCheckHost({ executable, prefix: app.isPackaged ? [] : ['-I', '-u', path.join(root, 'services/reception/run.py')], cwd: root, env: budgetEnvironment, timeoutMs: 180000 });
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
  window.on('close', event => {
    if (exiting) return;
    event.preventDefault();
    if (accountManager?.isRunning() && tray) window.hide();
    else void quit();
  });
  app.on('before-quit', event => { if (!exiting) { event.preventDefault(); void quit(); } });
  app.on('activate', show);
  window.on('closed', () => { for (const task of checks.values()) task.cancelled = true; checker.close(); if (exiting) app.quit(); });
  await window.loadFile(file);
}
