import { app, BrowserWindow, ipcMain, session, Menu } from 'electron';
import { readOriginal, validateReference } from '../../apps/desktop/src/main/media-host';
import { createExitDialogHost } from '../../apps/desktop/src/main/exit-dialog-host';
import { FeishuConfigHost } from '../../apps/desktop/src/main/feishu-config-host';
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
  const win = new BrowserWindow({ width: 1440, height: 960, useContentSize: true, show: true, autoHideMenuBar: true, webPreferences: { preload: path.join(assets, 'preload/index.js'), contextIsolation: true, sandbox: true, nodeIntegration: false } });
  win.removeMenu();
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
  (globalThis as any).__failWindowHistory = (enabled: boolean) => client!.invoke('_fixture_history_failure', { enabled });
  (globalThis as any).__setWindowSendMode = (mode: 'accepted' | 'unknown') => client!.invoke('_fixture_send_mode', { mode });
  (globalThis as any).__releaseWindowSend = () => client!.invoke('_fixture_send_release');
  (globalThis as any).__windowSendStats = () => client!.invoke('_fixture_send_stats');
  (globalThis as any).__seedWindowQuotes = () => client!.invoke('_fixture_seed_quotes');
  (globalThis as any).__seedWindowShowcase = () => client!.invoke('_fixture_seed_showcase');
  (globalThis as any).__setWindowReplyProgress = (phase: string) => client!.invoke('_fixture_progress', { phase });
  (globalThis as any).__setWindowImageProgress = (stage: string) => client!.invoke('_fixture_image_progress', { stage });
  if (process.env.HARNESS_MANUAL_AI_DIRECT === '1') {
    (globalThis as any).__setWindowAiFailure = (enabled: boolean) => client!.invoke('_fixture_ai_failure', { enabled });
    (globalThis as any).__getWindowAiStats = () => client!.invoke('_fixture_ai_stats');
    (globalThis as any).__repeatWindowAiFailure = () => client!.invoke('_fixture_repeat_ai_failure');
    (globalThis as any).__blockWindowAi = (enabled: boolean) => client!.invoke('_fixture_ai_block', { enabled });
    (globalThis as any).__windowAiBlockState = () => client!.invoke('_fixture_ai_block_state');
  }
  const rpcTrace: { method: string; started_at: number; completed_at?: number; status?: string; error?: string }[] = [];
  (globalThis as any).__windowRpcDiagnostics = () => rpcTrace.slice(-60);
  client.on('event', event => { if (!win.isDestroyed()) win.webContents.send('reception:event', event); });
  bind('reception:invoke', (method: unknown, params: unknown) => {
    if (closing || typeof method !== 'string' || !api.has(method) || !params || typeof params !== 'object' || Array.isArray(params)) throw new Error('fixture RPC rejected');
    validateChatParameters(method as any, params as Record<string, unknown>);
    const row = { method, started_at: Date.now() } as typeof rpcTrace[number];
    rpcTrace.push(row); if (rpcTrace.length > 120) rpcTrace.shift();
    return client!.invoke(method, params as Record<string, unknown>).then(value => { row.completed_at = Date.now(); row.status = 'succeeded'; return value; }, error => { row.completed_at = Date.now(); row.status = 'failed'; row.error = String(error?.message ?? 'RPC failed').slice(0, 400); throw error; });
  });
  const ready = () => { if (closing || win.isDestroyed()) throw new Error('窗口正在退出'); };
  const invokeMedia = <T>(method: string, params: Record<string, unknown>) => client!.invoke(method, params) as Promise<T>;
  const imageRequests = new Map<string, AbortController>();
  const usedImageIds = new Set<string>();
  const validImageId = (id: unknown): id is string => typeof id === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id);
  bind('reception:image:original', async (reference, id) => {
    ready();
    if (!validImageId(id) || usedImageIds.has(id) || usedImageIds.size >= 65536 || imageRequests.size >= 2) throw new Error('原图请求无效或繁忙');
    usedImageIds.add(id);
    const controller = new AbortController(); imageRequests.set(id, controller);
    try { return await readOriginal(invokeMedia, reference, ready, false, controller.signal); }
    finally { imageRequests.delete(id); }
  });
  bind('reception:image:cancel', id => { if (!validImageId(id)) throw new Error('原图请求无效'); imageRequests.get(id)?.abort(); });
  bind('reception:image:menu', async reference => {
    ready(); validateReference(reference);
    const selected = await new Promise<boolean>(resolve => {
      let chosen = false;
      Menu.buildFromTemplate([{ label: '复制图片', click: () => { chosen = true; } }]).popup({ window: win, callback: () => resolve(chosen) });
    });
    if (!selected) return 'cancelled';
    await readOriginal(invokeMedia, reference, ready, true);
    return 'copied'; // Synthetic host deliberately does not write the system clipboard.
  });
  bind('reception:image:copy', async reference => {
    ready(); validateReference(reference);
    await readOriginal(invokeMedia, reference, ready, true);
    // The isolated host exercises the full PNG preparation without changing the clipboard.
    (globalThis as any).__windowImageCopyCount = ((globalThis as any).__windowImageCopyCount ?? 0) + 1;
  });
  const exitHost = createExitDialogHost<BrowserWindow, Electron.WebFrameMain, Electron.IpcMainInvokeEvent>({
    currentFrame: target => target.isDestroyed() ? null : target.webContents.mainFrame,
    isSource: (event, target, frame) => event.sender === target.webContents && event.senderFrame === frame && isWorkbenchPage(frame.url, file),
    send: (target, event) => target.webContents.send('reception:exit-dialog', event),
  });
  ipcMain.handle('reception:exit-dialog:respond', (event, value: unknown) => exitHost.respond(event, value));
  (globalThis as any).__requestWindowExitDialog = (reason?: 'save_failed') => exitHost.request(win, reason);
  win.on('closed', () => exitHost.cancel());
  win.webContents.on('did-start-navigation', () => exitHost.cancel());
  let preferences = {};
  bind('reception:preferences:get', () => preferences);
  bind('reception:preferences:save', value => { preferences = { ...preferences, ...value }; return preferences; });
  bind('reception:models:summary', () => ({ format: 'model-settings-v1', directory_revision: 0, profiles: [], routes: { reception: { profile_id: null, route_revision: 0 }, translation: { profile_id: null, route_revision: 0 } } }));
  bind('reception:models:launcher', () => undefined);
  bind('reception:launcher:open', () => undefined);
  const syntheticSecrets = new Map<string, string>();
  const feishu = new FeishuConfigHost({
    stage: async (merchant, appId, secret) => { const ref = (await import('node:crypto')).randomBytes(32).toString('hex'); syntheticSecrets.set(`${merchant}:${appId}:${ref}`, secret); return ref; },
    read: async (merchant, appId, ref) => syntheticSecrets.get(`${merchant}:${appId}:${ref ?? ''}`) ?? null,
  }, (method, params = {}) => client!.invoke(method, params), 'live', path.join(temp, 'data'), ready);
  bind('reception:feishu:save', input => feishu.save(input));
  bind('reception:feishu:grant', input => feishu.grant(input));
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
