// @vitest-environment node
import { pathToFileURL } from 'node:url';
import { beforeAll, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  handlers: new Map<string, (...args: any[]) => any>(),
  window: undefined as any,
  file: '',
  navigate: undefined as any,
}));

vi.mock('electron', () => ({
  app: { whenReady: async () => {}, isPackaged: false, quit: vi.fn() },
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
  dialog: { showMessageBox: vi.fn() },
  ipcMain: { handle: (name: string, handler: (...args: any[]) => any) => state.handlers.set(name, handler) },
  Menu: { setApplicationMenu: vi.fn() },
}));
vi.mock('../src/main/model-check-host', () => ({ ModelCheckHost: class {} }));
vi.mock('../src/main/launcher-registry', () => ({ createLauncherRegistry: vi.fn() }));
vi.mock('../src/main/launcher-activation', () => ({ createWindowsLauncherActivation: vi.fn() }));
vi.mock('../src/main/model-settings-store', () => ({ createModelSettingsStore: vi.fn() }));
vi.mock('../src/main/launch-command', () => ({ buildAccioLaunchInfo: () => ({ instructions: 'authorized' }) }));

import { runLauncher } from '../src/main/launcher-host';

describe('launcher IPC origin', () => {
  beforeAll(async () => { await runLauncher(); });

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
});
