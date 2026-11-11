// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/react';
import { pathToFileURL } from 'node:url';

vi.mock('../src/main/installation-guard', () => ({ acquireInstallationStartGuard: vi.fn(async () => async () => {}) }));

const host = vi.hoisted(() => ({
  handlers: new Map<string, (...args: any[]) => any>(), window: null as any,
  configured: 'C:/synthetic/formal-root', invalid: false,
  resolve: vi.fn(), clipboard: vi.fn(), autoContext: false, registeredNew: false, sameConfigured: false,
  maintenance: vi.fn(async (..._args: any[]) => ({ status: 'completed', backup: null })),
  invoke: vi.fn(async (_method: string) => ({ app: { seller_id: '2500000000123', account_verified: true }, settings: { workctl_path: 'C:/synthetic/workctl.exe' } })),
}));
vi.mock('node:fs', () => ({ existsSync: () => true, mkdirSync: vi.fn() }));
vi.mock('node:fs/promises', () => ({ mkdir: vi.fn(), readFile: vi.fn(async () => { throw new Error('empty'); }), writeFile: vi.fn(), rename: vi.fn(), lstat: vi.fn() }));
vi.mock('../../../scripts/data-location.mjs', () => ({
  getBootstrapPaths: () => ({ configFile: 'C:/synthetic-host/storage.json', stateDir: 'C:/synthetic-host', defaultRoot: 'C:/synthetic/default-root' }),
  resolveDataLocation: (args: string[]) => {
    host.resolve(args);
    if (host.invalid) throw new Error('private raw configuration error');
    const override = args.includes('--data-dir');
    return { currentRoot: override && !host.sameConfigured ? 'C:/synthetic/demo-only-root' : host.configured,
      defaultRoot: 'C:/synthetic/default-root', configuredRoot: host.configured,
      configFile: 'C:/synthetic-host/storage.json', source: override ? 'argument' : 'configured' };
  },
  readBootstrap: () => ({ version: 1, dataRoot: host.configured }), writeBootstrap: vi.fn(), restartArguments: vi.fn(),
}));
vi.mock('../src/main/storage-host', async original => ({
  ...await original<typeof import('../src/main/storage-host')>(),
  createArchiveParent: vi.fn(async () => {}), acquireStorageOperation: vi.fn(async () => async () => {}),
  runMaintenance: host.maintenance,
}));
vi.mock('../../../scripts/merchant-profiles.mjs', async original => ({
  ...await original<typeof import('../../../scripts/merchant-profiles.mjs')>(),
  consumeAutoLaunchContext: () => host.autoContext ? { sellerId: '2500000000001' } : null,
}));
vi.mock('../src/main/launch-safety', async original => ({
  ...await original<typeof import('../src/main/launch-safety')>(), registeredNewRoot: () => host.registeredNew,
}));
vi.mock('../src/main/service-client', () => ({
  BackendError: class extends Error {},
  ServiceClient: class { invoke = host.invoke; on = vi.fn(); shutdown = vi.fn(async () => {}); },
}));
vi.mock('electron', () => ({
  app: { isPackaged: false, setName: vi.fn(), setPath: vi.fn(), requestSingleInstanceLock: () => true, whenReady: async () => {}, on: vi.fn(), quit: vi.fn(), exit: vi.fn() },
  BrowserWindow: class {
    webContents = { mainFrame: { url: '' }, setWindowOpenHandler: vi.fn(), on: vi.fn(), send: vi.fn(), session: { setPermissionRequestHandler: vi.fn() } };
    constructor() { host.window = this; }
    on = vi.fn(); once = vi.fn(); isDestroyed = () => false; setTitle = vi.fn();
    async loadFile(file: string) { this.webContents.mainFrame.url = pathToFileURL(file).href; }
  },
  clipboard: { writeText: host.clipboard }, shell: { openPath: vi.fn() },
  dialog: { showMessageBox: vi.fn(), showErrorBox: vi.fn() },
  ipcMain: { handle: (name: string, handler: (...args: any[]) => any) => host.handlers.set(name, handler) },
  Menu: { buildFromTemplate: vi.fn(), setApplicationMenu: vi.fn() },
  screen: { getPrimaryDisplay: () => ({ workArea: { width: 1500, height: 1000 } }) },
}));
const event = () => ({ sender: host.window.webContents, senderFrame: host.window.webContents.mainFrame });

