// @vitest-environment node
import { EventEmitter } from 'node:events';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { WhatsAppHost } from '../src/main/whatsapp/host';
import { connectionFailure, whatsappUserAgent, WhatsAppConnectionError } from '../src/main/whatsapp/connection';

const runtime = vi.hoisted(() => ({ contents: null as any, connect: vi.fn() }));
vi.mock('electron', () => ({ app: { getName: () => '询盘接待助手', getVersion: () => '0.3.18' }, WebContentsView: class { webContents = runtime.contents; setBounds = vi.fn(); } }));
vi.mock('puppeteer', () => ({ default: { connect: runtime.connect } }));
const directories: string[] = [];
const hosts: any[] = [];
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function setup(status: number | null = 200) {
  const directory = mkdtempSync(join(tmpdir(), 'arw-wa-0318-')); directories.push(directory);
  const debuggerApi = Object.assign(new EventEmitter(), { attach: vi.fn(), detach: vi.fn(), isAttached: vi.fn(() => false) });
  const contents = runtime.contents = Object.assign(new EventEmitter(), {
    debugger: debuggerApi, getUserAgent: () => 'Mozilla/5.0 询盘接待助手/0.3.17 Chrome/156.0.8078.80 Electron/45.2.3 Safari/537.36',
    setUserAgent: vi.fn(), setWindowOpenHandler: vi.fn(), stop: vi.fn(), isDestroyed: () => false, close: vi.fn(),
    loadURL: vi.fn(async () => {}), getURL: () => 'https://web.whatsapp.com/',
    session: { setUserAgent: vi.fn(), setPermissionRequestHandler: vi.fn(), setPermissionCheckHandler: vi.fn() }
  });
  const page = Object.assign(new EventEmitter(), { goto: vi.fn(async (): Promise<any> => status === null ? null : { status: () => status }), url: () => 'https://web.whatsapp.com/', evaluate: vi.fn(async () => false), waitForFunction: vi.fn(async () => ({})), mainFrame: vi.fn() });
  const browser = Object.assign(new EventEmitter(), { newPage: vi.fn(async () => page), disconnect: vi.fn() });
  runtime.connect.mockResolvedValue(browser);
  const events: any[] = [];
  const host: any = new WhatsAppHost({ partition: 'persist:synthetic-0318', directory, window: { contentView: { addChildView: vi.fn(), removeChildView: vi.fn() } } as never, onEvent: event => events.push(event) }); hosts.push(host);
  const clients: any[] = [];
  const initialize = host.initialize.bind(host);
  vi.spyOn(host, 'initialize').mockImplementation(async (client: any, generation: number) => {
    client.inject = vi.fn(async () => {});
    client.initWebVersionCache = vi.fn(async () => {});
    client.getChats = vi.fn(async () => []);
    clients.push(client);
    await initialize(client, generation);
  });
  return { host, events, clients, contents, page, browser, debuggerApi };
}
afterEach(async () => {
  for (const host of hosts.splice(0)) await host.close();
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true });
  vi.useRealTimers(); vi.restoreAllMocks(); runtime.connect.mockReset();
});

