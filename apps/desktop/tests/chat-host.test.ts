vi.mock('../src/main/installation-guard', () => ({ acquireInstallationStartGuard: vi.fn(async () => async () => {}) }));
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/react';
import { pathToFileURL } from 'node:url';
import type { ReceptionBridge } from '../src/shared/api';

const host = vi.hoisted(() => ({
  handlers: new Map<string, (...args: any[]) => any>(),
  invoke: vi.fn(async () => ({ job_id: 'synthetic-chat-job' })),
  showMessageBox: vi.fn(async () => ({ response: 1 })),
  ipcInvoke: vi.fn(async () => ({ job_id: 'synthetic-chat-job' })),
  translationSave: vi.fn(), translationGet: vi.fn(),
  exposed: undefined as ReceptionBridge | undefined,
  window: undefined as any,
}));
vi.mock('../src/main/translation-host', () => ({ TranslationHost: class {
  start = vi.fn(async () => {}); stop = vi.fn(); get = host.translationGet; save = host.translationSave;
} }));
vi.mock('node:fs', () => ({ existsSync: () => true, mkdirSync: vi.fn() }));
vi.mock('node:fs/promises', () => ({ mkdir: vi.fn(), readFile: vi.fn(async () => { throw new Error('synthetic empty profile'); }), writeFile: vi.fn(), rename: vi.fn() }));
vi.mock('../../../scripts/data-location.mjs', () => ({
  getBootstrapPaths: () => ({ configFile: 'C:/synthetic-host/storage.json', stateDir: 'C:/synthetic-host', defaultRoot: 'C:/synthetic-business' }),
  resolveDataLocation: () => ({ currentRoot: 'C:/synthetic-business', defaultRoot: 'C:/synthetic-business', configuredRoot: null, configFile: 'C:/synthetic-host/storage.json', source: 'default' }),
  readBootstrap: () => ({ version: 1 }), writeBootstrap: vi.fn(), restartArguments: (args: string[]) => args,
}));
vi.mock('../src/main/storage-host', async original => ({
  ...await original<typeof import('../src/main/storage-host')>(),
  createArchiveParent: vi.fn(async () => {}), acquireStorageOperation: vi.fn(async () => async () => {}),
  runMaintenance: vi.fn(async () => ({ status: 'completed', action: 'prepare', data_root: 'C:/synthetic-business', backup: null })),
}));
vi.mock('../src/main/service-client', () => ({
  BackendError: class extends Error {},
  ServiceClient: class {
    invoke = host.invoke;
    on = vi.fn();
    shutdown = vi.fn(async () => {});
  },
}));
vi.mock('electron', () => ({
  safeStorage: { isEncryptionAvailable: () => false, encryptString: vi.fn(), decryptString: vi.fn() },
  app: { isPackaged: false, setName: vi.fn(), setPath: vi.fn(), requestSingleInstanceLock: () => true, whenReady: async () => {}, on: vi.fn(), quit: vi.fn(), exit: vi.fn() },
  BrowserWindow: class {
    webContents = { mainFrame: { url: '' }, setWindowOpenHandler: vi.fn(), on: vi.fn(), send: vi.fn(), session: { setPermissionRequestHandler: vi.fn() } };
    constructor() { host.window = this; }
    on = vi.fn(); once = vi.fn(); isDestroyed = () => false; setTitle = vi.fn();
    async loadFile(file: string) { this.webContents.mainFrame.url = pathToFileURL(file).href; }
  },
  clipboard: { writeText: vi.fn() }, shell: { openPath: vi.fn(async () => '') },
  dialog: { showMessageBox: host.showMessageBox, showErrorBox: vi.fn() },
  ipcMain: { handle: (name: string, handler: (...args: any[]) => any) => host.handlers.set(name, handler) },
  Menu: { buildFromTemplate: vi.fn(), setApplicationMenu: vi.fn() },
  screen: { getPrimaryDisplay: () => ({ workArea: { width: 1500, height: 1000 } }) },
  contextBridge: { exposeInMainWorld: (_name: string, value: ReceptionBridge) => { host.exposed = value; } },
  ipcRenderer: { invoke: host.ipcInvoke, on: vi.fn(), removeListener: vi.fn() },
}));

const validEvent = () => ({ sender: host.window.webContents, senderFrame: host.window.webContents.mainFrame });
const request = { conversation_id: 'synthetic-customer', draft_id: 'synthetic-chat', expected_revision: 4, client_request_id: 'same-request-through-retry' };