async function boot(mode = 'demo') {
  vi.resetModules(); host.handlers.clear(); host.invalid = false; host.configured = 'C:/synthetic/formal-root';
  host.autoContext = false; host.registeredNew = false; host.sameConfigured = false; host.maintenance.mockClear();
  host.clipboard.mockReset(); host.resolve.mockClear();
  const previous = process.argv;
  process.argv = [previous[0], 'synthetic-entry', ...(mode === 'live' ? ['--live'] : []), '--data-dir', 'C:/synthetic/demo-only-root', '--expect-seller', '2500000000001', '--workctl', 'C:/synthetic/demo-workctl.exe'];
  try { await import('../src/main/index'); } finally { process.argv = previous; }
  await waitFor(() => expect(host.handlers.has('reception:launch:copy')).toBe(true));
  await new Promise(resolve => setTimeout(resolve, 0));
  host.invoke.mockClear(); host.resolve.mockClear();
}

beforeEach(async () => { await boot(); });

describe('host-owned Accio launch instructions', () => {
  it('notifies the renderer after the backend has answered its readiness snapshot', async () => {
    expect(host.window.webContents.send).toHaveBeenCalledWith('reception:event', { event: 'state.changed', data: { reason: 'backend_ready' } });
  });
  it('passes allow-new to GUI backend preflight only for the independently registered new workspace', async () => {
    for (const registered of [true, false]) {
      vi.resetModules(); host.handlers.clear(); host.maintenance.mockClear();
      host.autoContext = true; host.sameConfigured = true; host.registeredNew = registered;
      const previous = process.argv;
      process.argv = [previous[0], 'synthetic-entry', '--connect-from-accio', '--expect-seller', '2500000000001', '--data-dir', host.configured, '--workctl', 'synthetic-tool'];
      try {
        await import('../src/main/index');
        await waitFor(() => expect(host.maintenance).toHaveBeenCalled());
        expect(host.maintenance.mock.calls[0][1].includes('--allow-new')).toBe(registered);
      } finally { process.argv = previous; }
    }
  });
  it('shows a controlled error before initializing a workspace when authorized runtime is missing', async () => {
    const { app, dialog } = await import('electron');
    vi.resetModules(); host.handlers.clear(); host.resolve.mockClear();
    const previous = process.argv; const runtime = process.env.ACCIO_NODE_BIN;
    process.argv = [previous[0], 'synthetic-entry', '--connect-from-accio'];
    delete process.env.ACCIO_NODE_BIN;
    try {
      await import('../src/main/index');
      await waitFor(() => expect(dialog.showErrorBox).toHaveBeenCalled());
      expect(app.exit).toHaveBeenCalledWith(2);
      expect(host.resolve).not.toHaveBeenCalled();
      expect(host.handlers.size).toBe(0);
    } finally { process.argv = previous; if (runtime !== undefined) process.env.ACCIO_NODE_BIN = runtime; }
  });
  it('generates account-neutral commands without reading workspace state', async () => {
    const info = await host.handlers.get('reception:launch:get')!(event());
    expect(info).toMatchObject({ distribution: 'development', executable_path: process.execPath });
    expect(info.command).toContain('--connect-from-accio');
    for (const binding of ['--data-dir', '--expect-seller', '--workctl', 'demo-only-root']) expect(info.command).not.toContain(binding);
    expect(host.resolve).not.toHaveBeenCalled(); expect(host.invoke).not.toHaveBeenCalled();
  });
  it('copies instructions and reports clipboard failures safely', async () => {
    const info = await host.handlers.get('reception:launch:copy')!(event());
    expect(host.clipboard).toHaveBeenCalledExactlyOnceWith(info.instructions);
    host.clipboard.mockImplementationOnce(() => { throw new Error('private OS error'); });
    await expect(host.handlers.get('reception:launch:copy')!(event())).rejects.toThrow('复制失败');
  });
  it('rejects untrusted senders', async () => {
    for (const request of [{ ...event(), sender: {} }, { ...event(), senderFrame: {} }]) {
      await expect(host.handlers.get('reception:launch:get')!(request)).rejects.toThrow('无效的操作来源');
      await expect(host.handlers.get('reception:launch:copy')!(request)).rejects.toThrow('无效的操作来源');
    }
    expect(host.clipboard).not.toHaveBeenCalled();
  });
  it('uses the same neutral command in live mode', async () => {
    await boot('live');
    const info = await host.handlers.get('reception:launch:get')!(event());
    expect(info.command).toContain('--connect-from-accio');
    expect(info.command).not.toContain('--data-dir'); expect(info.command).not.toContain('--expect-seller');
    expect(host.invoke).not.toHaveBeenCalled();
  });
});