describe('WhatsApp connection compatibility', () => {
  it('keeps actual browser versions while converting only the application product to ASCII', () => {
    expect(whatsappUserAgent('Mozilla/5.0 询盘接待助手/0.3.17 Chrome/156.0.8078.80 Electron/45.2.3 Safari/537.36', '询盘接待助手', '0.3.18'))
      .toBe('Mozilla/5.0 ReceptionWorkbench/0.3.18 Chrome/156.0.8078.80 Electron/45.2.3 Safari/537.36');
    expect(whatsappUserAgent('a App.(Name)/1 Chrome/156', 'App.(Name)', '0.3.18')).toBe('a ReceptionWorkbench/0.3.18 Chrome/156');
  });
  it.each([400, 401, 403, 429, 500, 503])('does not inject the SDK into an HTTP %i document', async status => {
    const { host, clients, contents, browser } = setup(status);
    await host.connect();
    await vi.waitFor(() => expect(host.getStatus()).toMatchObject({ status: 'error', error_code: 'http_error', http_status: status }));
    expect(clients[0].inject).not.toHaveBeenCalled();
    expect(contents.setUserAgent).toHaveBeenCalledWith(expect.stringContaining('ReceptionWorkbench/0.3.18'));
    expect(contents.session.setUserAgent).toHaveBeenCalledExactlyOnceWith(contents.setUserAgent.mock.calls[0][0]);
    expect(contents.stop).toHaveBeenCalled(); expect(browser.disconnect).toHaveBeenCalled();
  });
  it('preserves HTTP evidence when Electron also reports a failed main-document load', async () => {
    const { host, clients, page, contents } = setup();
    const navigation = deferred<any>(); page.goto.mockImplementationOnce(() => navigation.promise);
    await host.connect(); await vi.waitFor(() => expect(page.goto).toHaveBeenCalled());
    contents.emit('did-fail-load', {}, -379, 'net::ERR_HTTP_RESPONSE_CODE_FAILURE', 'https://web.whatsapp.com/', true);
    page.emit('response', { request: () => ({ isNavigationRequest: () => true }), frame: () => page.mainFrame(), status: () => 403 });
    navigation.resolve({ status: () => 403 }); await Promise.resolve();
    expect(host.getStatus()).toMatchObject({ status: 'error', error_code: 'http_error', http_status: 403 });
    expect(clients[0].inject).not.toHaveBeenCalled();
  });
  it('rejects a navigation without an effective response', async () => {
    const { host, clients } = setup(null); await host.connect();
    await vi.waitFor(() => expect(host.getStatus()).toMatchObject({ error_code: 'invalid_response' }));
    expect(clients[0].inject).not.toHaveBeenCalled();
  });
  it('distinguishes navigation timeouts, TLS and network failures without returning source messages', async () => {
    const { host, page } = setup();
    page.goto.mockRejectedValue(Object.assign(new Error('private URL timeout'), { name: 'TimeoutError' }));
    await host.connect();
    await vi.waitFor(() => expect(host.getStatus()).toMatchObject({ error_code: 'navigation_timeout' }));
    expect(connectionFailure(new Error('ERR_CERT_AUTHORITY_INVALID private-token')).error_code).toBe('tls_error');
    expect(connectionFailure(new Error('ERR_NAME_NOT_RESOLVED private-token')).error_code).toBe('network_error');
    expect(JSON.stringify(host.getStatus())).not.toContain('private');
    expect(connectionFailure(new Error('customer-body private-token')).message).toBe('sdk_error');
  });
  it('leaves a displayed QR waiting for the user, then bounds the authenticated-to-ready phase', async () => {
    const { host, clients } = setup(); await host.connect();
    await vi.waitFor(() => expect(clients[0]?.inject).toHaveBeenCalled());
    host.clearPhase(); vi.useFakeTimers();
    host.phase(host.generation, 'sdk_timeout'); clients[0].emit('qr');
    await vi.advanceTimersByTimeAsync(180_000);
    expect(host.getStatus().status).toBe('qr');
    clients[0].emit('authenticated'); await vi.advanceTimersByTimeAsync(60_001);
    expect(host.getStatus()).toMatchObject({ status: 'error', error_code: 'ready_timeout' });
  });
  it('retires a hung SDK bootstrap and removes page listeners before a retry', async () => {
    const { host, clients, page, browser } = setup(); await host.connect();
    await vi.waitFor(() => expect(clients[0]?.inject).toHaveBeenCalled());
    host.clearPhase(); vi.useFakeTimers(); host.phase(host.generation, 'sdk_timeout');
    expect(page.listenerCount('framenavigated')).toBe(1);
    await vi.advanceTimersByTimeAsync(60_001);
    expect(host.getStatus()).toMatchObject({ status: 'error', error_code: 'sdk_timeout' });
    expect(page.listenerCount('framenavigated')).toBe(0);
    expect(page.listenerCount('response')).toBe(0);
    expect(browser.disconnect).toHaveBeenCalled();
  });
  it('gives SDK authentication a fresh budget after the application modules load', async () => {
    const { host, clients, page } = setup();
    const bootstrap = deferred<any>(); page.waitForFunction.mockImplementationOnce(() => bootstrap.promise);
    await host.connect(); await vi.waitFor(() => expect(page.waitForFunction).toHaveBeenCalled());
    host.clearPhase(); vi.useFakeTimers(); host.phase(host.generation, 'sdk_timeout');
    await vi.advanceTimersByTimeAsync(59_000);
    bootstrap.resolve({}); await vi.advanceTimersByTimeAsync(0);
    expect(clients[0].inject).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(host.getStatus().status).toBe('connecting');
    clients[0].emit('qr'); await vi.advanceTimersByTimeAsync(180_000);
    expect(host.getStatus().status).toBe('qr');
  });
  it('discards an old navigation after retirement rather than injecting into the next connection', async () => {
    const { host, page, clients, contents } = setup();
    const navigation = deferred<any>(); page.goto.mockImplementationOnce(() => navigation.promise);
    await host.connect(); await vi.waitFor(() => expect(page.goto).toHaveBeenCalled());
    const oldGeneration = host.generation;
    host.retire(oldGeneration, 'error', new WhatsAppConnectionError('navigation_timeout'));
    await host.connect();
    await vi.waitFor(() => expect(clients[1]?.inject).toHaveBeenCalled());
    navigation.resolve({ status: () => 200 }); await Promise.resolve(); await Promise.resolve();
    expect(clients[0].inject).not.toHaveBeenCalled();
    expect(clients[1].inject).toHaveBeenCalledOnce();
    expect(contents.loadURL).toHaveBeenCalledTimes(2);
    expect(host.getStatus().error_code).toBeUndefined();
  });
  it('does not duplicate connection setup and never deletes a profile on ordinary close', async () => {
    const { host, clients, contents } = setup(); await Promise.all([host.connect(), host.connect()]);
    await vi.waitFor(() => expect(clients[0]?.inject).toHaveBeenCalled());
    expect(runtime.connect).toHaveBeenCalledTimes(1);
    clients[0].authStrategy.logout = vi.fn(async () => {});
    await host.close();
    expect(clients[0].authStrategy.logout).not.toHaveBeenCalled();
    expect(contents.close).toHaveBeenCalledOnce();
  });
  it('rejects a failed main-document reload without SDK reinjection', async () => {
    const { host, clients, page } = setup(); await host.connect();
    await vi.waitFor(() => expect(clients[0]?.inject).toHaveBeenCalled());
    const frame = {} as any; page.mainFrame.mockReturnValue(frame);
    page.emit('response', { request: () => ({ isNavigationRequest: () => true }), frame: () => frame, status: () => 403 });
    expect(host.getStatus()).toMatchObject({ status: 'error', error_code: 'http_error', http_status: 403 });
    expect(clients[0].inject).toHaveBeenCalledOnce();
  });
});
