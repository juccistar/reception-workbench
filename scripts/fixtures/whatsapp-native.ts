import { app, BrowserWindow } from 'electron';
import puppeteer from 'puppeteer';
import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { WhatsAppHost } from '../../apps/desktop/src/main/whatsapp/host';
import { ViewTransport } from '../../apps/desktop/src/main/whatsapp/transport';

app.setPath('userData', process.env.RECEPTION_WA_FIXTURE_TEMP!);
const report = { status: 'failed', same_visible_dom: false, expose_function: false, preload_script: false, remote_page_has_business_bridge: false, sdk_injected_to_qr: false, logged_in: false, real_ai_calls: 0, real_customer_sends: 0, qr_recorded: false, normal_close: false, error_code: '' };
let host: WhatsAppHost | undefined;
let window: BrowserWindow | undefined;
let finished = false;
let timer: ReturnType<typeof setTimeout>;
async function finish(code = '') {
  if (finished) return;
  finished = true; clearTimeout(timer); report.error_code = code;
  try { await host?.close(); report.normal_close = true; } catch { report.error_code ||= 'CLOSE_FAILED'; }
  report.status = report.same_visible_dom && report.expose_function && report.preload_script && report.sdk_injected_to_qr && report.normal_close && !report.remote_page_has_business_bridge && !report.error_code ? 'passed' : 'failed';
  process.stdout.write('WA_FIXTURE_REPORT ' + JSON.stringify(report) + '\n');
  window?.destroy(); app.quit();
}
app.whenReady().then(async () => {
  window = new BrowserWindow({ width: 980, height: 740, show: true, webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } });
  host = new WhatsAppHost({ window, partition: 'persist:wa-fixture-' + randomUUID(), directory: join(process.env.RECEPTION_WA_FIXTURE_TEMP!, 'ledger'), onEvent: event => {
    if (event.type !== 'account') return;
    if (event.status === 'qr') { report.sdk_injected_to_qr = true; void finish(); }
    else if (event.status === 'error') void finish('PUBLIC_PAGE_INJECTION_FAILED');
  } });
  host.setVisible(true, { x: 0, y: 0, width: 980, height: 740 });
  const contents = (host as any).view.webContents;
  await contents.loadURL('about:blank');
  const transport = new ViewTransport(contents);
  const browser = await puppeteer.connect({ transport, defaultViewport: null });
  const page = await browser.newPage();
  await page.evaluate(() => { document.body.innerHTML = '<p id="same-view">synthetic</p>'; });
  report.same_visible_dom = await contents.executeJavaScript('document.getElementById("same-view")?.textContent === "synthetic"');
  report.remote_page_has_business_bridge = await contents.executeJavaScript('typeof window.reception !== "undefined" || typeof require !== "undefined" || typeof process !== "undefined"');
  await page.exposeFunction('fixtureAdd', (a: number, b: number) => a + b);
  report.expose_function = await page.evaluate(async () => await (window as any).fixtureAdd(2, 3) === 5);
  await page.evaluateOnNewDocument('window.__synthetic_before_load = true');
  await page.goto('about:blank');
  report.preload_script = await page.evaluate(() => (window as any).__synthetic_before_load === true);
  browser.disconnect(); transport.close();
  timer = setTimeout(() => void finish('PUBLIC_PAGE_TIMEOUT'), 35000);
  await host.connect();
}).catch(() => void finish('FIXTURE_START_FAILED'));
