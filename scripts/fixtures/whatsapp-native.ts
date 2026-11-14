import { app, BrowserWindow } from 'electron';
import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import puppeteer from 'puppeteer';
import { compiledWhatsAppHost } from './compiled-whatsapp-host';
import type { WhatsAppHost as Host } from '../../apps/desktop/src/main/whatsapp/host';

app.setPath('userData', process.env.RECEPTION_WA_FIXTURE_TEMP!);
app.setName('询盘接待助手');
const reference = process.env.RECEPTION_WA_REFERENCE_NO_CDP === '1';
const controlledFailure = process.env.RECEPTION_WA_SYNTHETIC_FAILURE === '1';
const report: any = { status: 'failed', version: app.getVersion(), cold_start: true, prewarm_before_connect: false,
  compiled_host_used: false, fixture_startup_replaced: false, real_merchant_bootstrap_used: false,
  reference_no_cdp: reference, controlled_sdk_failure: controlledFailure, formal_chinese_application_name: true,
  actual_ascii_request: false, application_tokens_omitted: false, request_chrome_matches_engine: false,
  session_ascii_user_agent: false, public_http_status: 0, sdk_injected_to_qr: false, sdk_inject_completed: false,
  qr_events: 0, native_qr_visible: false, qr_refresh_observed: false, qr_refresh_requested: false,
  stable_observation_ms: 0, sdk_state_stable: false, normal_close: false, qr_recorded: false,
  logged_in: false, real_ai_calls: 0, real_customer_sends: 0, production_data_accessed: false, error_code: '' };
let fixtureWindow: BrowserWindow;
let host: Host;
let classes: ReturnType<typeof compiledWhatsAppHost>;
let deadline: ReturnType<typeof setTimeout>;
let watcher: ReturnType<typeof setInterval>;
let visibleSince = 0, missingSamples = 0, probing = false, finished = false;
let expectedFault = false;
// WhatsApp has its own web module loader named require; verify Node isolation
// through actual WebPreferences and the absence of privileged page globals.
const qrProbe = `(() => {
  const node = document.querySelector('canvas[data-ref], [data-testid=qrcode], [data-ref] canvas');
  const visible = !!node && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  const reference = node?.getAttribute('data-ref') || node?.closest('[data-ref]')?.getAttribute('data-ref');
  const previous = window.__fixtureQrReference;
  const changed = !!reference && !!previous && reference !== previous;
  if (reference) window.__fixtureQrReference = reference;
  return { visible, changed, bridge_absent: typeof window.reception === 'undefined' && typeof process === 'undefined' };
})()`;

async function protocolAfterColdRun() {
  const probe: any = new classes.WhatsAppHost({ window: fixtureWindow, partition: 'persist:protocol-' + randomUUID(), directory: join(process.env.RECEPTION_WA_FIXTURE_TEMP!, 'protocol-ledger'), onEvent: () => {} });
  probe.setVisible(true, { x: 0, y: 0, width: 980, height: 740 });
  const contents = probe.view.webContents;
  await contents.loadURL('about:blank');
  const transport = new classes.ViewTransport(contents);
  const browser = await puppeteer.connect({ transport, defaultViewport: null });
  try {
    const page = await browser.newPage();
    report.cdp_version_matches_engine = await browser.version() === `Chrome/${process.versions.chrome}`;
    await page.evaluate(() => { document.body.innerHTML = '<p id="same-view">synthetic</p>'; });
    report.same_visible_dom = await contents.executeJavaScript('document.getElementById("same-view")?.textContent === "synthetic"');
    await page.exposeFunction('fixtureAdd', (a: number, b: number) => a + b);
    await page.evaluateOnNewDocument('window.__synthetic_before_load = true');
    await page.goto('about:blank');
    report.expose_function = await page.evaluate(async () => await (window as any).fixtureAdd(2, 3) === 5);
    report.preload_script = await page.evaluate(() => (window as any).__synthetic_before_load === true);
  } finally { browser.disconnect(); transport.close(); await probe.close(); }
}

