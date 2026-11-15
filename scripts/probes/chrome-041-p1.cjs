const { Client } = require('whatsapp-web.js');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');

const label = process.argv[2];
if (!/^[a-z0-9-]+$/.test(label || '')) throw Error('new label required');
const output = path.resolve(__dirname, '../../artifacts/verification/chrome-041-p1-' + label);
const chrome = path.join(process.env.LOCALAPPDATA, 'Google/Chrome/Application/chrome.exe');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const now = () => new Date().toISOString();
const all = { chrome, sdk: require('whatsapp-web.js/package.json').version, puppeteer: require('puppeteer/package.json').version, scenarios: [] };
function owned(profile) {
  const escaped = profile.replaceAll("'", "''");
  const script = `$p='${escaped}'; @(Get-CimInstance Win32_Process -Filter "name='chrome.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($p) } | ForEach-Object { [pscustomobject]@{pid=$_.ProcessId; pipe=$_.CommandLine.Contains('--remote-debugging-pipe'); port=$_.CommandLine -match '--remote-debugging-port'; window=((Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue).MainWindowHandle -ne [IntPtr]::Zero)} }) | ConvertTo-Json -Compress`;
  const raw = execFileSync('powershell.exe', ['-NoProfile', '-Command', script], {encoding:'utf8'}).trim();
  return raw ? JSON.parse(raw) : [];
}
(async () => {
  await fs.mkdir(output, { recursive: false });
  for (const mode of ['normal', 'sdk-fault']) {
    const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'arw-041-chrome-'));
    const report = {mode, started:now(), qr_times:[], phase:'launching', authenticated:false};
    all.scenarios.push(report);
    let client = new Client({userAgent:false, puppeteer:{executablePath:chrome, headless:false, pipe:true, userDataDir:profile}, webVersionCache:{type:'none'}});
    let auth = false, initError = null;
    client.on('qr', () => { report.qr_times.push(now()); if (report.qr_times.length === 1) report.phase='first-qr'; });
    for (const e of ['authenticated','ready']) client.on(e, () => {auth=true; report.authenticated=true; report.phase=e; client.pupBrowser?.close().catch(()=>{});});
    client.initialize().catch(e => {initError = String(e.message).slice(0,300);});
    try {
      const end = Date.now()+210000;
      while (!auth && !initError && report.qr_times.length === 0 && Date.now() < end) await sleep(500);
      if (!report.qr_times.length) throw Error(initError || 'first QR timeout');
      const first = Date.now();
      const page = client.pupPage, browser = client.pupBrowser;
      report.browser_version = await browser.version();
      report.same_target = (await browser.pages()).filter(p => p.target() === page.target()).length === 1;
      report.target_url_is_whatsapp = new URL(page.url()).hostname === 'web.whatsapp.com';
      const cdp = await browser.target().createCDPSession();
      const targetInfo = await (await page.createCDPSession()).send('Target.getTargetInfo');
      const windowInfo = await cdp.send('Browser.getWindowForTarget', {targetId:targetInfo.targetInfo.targetId});
      report.target_has_browser_window = Number.isInteger(windowInfo.windowId);
      report.browser_window_state = windowInfo.bounds.windowState;
      await cdp.detach();
      report.visible = await page.evaluate(() => ({visibility:document.visibilityState, focused:document.hasFocus(), userAgent:navigator.userAgent}));
      report.chrome_processes = owned(profile);
      if (mode === 'sdk-fault') {
        const original = page.evaluate;
        page.evaluate = async () => { throw Error('controlled SDK evaluation failure'); };
        try { await client.getWWebVersion(); } catch (e) { report.controlled_sdk_error = e.message; }
        finally { page.evaluate = original; }
      }
      while (!auth && !initError && Date.now() < end && (Date.now()-first < 65000 || report.qr_times.length < 2)) await sleep(1000);
      report.observation_ms = Date.now()-first;
      report.init_error = initError;
      report.page_retained = !page.isClosed() && new URL(page.url()).hostname === 'web.whatsapp.com';
      report.qr_refresh_observed = report.qr_times.length >= 2;
      if (!auth && report.page_retained) {
        await page.evaluate(() => { const cover=document.createElement('div'); cover.id='probe-redaction'; Object.assign(cover.style,{position:'fixed',inset:'0',zIndex:'2147483647',background:'#fff',color:'#111',font:'24px sans-serif',padding:'32px'}); cover.textContent='WhatsApp Web public page — all page content redacted'; document.documentElement.appendChild(cover); });
        await page.screenshot({path:path.join(output,mode+'-redacted.png')});
      }
      report.phase = auth ? 'authenticated-stopped' : (report.page_retained && report.qr_refresh_observed && report.observation_ms >= 60000 ? 'passed' : 'failed');
    } catch(e) { report.error=String(e.message).slice(0,300); report.phase='failed'; }
    finally {
      try { if(client.pupBrowser) await client.pupBrowser.close(); report.close='completed'; } catch(e) {report.close_error=String(e.message).slice(0,200);}
      await sleep(2000);
      try { report.remaining_owned_processes=owned(profile); } catch(e) {report.process_check_error=e.message;}
      try { await fs.rm(profile,{recursive:true,force:true,maxRetries:5,retryDelay:500}); } catch(e) {report.profile_cleanup_error=e.message;}
      report.finished=now();
      await fs.writeFile(path.join(output,'report.json'),JSON.stringify(all,null,2));
    }
    if (auth) break;
  }
  console.log(JSON.stringify(all,null,2));
})().catch(e => {console.error(e.message); process.exitCode=1;});
