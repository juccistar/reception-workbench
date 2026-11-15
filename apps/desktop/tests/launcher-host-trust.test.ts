// @vitest-environment node
import { pathToFileURL } from 'node:url';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const state = vi.hoisted(() => ({
  handlers: new Map<string, (...args: any[]) => any>(),
  window: undefined as any,
  file: '',
  directory: '',
  navigate: undefined as any,
  showOpenDialog: vi.fn(),
  readHistoricalMerchants: vi.fn(),
  importHistorical: vi.fn(),
}));

vi.mock('electron', () => ({
  app: { whenReady: async () => {}, isPackaged: false, quit: vi.fn(), setName: vi.fn(), setPath: vi.fn(), requestSingleInstanceLock: () => true, on: vi.fn() },
  BrowserWindow: class {
    webContents = {
      mainFrame: { url: '' },
      setWindowOpenHandler: vi.fn(),
      on: (name: string, handler: unknown) => { if (name === 'will-navigate') state.navigate = handler; },
      session: { setPermissionRequestHandler: vi.fn() },
    };
    constructor() { state.window = this; }
    once() {}
    on() {}
    async loadFile(file: string) { state.file = file; this.webContents.mainFrame.url = pathToFileURL(file).href; }
  },
  clipboard: { writeText: vi.fn() },
  dialog: { showMessageBox: vi.fn(), showOpenDialog: state.showOpenDialog },
  ipcMain: { handle: (name: string, handler: (...args: any[]) => any) => state.handlers.set(name, handler) },
  Menu: { setApplicationMenu: vi.fn() },
}));
vi.mock('../src/main/model-check-host', () => ({ ModelCheckHost: class {} }));
vi.mock('../src/main/launcher-registry', () => ({
  createLauncherRegistry: vi.fn(async () => ({ importHistorical: state.importHistorical })),
  readHistoricalMerchants: state.readHistoricalMerchants,
}));
vi.mock('../src/main/launcher-activation', () => ({ createWindowsLauncherActivation: vi.fn() }));
vi.mock('../src/main/model-settings-store', () => ({ createModelSettingsStore: vi.fn(), resolveModelSettingsDirectory: async () => path.join(state.directory, 'models') }));
vi.mock('../src/main/launch-command', () => ({ buildAccioLaunchInfo: () => ({ instructions: 'authorized' }) }));

import { runLauncher } from '../src/main/launcher-host';

describe('launcher IPC origin', () => {
  beforeAll(async () => { state.directory = await mkdtemp(path.join(os.tmpdir(), 'launcher-trust-041-')); await runLauncher(); });
  afterAll(async () => { await rm(state.directory, { recursive: true, force: true }); });

  it('accepts the launcher main frame with Windows drive-letter normalization', () => {
    const contents = state.window.webContents;
    const url = pathToFileURL(state.file).href;
    contents.mainFrame.url = process.platform === 'win32'
      ? url.replace(/^file:\/\/\/([a-z]):/i, (_, drive: string) => `file:///${drive === drive.toUpperCase() ? drive.toLowerCase() : drive.toUpperCase()}:`)
      : url;
    expect(state.handlers.get('launcher:authorization')!({ sender: contents, senderFrame: contents.mainFrame })).toBe('authorized');
    const preventDefault = vi.fn();
    state.navigate({ preventDefault }, contents.mainFrame.url);
    expect(preventDefault).not.toHaveBeenCalled();
  });

  it('rejects another page, iframe, and foreign webContents', () => {
    const contents = state.window.webContents;
    const invoke = state.handlers.get('launcher:authorization')!;
    const url = pathToFileURL(state.file).href;
    contents.mainFrame.url = url.replace('launcher.html', 'index.html');
    expect(() => invoke({ sender: contents, senderFrame: contents.mainFrame })).toThrow('无效的操作来源');
    const preventDefault = vi.fn();
    state.navigate({ preventDefault }, contents.mainFrame.url);
    expect(preventDefault).toHaveBeenCalledOnce();
    contents.mainFrame.url = url;
    expect(() => invoke({ sender: contents, senderFrame: { url } })).toThrow('无效的操作来源');
    expect(() => invoke({ sender: { mainFrame: contents.mainFrame }, senderFrame: contents.mainFrame })).toThrow('无效的操作来源');
    contents.mainFrame.url = 'https://example.com/launcher.html';
    expect(() => invoke({ sender: contents, senderFrame: contents.mainFrame })).toThrow('无效的操作来源');
  });

  it('imports only the native-selected directory and ignores renderer path arguments', async () => {
    const contents = state.window.webContents;
    contents.mainFrame.url = pathToFileURL(state.file).href;
    const rows = [{ record_id: 'first', verified_at: 0 }, { record_id: 'second', verified_at: 0 }];
    state.showOpenDialog.mockResolvedValue({ canceled: false, filePaths: ['native-selected-registry'] });
    state.readHistoricalMerchants.mockResolvedValue(rows);
    state.importHistorical.mockReset().mockResolvedValue(undefined);
    await expect(state.handlers.get('launcher:import-merchants')!({ sender: contents, senderFrame: contents.mainFrame }, 'renderer-supplied-path')).resolves.toBe(2);
    expect(state.readHistoricalMerchants).toHaveBeenLastCalledWith('native-selected-registry');
    expect(state.importHistorical.mock.calls.map(args => args[0])).toEqual(rows);
  });

  it('does not read or import a directory after native selection is cancelled', async () => {
    const contents = state.window.webContents;
    contents.mainFrame.url = pathToFileURL(state.file).href;
    state.showOpenDialog.mockResolvedValue({ canceled: true, filePaths: [] });
    state.readHistoricalMerchants.mockClear(); state.importHistorical.mockClear();
    await expect(state.handlers.get('launcher:import-merchants')!({ sender: contents, senderFrame: contents.mainFrame })).resolves.toBe(0);
    expect(state.readHistoricalMerchants).not.toHaveBeenCalled();
    expect(state.importHistorical).not.toHaveBeenCalled();
  });

  it('does not expose storage paths when importing a registration fails', async () => {
    const contents = state.window.webContents;
    contents.mainFrame.url = pathToFileURL(state.file).href;
    state.showOpenDialog.mockResolvedValue({ canceled: false, filePaths: ['native-selected-registry'] });
    state.readHistoricalMerchants.mockResolvedValue([{ record_id: 'first', verified_at: 0 }]);
    state.importHistorical.mockRejectedValueOnce(new Error('EACCES: private-storage-path/merchants.json'));
    await expect(state.handlers.get('launcher:import-merchants')!({ sender: contents, senderFrame: contents.mainFrame })).rejects.toThrow('商家登记导入未完成，请检查所选目录和本机写入权限。原业务数据未移动。');
  });

  it('rejects an import from a foreign webContents before opening the native picker', async () => {
    state.showOpenDialog.mockClear();
    await expect(state.handlers.get('launcher:import-merchants')!({ sender: {}, senderFrame: {} })).rejects.toThrow('无效的操作来源');
    expect(state.showOpenDialog).not.toHaveBeenCalled();
  });
});
