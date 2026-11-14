import { app, BrowserWindow } from 'electron';
import puppeteer from 'puppeteer';
import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { WhatsAppHost } from '../../apps/desktop/src/main/whatsapp/host';
import { ViewTransport } from '../../apps/desktop/src/main/whatsapp/transport';

app.setPath('userData', process.env.RECEPTION_WA_FIXTURE_TEMP!);
app.setName('询盘接待助手');
const report = { status: 'failed', reference_no_cdp: process.env.RECEPTION_WA_REFERENCE_NO_CDP === '1', formal_chinese_application_name: true, app_version: app.getVersion(), actual_ascii_request: false, actual_product_version: false, request_chrome_matches_engine: false, cdp_version_matches_engine: false, public_http_status: 0, failed_subresource_count: 0, resource_error_codes: [] as string[], resource_status_counts: {} as Record<string, number>, page_error_types: [] as string[], session_ascii_user_agent: false, non_ascii_subresource_requests: false, public_page: {} as Record<string, boolean>, public_module_names: [] as string[], environment: {} as Record<string, boolean>, same_visible_dom: false, expose_function: false, preload_script: false, remote_page_has_business_bridge: false, sdk_injected_to_qr: false, logged_in: false, real_ai_calls: 0, real_customer_sends: 0, qr_recorded: false, normal_close: false, error_code: '' };
let host: WhatsAppHost | undefined;
let fixtureWindow: BrowserWindow | undefined;
let finished = false;
let timer: ReturnType<typeof setTimeout>;
let referenceTimer: ReturnType<typeof setInterval> | undefined;
async function finish(code = '') {
  if (finished) return;
  finished = true; clearTimeout(timer); clearInterval(referenceTimer); report.error_code = code;
  try {
    const contents = (host as any)?.view.webContents;
    if (contents && !contents.isDestroyed()) report.public_page = await contents.executeJavaScript('(() => { const loaded = name => { try { return !!window.require?.(name); } catch { return false; } }; const text = document.body?.innerText || ""; return { document_complete: document.readyState === "complete", debug_version_available: !!window.Debug?.VERSION, debug_module_available: loaded("Debug"), socket_module_available: loaded("WAWebSocketModel"), module_loader_available: typeof window.require === "function", sdk_utilities_available: typeof window.WWebJS !== "undefined", four_xx_page: /4xx Client Error/i.test(text), browser_unsupported: /browser.{0,40}(not supported|unsupported)|update.{0,20}(your )?browser/i.test(text), qr_canvas_available: !!document.querySelector("canvas[data-ref], [data-testid=qrcode], [data-ref] canvas"), login_copy_visible: /Scan.{0,40}QR|Link with phone number|使用手机|扫描二维码/i.test(text), loading_copy_visible: /loading|正在加载/i.test(text), scripts_present: document.scripts.length > 0, business_bridge_absent: typeof window.reception === "undefined" && typeof process === "undefined" }; })()');
    if (contents && !contents.isDestroyed()) report.public_module_names = await contents.executeJavaScript('(() => { try { return Object.keys(window.require("__debug").modulesMap).filter(name => /^WA(?:Web)?[A-Za-z]*(?:Socket|Conn)[A-Za-z]*$/.test(name)).slice(0,30); } catch { return []; } })()');
  } catch { /* Capture only public symbols/booleans, never credentials or DOM text. */ }
  try { await host?.close(); report.normal_close = true; } catch { report.error_code ||= 'CLOSE_FAILED'; }
  report.status = report.actual_ascii_request && report.session_ascii_user_agent && !report.non_ascii_subresource_requests && report.actual_product_version && report.request_chrome_matches_engine && report.cdp_version_matches_engine && report.public_http_status === 200 && Object.values(report.environment).every(Boolean) && report.same_visible_dom && report.expose_function && report.preload_script && report.sdk_injected_to_qr && report.normal_close && !report.remote_page_has_business_bridge && !report.error_code ? 'passed' : 'failed';
  if (report.reference_no_cdp) report.status = report.public_page.qr_canvas_available && report.public_http_status === 200 && report.normal_close && !report.error_code ? 'reference-ready' : 'reference-not-ready';
  await new Promise<void>(resolve => process.stdout.write('WA_FIXTURE_REPORT ' + JSON.stringify(report) + '\n', () => resolve()));
  fixtureWindow?.destroy(); app.quit();
}
app.whenReady().then(async () => {
  fixtureWindow = new BrowserWindow({ width: 980, height: 740, show: true, webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } });
  host = new WhatsAppHost({ window: fixtureWindow, partition: 'persist:wa-fixture-' + randomUUID(), directory: join(process.env.RECEPTION_WA_FIXTURE_TEMP!, 'ledger'), onEvent: event => {
    if (event.type !== 'account') return;
    if (event.status === 'qr') { report.sdk_injected_to_qr = true; void finish(); }
    else if (event.status === 'error') void finish(event.error_code || 'PUBLIC_PAGE_INJECTION_FAILED');
  } });
  host.setVisible(true, { x: 0, y: 0, width: 980, height: 740 });
  const contents = (host as any).view.webContents;
  report.session_ascii_user_agent = !/[^\x20-\x7e]/.test(contents.session.getUserAgent());
  contents.debugger.on('message', (_event: unknown, method: string, params: any) => {
    const kind = params?.exceptionDetails?.exception?.className;
    if (method === 'Runtime.exceptionThrown' && typeof kind === 'string' && /^[A-Za-z]{1,40}$/.test(kind) && !report.page_error_types.includes(kind)) report.page_error_types.push(kind);
  });
  contents.session.webRequest.onCompleted((details: Electron.OnCompletedListenerDetails) => {
    const key = `${details.resourceType}:${details.statusCode}`;
    report.resource_status_counts[key] = (report.resource_status_counts[key] || 0) + 1;
  });
  contents.session.webRequest.onBeforeSendHeaders((details: Electron.OnBeforeSendHeadersListenerDetails, callback: (response: Electron.BeforeSendResponse) => void) => {
    if (details.resourceType === 'mainFrame' && details.url.startsWith('https://web.whatsapp.com/')) {
      const agent = Object.entries(details.requestHeaders).find(([key]) => key.toLowerCase() === 'user-agent')?.[1] || '';
      report.actual_ascii_request = !!agent && !/[^\x20-\x7e]/.test(agent);
      report.actual_product_version = agent.includes(`ReceptionWorkbench/${app.getVersion()}`);
      report.request_chrome_matches_engine = agent.includes(`Chrome/${process.versions.chrome}`);
    }
    if (details.resourceType !== 'mainFrame') {
      const agent = Object.entries(details.requestHeaders).find(([key]) => key.toLowerCase() === 'user-agent')?.[1] || '';
      if (/[^\x20-\x7e]/.test(agent)) report.non_ascii_subresource_requests = true;
    }
    callback({ requestHeaders: details.requestHeaders });
  });
  contents.session.webRequest.onErrorOccurred((details: Electron.OnErrorOccurredListenerDetails) => {
    report.failed_subresource_count++;
    const code = /^net::(ERR_[A-Z_]+)$/.exec(details.error)?.[1];
    if (code && !report.resource_error_codes.includes(code)) report.resource_error_codes.push(code);
  });
  contents.session.webRequest.onHeadersReceived((details: Electron.OnHeadersReceivedListenerDetails, callback: (response: Electron.HeadersReceivedResponse) => void) => {
    if (details.resourceType === 'mainFrame' && details.url.startsWith('https://web.whatsapp.com/')) report.public_http_status = details.statusCode;
    callback({});
  });
  if (report.reference_no_cdp) {
    timer = setTimeout(() => void finish('REFERENCE_TIMEOUT'), 65000);
    await contents.loadURL('https://web.whatsapp.com/');
    let checking = false;
    referenceTimer = setInterval(() => {
      if (checking || finished) return;
      checking = true;
      void contents.executeJavaScript('!!document.querySelector("canvas[data-ref], [data-testid=qrcode], [data-ref] canvas")').then((ready: boolean) => { if (ready) void finish(); }).catch(() => {}).finally(() => { checking = false; });
    }, 1000);
    return;
  }
  await contents.loadURL('about:blank');
  const transport = new ViewTransport(contents);
  const browser = await puppeteer.connect({ transport, defaultViewport: null });
  const page = await browser.newPage();
  page.on('pageerror', error => {
    const kind = error instanceof Error && /^[A-Za-z]{1,40}$/.test(error.name) ? error.name : 'Error';
    if (!report.page_error_types.includes(kind)) report.page_error_types.push(kind);
  });
  report.cdp_version_matches_engine = (await browser.version()) === `Chrome/${process.versions.chrome}`;
  report.environment = await page.evaluate((chrome: string) => {
    const hints = (navigator as any).userAgentData;
    return { page_ua_matches_engine: navigator.userAgent.includes(`Chrome/${chrome}`), language_list_contains_primary: navigator.languages.includes(navigator.language), timezone_available: !!Intl.DateTimeFormat().resolvedOptions().timeZone, windows_platform: navigator.platform === 'Win32', valid_dpr: Number.isFinite(devicePixelRatio) && devicePixelRatio > 0, client_hints_consistent: !hints || hints.brands.some((brand: any) => brand.brand === 'Chromium' && brand.version === chrome.split('.')[0]), webdriver_type_valid: navigator.webdriver === undefined || typeof navigator.webdriver === 'boolean', chrome_api_type_valid: (window as any).chrome === undefined || typeof (window as any).chrome === 'object', permissions_api_available: typeof navigator.permissions?.query === 'function', plugin_api_available: typeof navigator.plugins?.length === 'number' && typeof navigator.mimeTypes?.length === 'number' };
  }, process.versions.chrome!);
  await page.evaluate(() => { document.body.innerHTML = '<p id="same-view">synthetic</p>'; });
  report.same_visible_dom = await contents.executeJavaScript('document.getElementById("same-view")?.textContent === "synthetic"');
  report.remote_page_has_business_bridge = await contents.executeJavaScript('typeof window.reception !== "undefined" || typeof require !== "undefined" || typeof process !== "undefined"');
  await page.exposeFunction('fixtureAdd', (a: number, b: number) => a + b);
  await page.evaluateOnNewDocument('window.__synthetic_before_load = true');
  await page.goto('about:blank');
  report.expose_function = await page.evaluate(async () => await (window as any).fixtureAdd(2, 3) === 5);
  report.preload_script = await page.evaluate(() => (window as any).__synthetic_before_load === true);
  browser.disconnect(); transport.close();
  timer = setTimeout(() => void finish('PUBLIC_PAGE_TIMEOUT'), 150000);
  await host.connect();
}).catch(() => { void finish('FIXTURE_START_FAILED'); });
