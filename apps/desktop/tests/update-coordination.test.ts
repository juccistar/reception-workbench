vi.mock('../src/main/installation-guard', () => ({ acquireInstallationStartGuard: vi.fn(async () => async () => {}) }));
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/react';
import { pathToFileURL } from 'node:url';
import { version as currentVersion } from '../../../package.json';
const nextVersion = currentVersion.split('.').map((part, index) => index === 2 ? String(Number(part) + 1) : part).join('.');

const host = vi.hoisted(() => ({
  handlers: new Map<string, (...args: any[]) => any>(), window: null as any,
  options: null as any, order: [] as string[], dialogs: [1] as number[], dirty: false, frozenAtSnapshot: false,
  shutdownWait: null as Promise<void> | null, cancelWait: null as Promise<void> | null, failBackup: false,
  relaunch: vi.fn(), quit: vi.fn(), restart: vi.fn(async () => {
    let cancelled = false;
    return { commit: async () => { host.order.push('commit'); }, cancel: async () => { if (cancelled) return; cancelled = true; host.order.push('cancel'); await host.cancelWait; host.order.push('helper-lock-released'); } };
  }),
  bootstrapWrite: vi.fn(),
}));
const snapshot = { app: { account_verified: false, seller_id: 'demo-seller' }, settings: { workctl_path: '' }, outbox: [{ status: 'unknown', operation_id: 'original-operation' }] };
vi.mock('node:fs', () => ({ existsSync: () => true, mkdirSync: vi.fn() }));
vi.mock('node:fs/promises', () => ({ mkdir: vi.fn(), readFile: vi.fn(async () => { throw new Error('empty'); }), writeFile: vi.fn(), rename: vi.fn(), lstat: vi.fn() }));
vi.mock('../../../scripts/data-location.mjs', () => ({
  getBootstrapPaths: () => ({ configFile: 'C:/synthetic-host/storage.json', stateDir: 'C:/synthetic-host', defaultRoot: 'C:/synthetic-business' }),
  resolveDataLocation: () => ({ currentRoot: 'C:/synthetic-business', defaultRoot: 'C:/synthetic-business', configuredRoot: 'C:/synthetic-business', configFile: 'C:/synthetic-host/storage.json', source: 'configured' }),
  readBootstrap: () => ({ version: 1, dataRoot: 'C:/synthetic-business' }),
  writeBootstrap: (...args: any[]) => host.bootstrapWrite(...args),
  restartArguments: (_args: string[], root: string) => ['--data-dir', root],
}));
vi.mock('../src/main/storage-host', async original => ({
  ...await original<typeof import('../src/main/storage-host')>(),
  createArchiveParent: vi.fn(async () => {}),
  acquireStorageOperation: vi.fn(async () => async () => { host.order.push('release-lock'); }),
  runMaintenance: vi.fn(async (_runner, args: string[]) => {
    const action = args[1];
    if (action === 'backup') { host.order.push('backup'); if (host.failBackup) throw new Error('synthetic failure'); }
    return { status: 'completed', action, data_root: 'C:/synthetic-business', backup: action === 'backup' ? 'C:/synthetic-host/backups/complete' : null };
  }),
}));
vi.mock('../src/main/update-restart', () => ({
  installedDistribution: async () => true,
  startUpdateRestart: async (plan: any) => { host.order.push('restart-helper'); return host.restart(plan); },
}));
vi.mock('../src/main/update-host', () => ({
  UpdateHost: class {
    constructor(options: any) { host.options = options; }
    getInfo() { return { status: 'downloaded' }; }
    async install() {
      try { return { status: await host.options.beforeInstall(nextVersion, 'C:/synthetic-installer.exe', 'synthetic-signed-hash') ? 'installing' : 'downloaded' }; }
      catch { await host.options.onInstallError(); return { status: 'error' }; }
    }
  },
}));
vi.mock('../src/main/service-client', () => ({
  BackendError: class extends Error {},
  ServiceClient: class {
    async invoke(method: string) { if (method === 'get_snapshot') { if (host.options) host.frozenAtSnapshot = host.window.setEnabled.mock.calls.at(-1)?.[0] === false; return snapshot; } return {}; }
    on = vi.fn();
    async shutdown() { host.order.push('shutdown'); await host.shutdownWait; host.order.push('shutdown-complete'); }
  },
}));
vi.mock('electron', () => ({
  app: { isPackaged: false, setName: vi.fn(), setPath: vi.fn(), requestSingleInstanceLock: () => true, whenReady: async () => {}, on: vi.fn(), quit: (...args: any[]) => host.quit(...args), relaunch: (...args: any[]) => host.relaunch(...args), exit: vi.fn() },
  BrowserWindow: class {
    webContents = { mainFrame: { url: '' }, setWindowOpenHandler: vi.fn(), on: vi.fn(), send: vi.fn(), session: { setPermissionRequestHandler: vi.fn() }, executeJavaScript: vi.fn(async () => !host.dirty) };
    constructor() { host.window = this; }
    isDestroyed = () => false; setEnabled = vi.fn(); setTitle = vi.fn(); destroy = vi.fn(); on = vi.fn(); once = vi.fn();
    async loadFile(file: string) { this.webContents.mainFrame.url = pathToFileURL(file).href; }
  },
  clipboard: { writeText: vi.fn() }, shell: { openPath: vi.fn(async () => '') },
  dialog: { showMessageBox: vi.fn(async () => ({ response: host.dialogs.shift() ?? 1 })), showErrorBox: vi.fn() },
  ipcMain: { handle: (name: string, handler: (...args: any[]) => any) => host.handlers.set(name, handler) },
  Menu: { buildFromTemplate: vi.fn(), setApplicationMenu: vi.fn() },
  screen: { getPrimaryDisplay: () => ({ workArea: { width: 1500, height: 1000 } }) },
}));
const event = () => ({ sender: host.window.webContents, senderFrame: host.window.webContents.mainFrame });

