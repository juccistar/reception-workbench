import { app, BrowserWindow, ipcMain, session } from 'electron';
import path from 'node:path';
import { mkdir, writeFile } from 'node:fs/promises';
import { isWorkbenchPage } from '../../apps/desktop/src/main/trusted-page';
import { ServiceClient } from '../../apps/desktop/src/main/service-client';
import { API_METHODS } from '../../apps/desktop/src/shared/api';
import { validateChatParameters } from '../../apps/desktop/src/shared/chat-protocol';
import type { AccioLaunchInfo } from '../../apps/desktop/src/shared/launch';
import type { UpdateInfo } from '../../apps/desktop/src/shared/updates';
import { version } from '../../package.json';

const root = process.env.HARNESS_ROOT!;
const temp = process.env.HARNESS_TEMP!;
const assets = process.env.HARNESS_ASSETS!;
const python = process.env.HARNESS_PYTHON!;
const serviceEntry = path.join(root, 'scripts/fixtures/workbench-service-042.py');
const api = new Set<string>(API_METHODS);
const pythonGuard = "import runpy,socket,sys\ndef deny(*a,**k): raise OSError('network disabled in workbench fixture')\nsocket.create_connection=deny\nsocket.socket.connect=deny\nsocket.socket.connect_ex=deny\np=sys.argv[1]; sys.argv=sys.argv[1:]; runpy.run_path(p,run_name='__main__')";
let client: ServiceClient | undefined;
let closing = false;

async function start() {
  await mkdir(path.join(temp, 'electron'), { recursive: true });
  app.setPath('userData', path.join(temp, 'electron'));
  await app.whenReady();
  session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
  session.defaultSession.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !details.url.startsWith('file:') }));
  const file = path.join(assets, 'renderer/index.html');
  const win = new BrowserWindow({ width: 1440, height: 960, show: true, webPreferences: { preload: path.join(assets, 'preload/index.js'), contextIsolation: true, sandbox: true, nodeIntegration: false } });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (event, target) => { if (!isWorkbenchPage(target, file)) event.preventDefault(); });
  const bind = (channel: string, handler: (...args: any[]) => unknown) => ipcMain.handle(channel, (event, ...args) => {
    if (event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame || !isWorkbenchPage(event.senderFrame.url, file)) throw new Error('invalid IPC sender');
    return handler(...args);
  });
  const env = { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' };
  client = new ServiceClient(python, ['-I', '-c', pythonGuard, serviceEntry, '--data-dir', path.join(temp, 'data')], root, env);
  (globalThis as any).__injectWindowFixture = () => client!.invoke('_fixture_inject', { conversation_id: 'demo-emma', text: 'I need 100 pieces' });
  (globalThis as any).__blockWindowHistory = (enabled: boolean) => client!.invoke('_fixture_history_block', { enabled });
  (globalThis as any).__windowHistoryState = () => client!.invoke('_fixture_history_state');
  (globalThis as any).__setWindowSendMode = (mode: 'accepted' | 'unknown') => client!.invoke('_fixture_send_mode', { mode });
  (globalThis as any).__releaseWindowSend = () => client!.invoke('_fixture_send_release');
  (globalThis as any).__windowSendStats = () => client!.invoke('_fixture_send_stats');
  (globalThis as any).__seedWindowQuotes = () => client!.invoke('_fixture_seed_quotes');
  if (process.env.HARNESS_MANUAL_AI_DIRECT === '1') {
    (globalThis as any).__setWindowAiFailure = (enabled: boolean) => client!.invoke('_fixture_ai_failure', { enabled });
    (globalThis as any).__getWindowAiStats = () => client!.invoke('_fixture_ai_stats');
    (globalThis as any).__repeatWindowAiFailure = () => client!.invoke('_fixture_repeat_ai_failure');
    (globalThis as any).__blockWindowAi = (enabled: boolean) => client!.invoke('_fixture_ai_block', { enabled });
    (globalThis as any).__windowAiBlockState = () => client!.invoke('_fixture_ai_block_state');
  }
  client.on('event', event => { if (!win.isDestroyed()) win.webContents.send('reception:event', event); });
  bind('reception:invoke', (method: unknown, params: unknown) => {
    if (closing || typeof method !== 'string' || !api.has(method) || !params || typeof params !== 'object' || Array.isArray(params)) throw new Error('fixture RPC rejected');
    validateChatParameters(method as any, params as Record<string, unknown>);
    return client!.invoke(method, params as Record<string, unknown>);
  });
  let preferences = {};
  bind('reception:preferences:get', () => preferences);
  bind('reception:preferences:save', value => { preferences = { ...preferences, ...value }; return preferences; });
  bind('reception:models:summary', () => ({ format: 'model-settings-v1', directory_revision: 0, profiles: [], routes: { reception: { profile_id: null, route_revision: 0 }, translation: { profile_id: null, route_revision: 0 } } }));
  bind('reception:translation:get', () => ({ enabled: false, has_key: false }));
  bind('reception:storage:get', () => ({ current_root: path.join(temp, 'data'), default_root: path.join(temp, 'data'),
    configured_root: null, source: 'argument', config_file: path.join(temp, 'storage.json'), last_backup: null, busy: false }));
  // These read-only host projections are synthetic; no update or launch action is exposed.
  bind('reception:launch:get', (): AccioLaunchInfo => ({ version, distribution: 'development', executable_path: 'synthetic-window-host', command: '', instructions: '隔离窗口检查，不提供授权启动操作。' }));
  bind('reception:update:get', (): UpdateInfo => ({ current_version: version, available_version: null, status: 'unavailable', installed: false, feed_url: '', checked_at: null, progress: null, release_notes: '', message: '隔离窗口不检查更新。' }));
  win.on('close', event => {
    if (closing) return;
    event.preventDefault(); closing = true;
    win.webContents.send('reception:event', { event: 'backend.closing', data: {} });
    void client!.shutdown().then(async () => {
      if (!client!.hasExited()) throw new Error('service did not exit');
      await writeFile(path.join(temp, 'normal-shutdown.json'), JSON.stringify({ service_exited: true }));
      win.destroy(); app.quit();
    }).catch(() => app.exit(1));
  });
  await win.loadFile(file);
}
void start().catch(error => { console.error(error); client?.forceExit(); app.exit(1); });