beforeAll(async () => {
  const previousArgs = process.argv;
  process.argv = [...previousArgs, '--live'];
  try { await import('../src/main/index'); } finally { process.argv = previousArgs; }
  await waitFor(() => expect(host.handlers.has('reception:invoke')).toBe(true));
  await import('../src/preload/index');
});

describe('native chat IPC boundary', () => {
  it('forwards the three exact chat methods, preserving the request key without a native send dialog', async () => {
    host.invoke.mockClear(); host.showMessageBox.mockClear();
    const call = host.handlers.get('reception:invoke')!;
    await call(validEvent(), 'begin_manual_edit', { conversation_id: request.conversation_id });
    await call(validEvent(), 'save_chat_draft', { conversation_id: request.conversation_id, body: 'Literal human text\n中文', expected_revision: null, client_mutation_id: 'save-1' });
    await call(validEvent(), 'send_chat', request);
    await call(validEvent(), 'send_chat', request);
    expect(host.invoke.mock.calls.map(call => call)).toEqual([
      ['begin_manual_edit', { conversation_id: request.conversation_id }],
      ['save_chat_draft', { conversation_id: request.conversation_id, body: 'Literal human text\n中文', expected_revision: null, client_mutation_id: 'save-1' }],
      ['send_chat', request], ['send_chat', request],
    ]);
    expect(host.showMessageBox).not.toHaveBeenCalled();
  });

  it.each([null, undefined, 0, -1, 1.5, '4', Number.MAX_SAFE_INTEGER + 1])('rejects an invalid exact send revision (%s) before reaching the service', async expected_revision => {
    host.invoke.mockClear();
    await expect(host.handlers.get('reception:invoke')!(validEvent(), 'send_chat', { ...request, expected_revision })).rejects.toThrow(/版本无效|参数无效/);
    expect(host.invoke).not.toHaveBeenCalled();
  });

  it('rejects raw body injection, missing dedupe keys, shell methods and direct legacy confirmation', async () => {
    const call = host.handlers.get('reception:invoke')!; host.invoke.mockClear();
    await expect(call(validEvent(), 'send_chat', { ...request, body: 'not a saved version' })).rejects.toThrow();
    await expect(call(validEvent(), 'send_chat', { ...request, client_request_id: '' })).rejects.toThrow();
    await expect(call(validEvent(), 'exec', { command: 'arbitrary' })).rejects.toThrow('不允许');
    await expect(call(validEvent(), 'confirm_send', {})).rejects.toThrow('不允许');
    await expect(call(validEvent(), 'configure_translation_credential', { api_key: 'synthetic' })).rejects.toThrow('不允许');
    await expect(call(validEvent(), 'save_translation_settings', { enabled: true })).rejects.toThrow('不允许');
    await expect(call(validEvent(), 'validate_translation_settings', { base_url: 'https://example.com/v1' })).rejects.toThrow('不允许');
    expect(host.invoke).not.toHaveBeenCalled();
  });

  it('rejects foreign webContents, subframes and foreign file URLs', async () => {
    const call = host.handlers.get('reception:invoke')!; host.invoke.mockClear();
    await expect(call({ ...validEvent(), sender: {} }, 'send_chat', request)).rejects.toThrow('无效的操作来源');
    await expect(call({ ...validEvent(), senderFrame: { ...host.window.webContents.mainFrame } }, 'send_chat', request)).rejects.toThrow('无效的操作来源');
    const previous = host.window.webContents.mainFrame.url;
    host.window.webContents.mainFrame.url = 'https://example.invalid/';
    try { await expect(call(validEvent(), 'send_chat', request)).rejects.toThrow('当前页面不允许'); }
    finally { host.window.webContents.mainFrame.url = previous; }
    expect(host.invoke).not.toHaveBeenCalled();
  });

  it('keeps storage paths behind host-owned operations and validates their sender', async () => {
    const read = host.handlers.get('reception:storage:get')!;
    expect(read(validEvent()).current_root).toBe('C:/synthetic-business');
    expect(() => read({ ...validEvent(), sender: {} })).toThrow('无效的操作来源');
    await expect(host.handlers.get('reception:storage:choose')!(validEvent(), 'shell')).rejects.toThrow('用途无效');
    await expect(host.handlers.get('reception:storage:open')!(validEvent(), 'C:/arbitrary')).rejects.toThrow('目录类型无效');
    host.showMessageBox.mockClear();
    const result = await host.handlers.get('reception:storage:manage')!(validEvent(), { action: 'migrate', target: 'relative' });
    expect(result.status).toBe('failed'); expect(host.showMessageBox).not.toHaveBeenCalled();
    expect(() => host.handlers.get('reception:storage:manage')!({ ...validEvent(), senderFrame: {} }, { action: 'backup' })).toThrow('无效的操作来源');
  });

  it('keeps update operations behind validated IPC and outside the business method list', async () => {
    const get = host.handlers.get('reception:update:get')!;
    await expect(get(validEvent())).resolves.toMatchObject({ installed: false, status: 'unavailable' });
    await expect(get({ ...validEvent(), sender: {} })).rejects.toThrow('无效的操作来源');
    await expect(host.handlers.get('reception:update:install')!({ ...validEvent(), senderFrame: {} })).rejects.toThrow('无效的操作来源');
    await expect(host.handlers.get('reception:invoke')!(validEvent(), 'installUpdate', {})).rejects.toThrow('不允许');
    host.ipcInvoke.mockClear();
    await host.exposed!.saveUpdateSettings!({ feed_url: 'https://updates.example.test/stable/' });
    expect(host.ipcInvoke).toHaveBeenCalledWith('reception:update:settings', { feed_url: 'https://updates.example.test/stable/' });
  });

  it('routes translation saves through the shared coordinator and preserves both revisions', async () => {
    const settings = { enabled: false, base_url: 'http://127.0.0.1:3425/v1', model: 'sample', revision: 1, shared_revision: 4 };
    const info = { ...settings, revision: 2, has_key: false };
    host.invoke.mockClear(); host.translationSave.mockClear();
    host.translationSave.mockResolvedValueOnce(info);
    await expect(host.handlers.get('reception:translation:save')!(validEvent(), settings)).resolves.toEqual(info);
    expect(host.translationSave).toHaveBeenCalledWith(settings);
    expect(host.invoke).not.toHaveBeenCalled();
  });

  it('returns coordinator errors on dedicated IPC and rejects foreign translation senders', async () => {
    const save = host.handlers.get('reception:translation:save')!;
    const settings = { enabled: false, base_url: 'http://remote.example.com/v1', model: 'sample', revision: 1 };
    host.translationSave.mockClear();
    host.translationSave.mockRejectedValueOnce(new Error('本机网关可使用 HTTP'));
    await expect(save(validEvent(), settings)).rejects.toThrow('本机网关可使用 HTTP');
    host.translationSave.mockClear();
    expect(() => save({ ...validEvent(), sender: {} }, { ...settings, base_url: 'http://127.0.0.1:3425/v1' })).toThrow('无效的操作来源');
    expect(host.translationSave).not.toHaveBeenCalled();
  });

  it('copies only host-generated launch instructions and never forwards renderer commands', async () => {
    host.ipcInvoke.mockClear();
    await (host.exposed!.getAccioLaunchInfo as any)({ executable: 'never-accepted.exe' });
    await (host.exposed!.copyAccioLaunchInstructions as any)({ command: 'never-executed' });
    expect(host.ipcInvoke.mock.calls).toEqual([['reception:launch:get'], ['reception:launch:copy']]);
    await expect(host.handlers.get('reception:invoke')!(validEvent(), 'copyAccioLaunchInstructions', {})).rejects.toThrow('不允许');
  });

  it('keeps the frozen preload bridge narrow and validates versions before IPC', async () => {
    const bridge = host.exposed!; host.ipcInvoke.mockClear();
    expect(Object.isFrozen(bridge)).toBe(true);
    await bridge.invoke('send_chat', request);
    expect(host.ipcInvoke).toHaveBeenCalledWith('reception:invoke', 'send_chat', request);
    host.ipcInvoke.mockClear();
    await expect(bridge.invoke('send_chat', { ...request, expected_revision: null })).rejects.toThrow('版本无效');
    await expect(bridge.invoke('shell' as any, {})).rejects.toThrow('不允许');
    await expect(bridge.invoke('confirm_send', {})).rejects.toThrow('不允许');
    expect(host.ipcInvoke).not.toHaveBeenCalled();
    expect(Object.keys(bridge).sort()).toEqual(['chooseWorkctl', 'confirmSend', 'getPreferences', 'invoke', 'onEvent', 'savePreferences', 'getStorageInfo', 'chooseStorageDirectory', 'openStorageDirectory', 'manageStorage', 'getAccioLaunchInfo', 'copyAccioLaunchInstructions', 'getUpdateInfo', 'saveUpdateSettings', 'checkForUpdates', 'downloadUpdate', 'installUpdate', 'getTranslationSettings', 'saveTranslationSettings', 'checkTranslationProvider', 'clearTranslationCache'].sort());
  });
});