beforeEach(async () => {
  vi.resetModules(); host.handlers.clear(); host.options = null; host.order = []; host.dialogs = [1]; host.dirty = false;
  host.shutdownWait = null; host.cancelWait = null; host.failBackup = false; host.relaunch.mockClear(); host.quit.mockClear(); host.restart.mockClear(); host.bootstrapWrite.mockClear();
  await import('../src/main/index');
  await waitFor(() => expect(host.handlers.has('reception:update:install')).toBe(true));
  await new Promise(resolve => setTimeout(resolve, 0));
  await host.handlers.get('reception:update:get')!(event());
  host.order = [];
});

describe('update installation coordination', () => {
  it('leaves the backend and original operations alone when native consent is cancelled', async () => {
    host.dialogs = [0];
    await expect(host.handlers.get('reception:update:install')!(event())).resolves.toEqual({ status: 'downloaded' });
    expect(host.order).toEqual([]); expect(host.restart).not.toHaveBeenCalled(); expect(host.bootstrapWrite).not.toHaveBeenCalled();
    expect(snapshot.outbox[0]).toEqual({ status: 'unknown', operation_id: 'original-operation' });
  });
  it('does not stop or install when the user keeps editing unsaved text', async () => {
    host.dirty = true; host.dialogs = [1, 0];
    await expect(host.handlers.get('reception:update:install')!(event())).resolves.toEqual({ status: 'downloaded' });
    expect(host.order).toEqual([]); expect(host.restart).not.toHaveBeenCalled();
  });
  it('waits for the actual shutdown before backup and prepares restart only after backup', async () => {
    let finish!: () => void; host.shutdownWait = new Promise(resolve => { finish = resolve; });
    const installation = host.handlers.get('reception:update:install')!(event());
    await waitFor(() => expect(host.order).toContain('shutdown'));
    expect(host.order).not.toContain('backup'); expect(host.order).not.toContain('commit');
    finish();
    await expect(installation).resolves.toEqual({ status: 'installing' });
    expect(host.order).toEqual(['restart-helper', 'shutdown', 'shutdown-complete', 'backup', 'commit']);
    expect(host.frozenAtSnapshot).toBe(true);
    expect(host.restart).toHaveBeenCalledWith(expect.objectContaining({ version: nextVersion, previousVersion: currentVersion, installerPath: 'C:/synthetic-installer.exe', stateDirectory: 'C:/synthetic-host', parentPid: process.pid, args: ['--data-dir', 'C:/synthetic-business'] }));
    expect(host.bootstrapWrite).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ dataRoot: 'C:/synthetic-business', lastBackup: expect.objectContaining({ path: 'C:/synthetic-host/backups/complete' }) }), expect.anything());
  });
  it('does not start the installer after a failed backup and reopens the original workbench once', async () => {
    host.failBackup = true;
    let release!: () => void; host.cancelWait = new Promise(resolve => { release = resolve; });
    const installation = host.handlers.get('reception:update:install')!(event());
    await waitFor(() => expect(host.order).toContain('cancel'));
    expect(host.relaunch).not.toHaveBeenCalled();
    release();
    await expect(installation).resolves.toEqual({ status: 'error' });
    expect(host.order).toEqual(['restart-helper', 'shutdown', 'shutdown-complete', 'backup', 'cancel', 'helper-lock-released']);
    expect(host.order).not.toContain('commit'); expect(host.bootstrapWrite).not.toHaveBeenCalled();
    expect(host.relaunch).toHaveBeenCalledTimes(1); expect(host.quit).toHaveBeenCalledTimes(1);
    expect(host.relaunch).toHaveBeenCalledWith({ args: ['--data-dir', 'C:/synthetic-business'] });
  });
});
