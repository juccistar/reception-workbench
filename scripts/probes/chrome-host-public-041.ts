import { createRequire } from 'node:module';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { ChromeWhatsAppHost } from '../../apps/desktop/src/main/whatsapp/chrome-host';

const [root, output, sdkEntry] = process.argv.slice(2);
assert(root && output && path.isAbsolute(root) && path.isAbsolute(output) && (!sdkEntry || path.isAbsolute(sdkEntry)));
const sdk = createRequire(sdkEntry || path.join(root, 'artifacts/app/main/index.js'))('whatsapp-web.js');
const pause = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
function resources(ownedId: number) {
  const program = `$ErrorActionPreference='Stop';$seen=[Collections.Generic.HashSet[int]]::new();$todo=[Collections.Generic.Queue[int]]::new();$todo.Enqueue(${ownedId});$rows=@();while($todo.Count -gt 0){$ownedId=$todo.Dequeue();if(-not $seen.Add($ownedId)){continue};$item=Get-CimInstance Win32_Process -Filter "ProcessId=$ownedId";if($item){$rows += [pscustomobject]@{pid=$ownedId;working_set=[long]$item.WorkingSetSize;private_bytes=[long]$item.PrivatePageCount;cpu_ms=([long]$item.KernelModeTime+[long]$item.UserModeTime)/10000.0;creation_utc=$item.CreationDate.ToUniversalTime().ToString('o')};Get-CimInstance Win32_Process -Filter "ParentProcessId=$ownedId AND Name='chrome.exe'" | ForEach-Object {$todo.Enqueue([int]$_.ProcessId)}}};ConvertTo-Json -Compress -InputObject @($rows)`;
  const ps = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe');
  return JSON.parse(execFileSync(ps, ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(program, 'utf16le').toString('base64')], { encoding: 'utf8', timeout: 15000, windowsHide: true }));
}
function remainingOwned(rows: Array<{ pid: number; creation_utc: string }>) {
  assert(rows.length > 0 && rows.every(row => Number.isSafeInteger(row.pid) && row.pid > 0));
  const filter = rows.map(row => `ProcessId=${row.pid}`).join(' OR ');
  const program = `$ErrorActionPreference='Stop';$remainingOwned=@(Get-CimInstance Win32_Process -Filter "${filter}" | ForEach-Object {[pscustomobject]@{pid=[int]$_.ProcessId;creation_utc=$_.CreationDate.ToUniversalTime().ToString('o')}});ConvertTo-Json -Compress -InputObject $remainingOwned`;
  const ps = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe');
  const current = JSON.parse(execFileSync(ps, ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(program, 'utf16le').toString('base64')], { encoding: 'utf8', timeout: 15000, windowsHide: true }));
  return current.filter((item: any) => rows.some(row => row.pid === item.pid && row.creation_utc === item.creation_utc));
}
async function main() {
  await mkdir(output);
  const report: any = { status: 'failed', production_host: true, launcher_ui: false, real_system_chrome: true, verified_runtime_snapshot: !!sdkEntry, sdk_initialize_fault_injected: true, real_account_authenticated: false, contact_reads: 0, customer_sends: 0, model_calls: 0, scenarios: [] };
  for (const mode of ['normal', 'initialize-fault']) {
    const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-chrome-host-041-'));
    const row: any = { mode, qr_times: [], statuses: [], screenshots: [] };
    report.scenarios.push(row);
    let auth = false;
    let client: any;
    let host: ChromeWhatsAppHost;
    class PublicClient extends sdk.Client {
      constructor(options: any) {
        super(options); client = this;
        this.on('qr', () => row.qr_times.push(Date.now()));
        for (const event of ['authenticated', 'ready']) this.on(event, () => { auth = true; report.real_account_authenticated = true; void this.pupBrowser?.close().catch(() => {}); });
      }
      async initialize() {
        if (mode === 'normal') return super.initialize();
        const qr = new Promise(resolve => this.once('qr', resolve));
        // Inject rejection at the SDK initialize API boundary while retaining
        // its genuine visible page. No alternate browser or page is created.
        void super.initialize().catch(() => {});
        await qr;
        throw new Error('controlled initialize rejection after first public QR');
      }
    }
    host = new ChromeWhatsAppHost({ profileDirectory: path.join(temporary, 'profile'), directory: path.join(temporary, 'business'), loadSdk: async () => ({ Client: PublicClient }),
      authorizeAccount: async () => { throw new Error('public probe never authorizes an account'); },
      onEvent: event => { if (event.type === 'account') row.statuses.push({ status: event.status, page_state: event.page_state, error_code: event.error_code, phase: event.phase }); },
    });
    const start = Date.now();
    try {
      await host.connect();
      const end = Date.now() + 180000;
      while (!auth && !row.qr_times.length && Date.now() < end) await pause(250);
      assert(!auth, 'authentication was stopped'); assert(row.qr_times.length, 'public QR was not reached');
      const first = Date.now();
      const browser = client.pupBrowser, page = client.pupPage;
      row.browser_version = await browser.version();
      row.startup_to_qr_ms = first - start;
      row.same_sdk_page = (await browser.pages()).some((candidate: any) => candidate === page);
      const cdp = await page.createCDPSession();
      const target = await cdp.send('Target.getTargetInfo');
      const bounds = await cdp.send('Browser.getWindowForTarget', { targetId: target.targetInfo.targetId });
      await cdp.detach();
      row.has_normal_browser_window = Number.isInteger(bounds.windowId) && bounds.bounds.windowState === 'normal';
      row.private_pipe = browser.process().spawnargs.includes('--remote-debugging-pipe');
      row.debugging_tcp_port = browser.process().spawnargs.some((argument: string) => argument.startsWith('--remote-debugging-port'));
      row.visibility = await page.evaluate(() => document.visibilityState);
      while (!auth && (Date.now() - first < 65000 || row.qr_times.length < 2) && Date.now() < end) await pause(500);
      row.observation_ms = Date.now() - first;
      row.qr_refresh_observed = row.qr_times.length >= 2;
      row.page_retained = !page.isClosed() && new URL(page.url()).hostname === 'web.whatsapp.com';
      assert(!auth && row.observation_ms >= 60000 && row.qr_refresh_observed && row.page_retained);
      if (mode === 'initialize-fault') assert.equal(host.getStatus().status, 'error');
      const boxes = await page.$$eval('[data-ref], canvas', (nodes: Element[]) => nodes.map(node => {
        const r = node.getBoundingClientRect();
        return { left: r.left, top: r.top, width: r.width, height: r.height };
      }).filter(r => r.width > 0 && r.height > 0));
      assert(boxes.length > 0, 'no precise public QR mask found');
      assert(boxes.every((box: any) => box.width <= 450 && box.height <= 450), 'QR mask would cover too much of the page');
      // Puppeteer screenshots have no Playwright mask option. Cover only the
      // measured public QR rectangles and remove the covers after capture.
      await page.evaluate((rectangles: Array<{ left: number; top: number; width: number; height: number }>) => {
        for (const r of rectangles) {
          const cover = document.createElement('div');
          cover.dataset.arwProbeMask = 'true';
          Object.assign(cover.style, { position: 'fixed', left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px`, background: '#727a82', zIndex: '2147483647', pointerEvents: 'none' });
          document.documentElement.append(cover);
        }
      }, boxes);
      const shot = `${mode}-public-qr.png`;
      try { await page.screenshot({ path: path.join(output, shot) }); row.screenshots.push(shot); }
      finally { await page.evaluate(() => document.querySelectorAll('[data-arw-probe-mask]').forEach(node => node.remove())); }
      row.mask_rectangles = boxes;
      const owned = browser.process();
      row.process_group = resources(owned.pid);
      await host.close();
      row.close_completed = true;
      row.owned_root_process_exited = owned.exitCode !== null || owned.signalCode !== null;
      assert(row.owned_root_process_exited);
      const closeDeadline = Date.now() + 5000;
      let remaining = remainingOwned(row.process_group);
      while (remaining.length && Date.now() < closeDeadline) { await pause(250); remaining = remainingOwned(row.process_group); }
      row.owned_processes_remaining = remaining.length;
      assert.equal(row.owned_processes_remaining, 0, 'owned Chrome processes still running');
      row.status = 'passed';
    } catch (error) {
      row.status = 'failed'; row.error = error instanceof Error ? error.message : 'probe failed';
      process.exitCode = 1;
    } finally {
      await host.close().catch(() => {});
      await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
      await rm(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }).catch(() => {});
    }
    if (auth) break;
  }
  report.status = report.scenarios.length === 2 && report.scenarios.every((row: any) => row.status === 'passed') && !authGuard(report) ? 'passed' : 'failed';
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
}
function authGuard(report: any) { return report.real_account_authenticated; }
void main().catch(error => { console.error(error instanceof Error ? error.message : 'probe failed'); process.exitCode = 1; });