async function finish(code = '') {
  if (finished) return;
  finished = true; clearTimeout(deadline); clearInterval(watcher);
  report.error_code = code;
  try {
    const contents = (host as any)?.view.webContents;
    if (contents && !contents.isDestroyed()) {
      report.final_host_status = host.getStatus().status;
      report.final_page_state = host.getStatus().page_state;
      if (process.env.RECEPTION_WA_SCREENSHOT_OUTPUT && !report.logged_in) {
        await contents.insertCSS('canvas,img,svg,video,iframe,[data-ref],[data-testid="qrcode"] { visibility:hidden !important; }');
        await contents.executeJavaScript('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
        await writeFile(process.env.RECEPTION_WA_SCREENSHOT_OUTPUT, (await contents.capturePage()).toPNG());
        report.redacted_screenshot = true;
      }
    }
    await host?.close();
    if (!reference && !code) await protocolAfterColdRun();
    report.normal_close = true;
  } catch { report.error_code ||= 'CLOSE_OR_PROTOCOL_CHECK_FAILED'; }
  report.sdk_state_stable = reference || (controlledFailure
    ? expectedFault && report.final_host_status === 'error'
    : report.sdk_inject_completed && report.final_host_status === 'qr');
  const common = report.actual_ascii_request && report.session_ascii_user_agent && report.application_tokens_omitted
    && report.request_chrome_matches_engine && report.public_http_status === 200 && report.native_qr_visible
    && report.stable_observation_ms >= 60_000 && report.qr_refresh_observed && report.remote_business_bridge_absent && report.remote_node_integration_disabled
    && report.final_page_state === 'available'
    && report.normal_close && !report.error_code && !report.logged_in;
  report.status = reference ? common ? 'reference-ready' : 'failed'
    : common && report.sdk_injected_to_qr && report.sdk_inject_completed && report.sdk_state_stable
      && (!controlledFailure || expectedFault) && report.cdp_version_matches_engine && report.same_visible_dom
      && report.expose_function && report.preload_script ? 'passed' : 'failed';
  await new Promise<void>(resolve => process.stdout.write('WA_FIXTURE_REPORT ' + JSON.stringify(report) + '\n', () => resolve()));
  fixtureWindow?.destroy(); app.quit();
}

app.whenReady().then(async () => {
  classes = compiledWhatsAppHost(process.env.RECEPTION_WA_HOST_ENTRY!);
  report.compiled_host_used = true;
  report.fixture_startup_replaced = true;
  report.host_sha256 = classes.host_sha256;
  fixtureWindow = new BrowserWindow({ width: 980, height: 740, show: true, webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } });
  host = new classes.WhatsAppHost({ window: fixtureWindow, partition: 'persist:wa-fixture-' + randomUUID(),
    directory: join(process.env.RECEPTION_WA_FIXTURE_TEMP!, 'ledger'), diagnosticsDirectory: join(process.env.RECEPTION_WA_FIXTURE_TEMP!, 'diagnostics'),
    onEvent: event => {
      if (event.type !== 'account' || finished) return;
      if (event.status === 'qr') { report.sdk_injected_to_qr = true; ++report.qr_events; }
      if (event.status === 'authenticated' || event.status === 'ready') { report.logged_in = true; void finish('UNEXPECTED_AUTHENTICATION'); }
      if (event.status === 'error') {
        report.failure_phase = event.phase;
        report.failure_code = event.error_code;
        if (controlledFailure && event.phase === 'sdk_inject' && event.error_code === 'sdk_error') expectedFault = true;
        else void finish(event.error_code || 'PUBLIC_PAGE_INJECTION_FAILED');
      }
    } });
  host.setVisible(true, { x: 0, y: 0, width: 980, height: 740 });
  const internal = host as any;
  const contents = internal.view.webContents;
  const preferences = contents.getLastWebPreferences();
  report.remote_node_integration_disabled = preferences.nodeIntegration === false && preferences.contextIsolation === true && preferences.sandbox === true;
  report.session_ascii_user_agent = !/[^\x20-\x7e]/.test(contents.session.getUserAgent());
  contents.session.webRequest.onBeforeSendHeaders((details: Electron.OnBeforeSendHeadersListenerDetails, callback: (response: Electron.BeforeSendResponse) => void) => {
    if (details.resourceType === 'mainFrame' && details.url.startsWith('https://web.whatsapp.com/')) {
      const agent = Object.entries(details.requestHeaders).find(([key]) => key.toLowerCase() === 'user-agent')?.[1] || '';
      report.actual_ascii_request = !!agent && !/[^\x20-\x7e]/.test(agent);
      report.application_tokens_omitted = !/ReceptionWorkbench\/|询盘接待助手\/|Electron\//.test(agent);
      report.request_chrome_matches_engine = agent.includes(`Chrome/${process.versions.chrome}`);
    }
    callback({ requestHeaders: details.requestHeaders });
  });
  contents.session.webRequest.onHeadersReceived((details: Electron.HeadersReceivedResponse & { resourceType: string; url: string; statusCode: number }, callback: (response: Electron.HeadersReceivedResponse) => void) => {
    if (details.resourceType === 'mainFrame' && details.url.startsWith('https://web.whatsapp.com/')) report.public_http_status = details.statusCode;
    callback({});
  });
  const initialize = internal.initialize.bind(host);
  internal.initialize = async (client: any, generation: number) => {
    const inject = client.inject.bind(client);
    client.inject = async () => {
      await inject(); report.sdk_inject_completed = true;
      if (controlledFailure) throw new Error('SYNTHETIC_PRIVATE_SDK_POST_QR_FAILURE');
    };
    await initialize(client, generation);
  };
  deadline = setTimeout(() => void finish('PUBLIC_PAGE_TIMEOUT'), 270000);
  watcher = setInterval(() => {
    if (probing || finished) return;
    probing = true;
    void (async () => {
      const result = await contents.executeJavaScript(qrProbe);
      if (finished) return;
      report.remote_business_bridge_absent = result.bridge_absent === true;
      if (result.changed) report.qr_refresh_observed = true;
      if (!result.visible) { if (visibleSince && ++missingSamples > 5) await finish('NATIVE_QR_DISAPPEARED'); return; }
      missingSamples = 0;
      report.native_qr_visible = true;
      const state = host.getStatus().status;
      if (!reference && (controlledFailure ? !expectedFault || state !== 'error' : state !== 'qr' || !report.sdk_inject_completed)) return;
      visibleSince ||= Date.now();
      report.stable_observation_ms = Date.now() - visibleSince;
      if (report.stable_observation_ms >= 30_000 && !report.qr_refresh_observed && !report.qr_refresh_requested) {
        report.qr_refresh_requested = await contents.executeJavaScript('(async () => { try { const cmd = window.require("WAWebCmd").Cmd; if (typeof cmd.refreshQR !== "function") return false; await cmd.refreshQR(); return true; } catch { return false; } })()');
      }
      if (report.stable_observation_ms >= 60_000 && report.qr_refresh_observed) await finish();
    })().catch(() => { if (!finished && visibleSince) void finish('NATIVE_PAGE_PROBE_FAILED'); }).finally(() => { probing = false; });
  }, 1000);
  if (reference) await contents.loadURL('https://web.whatsapp.com/');
  else await host.connect();
}).catch(() => { void finish('FIXTURE_START_FAILED'); });
