import { app, BrowserWindow, ipcMain, session } from 'electron';
import path from 'node:path';
import { mkdir, writeFile } from 'node:fs/promises';
import { ServiceClient } from '../../apps/desktop/src/main/service-client';
import { isWorkbenchPage } from '../../apps/desktop/src/main/trusted-page';
import { readOriginal } from '../../apps/desktop/src/main/media-host';

const root = process.env.HARNESS_ROOT!, temp = process.env.HARNESS_TEMP!, assets = process.env.HARNESS_ASSETS!;
const guard = "import runpy,socket,sys\ndef deny(*a,**k): raise OSError('network disabled in image fixture')\nsocket.create_connection=deny\nsocket.socket.connect=deny\nsocket.socket.connect_ex=deny\np=sys.argv[1]; sys.argv=sys.argv[1:]; runpy.run_path(p,run_name='__main__')";
let client: ServiceClient | undefined;
async function start() {
  await mkdir(path.join(temp, 'electron'), { recursive: true });
  app.setPath('userData', path.join(temp, 'electron'));
  await app.whenReady();
  session.defaultSession.setPermissionRequestHandler((_w, _p, done) => done(false));
  session.defaultSession.webRequest.onBeforeRequest((details, done) => done({ cancel: !/^(file|blob|data):/.test(details.url) }));
  const file = path.join(assets, 'renderer/index.html');
  const win = new BrowserWindow({ width: 1440, height: 960, show: true, webPreferences: { preload: path.join(assets, 'preload/index.js'), contextIsolation: true, sandbox: true, nodeIntegration: false } });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (event, url) => { if (!isWorkbenchPage(url, file)) event.preventDefault(); });
  const bind = (channel: string, action: (...args: any[]) => unknown) => ipcMain.handle(channel, (event, ...args) => {
    if (event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame || !isWorkbenchPage(event.senderFrame.url, file)) throw new Error('invalid image fixture sender');
    return action(...args);
  });
  client = new ServiceClient(process.env.HARNESS_PYTHON!, ['-I', '-c', guard, path.join(root, 'scripts/fixtures/transparent-image-service-041.py'), '--data-dir', path.join(temp, 'data')], root, { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' });
  client.on('event', value => { if (!win.isDestroyed()) win.webContents.send('reception:event', value); });
  const allowed = new Set(['get_snapshot', 'get_executor_health', 'get_conversation', 'list_jobs', 'mark_messages_read', 'get_translations', 'get_message_image', 'load_message_image']);
  bind('reception:invoke', (method: string, params: Record<string, unknown>) => { if (!allowed.has(method)) throw new Error('fixture action rejected'); return client!.invoke(method, params); });
  let preferences = {};
  bind('reception:preferences:get', () => preferences);
  bind('reception:preferences:save', value => { preferences = { ...preferences, ...value }; return preferences; });
  const controllers = new Map<string, AbortController>();
  bind('reception:image:original', async (reference, id: string) => {
    const controller = new AbortController(); controllers.set(id, controller);
    try { return await readOriginal((method, params) => client!.invoke(method, params), reference, () => {}, false, controller.signal); }
    finally { controllers.delete(id); }
  });
  bind('reception:image:cancel', (id: string) => controllers.get(id)?.abort());
  bind('reception:image:menu', () => 'cancelled');
  (globalThis as any).__imageIntegrity = () => client!.invoke('_fixture_image_integrity');
  let closing = false;
  win.on('close', event => {
    if (closing) return;
    event.preventDefault(); closing = true;
    void client!.shutdown().then(async () => { await writeFile(path.join(temp, 'normal-shutdown.json'), JSON.stringify({ service_exited: client!.hasExited() })); win.destroy(); app.quit(); }).catch(() => app.exit(1));
  });
  await win.loadFile(file);
}
void start().catch(error => { console.error(error); client?.forceExit(); app.exit(1); });
