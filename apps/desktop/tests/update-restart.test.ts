// @vitest-environment node
import { afterEach, describe, expect, it } from 'vitest';
import { mkdtemp, mkdir, writeFile, readFile, copyFile, rm, symlink } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { build } from 'esbuild';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { installedDistribution, INSTALL_APP_ID, startUpdateRestart, updateRestartProgram } from '../src/main/update-restart';
import { acquireStorageOperation } from '../src/main/storage-host';
import { acquireInstallationStartGuard } from '../src/main/installation-guard';
import { spawn } from 'node:child_process';
const roots: string[] = [];
const fixture = async () => { const root = await mkdtemp(path.join(tmpdir(), 'reception-update-')); roots.push(root); return root; };
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }))); });
async function setup() {
  const root = await fixture(); const resources = path.join(root, 'resources'); await mkdir(path.join(resources, 'backend'), { recursive: true });
  const executable = path.join(root, 'ReceptionWorkbench.exe'); await copyFile(process.execPath, executable);
  await writeFile(path.join(resources, 'app.asar'), 'test asar');
  await writeFile(path.join(resources, 'backend', 'reception-backend.exe'), 'test backend');
  const installerPath = path.join(root, 'installer.exe'); await writeFile(installerPath, 'unused synthetic installer');
  const installerSha512 = createHash('sha512').update(await readFile(installerPath)).digest('base64');
  const stateDirectory = path.join(root, 'state'); await mkdir(stateDirectory);
  return { root, resources, executable, installerPath, installerSha512, recoveryPrompt: false, stateDirectory, version: '0.2.16', previousVersion: '0.2.15', parentPid: 2147483647, args: [] as string[], resultFile: path.join(root, 'result.json') };
}
const literal = (s: string) => "'" + s.replace(/'/g, "''") + "'";
const cs = (s: string) => '@"' + s.replace(/"/g, '""') + '"';
async function compileInstaller(plan: Awaited<ReturnType<typeof setup>>, kind: 'success' | 'fail') {
  await rm(plan.installerPath);
  const marker = path.join(plan.resources, 'reception-installed.json');
  const childDone = path.join(plan.root, 'child-done.txt');
  const child = `[Threading.Thread]::Sleep(2500);[IO.File]::WriteAllText(${literal(childDone)},'done')`;
  const encoded = Buffer.from(child, 'utf16le').toString('base64');
  const program = kind === 'success'
    ? `System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo("powershell.exe","-NoProfile -NonInteractive -EncodedCommand ${encoded}"){UseShellExecute=false,CreateNoWindow=true}); System.IO.File.WriteAllText(${cs(marker)},${cs(JSON.stringify({ format: 1, app_id: INSTALL_APP_ID, version: plan.version }))});`
    : 'System.Environment.Exit(7);';
  const source = `public class FixtureInstaller { public static void Main(string[] args) { ${program} } }`;
  const command = `Add-Type -TypeDefinition ${literal(source)} -OutputAssembly ${literal(plan.installerPath)} -OutputType ConsoleApplication`;
  await promisify(execFile)('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(command, 'utf16le').toString('base64')], { timeout: 45000 });
  return { ...plan, installerSha512: createHash('sha512').update(await readFile(plan.installerPath)).digest('base64'), childDone };
}
async function readJson(file: string) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) { try { return JSON.parse(await readFile(file, 'utf8')); } catch { await new Promise(resolve => setTimeout(resolve, 100)); } }
  throw new Error('Synthetic helper result missing');
}

describe('installed update and restart boundary', () => {
  it('requires the matching installation marker', async () => {
    const resources = await fixture(); expect(await installedDistribution(resources, '0.2.16')).toBe(false);
    await writeFile(path.join(resources, 'reception-installed.json'), JSON.stringify({ format: 1, app_id: INSTALL_APP_ID, version: '0.2.15' }));
    expect(await installedDistribution(resources, '0.2.16')).toBe(false);
    await writeFile(path.join(resources, 'reception-installed.json'), JSON.stringify({ format: 1, app_id: INSTALL_APP_ID, version: '0.2.16' }));
    expect(await installedDistribution(resources, '0.2.16')).toBe(true);
  });
  it('rejects invalid commands, versions and unsigned installer hashes', async () => {
    const plan = await setup();
    expect(() => updateRestartProgram({ ...plan, executable: 'arbitrary.exe' })).toThrow();
    expect(() => updateRestartProgram({ ...plan, version: '0.2.16; arbitrary' })).toThrow();
    expect(() => updateRestartProgram({ ...plan, args: ['line\ninjection'] })).toThrow();
    await expect(startUpdateRestart({ ...plan, installerSha512: Buffer.alloc(64).toString('base64') })).rejects.toThrow('安装包已变化');
  });
  it.skipIf(process.platform !== 'win32')('hashes the physical ASAR inside Electron before starting and cancelling the real helper', async () => {
    const plan = await setup();
    const project = path.resolve(import.meta.dirname, '../../..');
    const input = path.join(plan.root, 'packaged-source'); await mkdir(input);
    const archive = path.join(plan.resources, 'app.asar');
    const resultFile = path.join(plan.root, 'electron-result.json');
    const harness = String.raw`
      const { app } = require('electron');
      const fs = require('node:fs'); const path = require('node:path');
      const { startUpdateRestart } = require(${JSON.stringify(path.join(project, 'apps/desktop/src/main/update-restart.ts'))});
      const { acquireStorageOperation } = require(${JSON.stringify(path.join(project, 'apps/desktop/src/main/storage-host.ts'))});
      const plan = ${JSON.stringify(plan)};
      app.setPath('userData', path.join(plan.root, 'profile'));
      app.whenReady().then(async () => {
        let helper; let result = {};
        const before = process.noAsar;
        try {
          // Reproduce the failed API in the same runtime, without altering noAsar.
          try { await fs.promises.readFile(${JSON.stringify(archive)}); }
          catch (error) { result.virtualReadError = error.code; }
          helper = await startUpdateRestart(plan);
          const folder = path.join(plan.stateDirectory, 'updates', 'helpers');
          const files = await fs.promises.readdir(folder);
          const script = await fs.promises.readFile(path.join(folder, files[0]), 'utf8');
          const encoded = script.match(/FromBase64String\('([^']+)'\)/)[1];
          result.payload = JSON.parse(Buffer.from(encoded, 'base64').toString('utf8'));
          try { const release = await acquireStorageOperation(plan.stateDirectory); await release(); }
          catch { result.lockHeld = true; }
          await helper.cancel();
          const release = await acquireStorageOperation(plan.stateDirectory); await release();
          result.lockReleased = true;
          result.noAsarUnchanged = process.noAsar === before;
        } catch (error) { result.error = error.stack; }
        finally {
          if (helper) await helper.cancel();
          fs.writeFileSync(${JSON.stringify(resultFile)}, JSON.stringify(result));
          app.quit();
        }
      });
    `;
    await build({ stdin: { contents: harness, resolveDir: project, sourcefile: 'packaged-update-regression.cjs' }, outfile: path.join(input, 'main.cjs'), bundle: true, platform: 'node', format: 'cjs', external: ['electron', 'original-fs'], logLevel: 'silent' });
    await writeFile(path.join(input, 'package.json'), JSON.stringify({ name: 'synthetic-update-regression', version: '1.0.0', main: 'main.cjs' }));
    await rm(archive);
    const { createPackage } = createRequire(import.meta.url)('@electron/asar');
    await createPackage(input, archive);
    const expectedAsar = createHash('sha256').update(await readFile(archive)).digest('hex').toUpperCase();
    const expectedExe = createHash('sha256').update(await readFile(plan.executable)).digest('hex').toUpperCase();
    const expectedBackend = createHash('sha256').update(await readFile(path.join(plan.resources, 'backend', 'reception-backend.exe'))).digest('hex').toUpperCase();
    const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
    await promisify(execFile)(path.join(project, 'node_modules/electron/dist/electron.exe'), [archive], { env, timeout: 75000, windowsHide: true, maxBuffer: 1024 * 1024 });
    const result = JSON.parse(await readFile(resultFile, 'utf8'));
    expect(result.error).toBeUndefined();
    expect(result.virtualReadError).toBe('ENOENT');
    expect(result.payload.fingerprints).toMatchObject({ executable: expectedExe, asar: expectedAsar, backend: expectedBackend });
    expect(result.payload.fingerprints.launchComponents).toHaveLength(8);
    expect(result.payload.fingerprints.launchComponents.every((component: { hash: string | null }) => component.hash === null)).toBe(true);
    expect(result.payload.installer_sha512).toBe(plan.installerSha512);
    expect(result.lockHeld).toBe(true); expect(result.lockReleased).toBe(true);
    expect(result.noAsarUnchanged).toBe(true);
  }, 90000);
  it.skipIf(process.platform !== 'win32')('serializes startup and update and releases the installation gate on cancellation', async () => {
    const plan = await setup();
    const startup = await acquireInstallationStartGuard(plan.executable);
    try { await expect(startUpdateRestart(plan)).rejects.toThrow(); }
    finally { await startup(); }
    const helper = await startUpdateRestart(plan);
    try { await expect(acquireInstallationStartGuard(plan.executable)).rejects.toThrow(); }
    finally { await helper.cancel(); }
    const release = await acquireInstallationStartGuard(plan.executable); await release();
  }, 60000);
  it.skipIf(process.platform !== 'win32')('rejects another process from the same installation', async () => {
    const plan = await setup();
    await rm(plan.executable);
    const compile = `Add-Type -TypeDefinition 'public class FixtureMain { public static void Main() { System.Threading.Thread.Sleep(120000); } }' -OutputAssembly ${literal(plan.executable)} -OutputType ConsoleApplication`;
    await promisify(execFile)('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(compile, 'utf16le').toString('base64')], { timeout: 45000 });
    const child = spawn(plan.executable, [], { windowsHide: true, stdio: 'ignore' });
    const closed = new Promise<void>(resolve => child.once('close', () => resolve()));
    await new Promise<void>((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
    try { await expect(startUpdateRestart(plan)).rejects.toThrow(); }
    finally { child.kill(); await closed; }
    const helper = await startUpdateRestart(plan); await helper.cancel();
  }, 60000);
  it.skipIf(process.platform !== 'win32')('uses one gate for case and junction aliases', async () => {
    const plan = await setup(); const alias = path.join(await fixture(), 'linked');
    await symlink(plan.root, alias, 'junction');
    const release = await acquireInstallationStartGuard(plan.executable);
    try {
      await expect(acquireInstallationStartGuard(path.join(alias, 'RECEPTIONWORKBENCH.EXE'))).rejects.toMatchObject({ code: 'installation_busy' });
    } finally { await release(); }
  }, 60000);
  it.skipIf(process.platform !== 'win32')('rejects an orphan backend from the installation and releases both locks', async () => {
    const plan = await setup(); const backend = path.join(plan.resources, 'backend', 'reception-backend.exe');
    await copyFile(process.execPath, backend);
    const child = spawn(backend, ['-e', 'setInterval(() => {}, 1000)'], { windowsHide: true, stdio: 'ignore' });
    const closed = new Promise<void>(resolve => child.once('close', () => resolve()));
    await new Promise<void>((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
    try { await expect(startUpdateRestart(plan)).rejects.toThrow(); }
    finally { child.kill(); await closed; }
    const release = await acquireInstallationStartGuard(plan.executable); await release();
    const storage = await acquireStorageOperation(plan.stateDirectory); await storage();
  }, 60000);
  it.skipIf(process.platform !== 'win32')('blocks a backend that appears after the first check without restarting the old app', async () => {
    const plan = await compileInstaller(await setup(), 'fail'); const backend = path.join(plan.resources, 'backend', 'reception-backend.exe');
    await copyFile(process.execPath, backend);
    await writeFile(path.join(plan.resources, 'reception-installed.json'), JSON.stringify({ format: 1, app_id: INSTALL_APP_ID, version: plan.previousVersion }));
    const helper = await startUpdateRestart(plan);
    const child = spawn(backend, ['-e', 'setInterval(() => {}, 1000)'], { windowsHide: true, stdio: 'ignore' });
    const closed = new Promise<void>(resolve => child.once('close', () => resolve()));
    await new Promise<void>((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
    try {
      await helper.commit();
      expect(await readJson(plan.resultFile)).toMatchObject({ status: 'failed', code: 'installation_failed', pid: null });
    } finally { child.kill(); await closed; }
    const release = await acquireInstallationStartGuard(plan.executable); await release();
    const storage = await acquireStorageOperation(plan.stateDirectory); await storage();
  }, 60000);
  it.skipIf(process.platform !== 'win32')('rejects an installer changed after acquisition and releases both locks', async () => {
    const plan = await setup(); const helper = await startUpdateRestart(plan);
    await writeFile(plan.installerPath, 'tampered'); await helper.commit();
    expect(await readJson(plan.resultFile)).toMatchObject({ status: 'failed', code: 'installation_failed', pid: null });
    const release = await acquireInstallationStartGuard(plan.executable); await release();
    const storage = await acquireStorageOperation(plan.stateDirectory); await storage();
  }, 60000);
  it.skipIf(process.platform !== 'win32')('restores the intact old app after the installer changes', async () => {
    const plan = await setup();
    await writeFile(path.join(plan.resources, 'reception-installed.json'), JSON.stringify({ format: 1, app_id: INSTALL_APP_ID, version: plan.previousVersion }));
    const recovered = path.join(plan.root, 'recovered.json');
    const helper = await startUpdateRestart({ ...plan, args: ['-e', 'require("node:fs").writeFileSync(process.argv[1],"{}")', '--', recovered] });
    await writeFile(plan.installerPath, 'tampered'); await helper.commit();
    expect(await readJson(plan.resultFile)).toMatchObject({ status: 'started', code: 'previous_version_restored' });
    expect(await readJson(recovered)).toEqual({});
  }, 60000);
  it.skipIf(process.platform !== 'win32')('restores a complete unchanged 0.2.20 installation and rejects missing components', async () => {
    for (const missing of [false, true]) {
      const plan = { ...await setup(), previousVersion: '0.2.20', version: '0.2.21' };
      const scripts = path.join(plan.root, 'scripts'); await mkdir(scripts);
      for (const name of ['connect-live.mjs', 'merchant-profiles.mjs', 'merchant-mutex.mjs', 'launch-identity.mjs', 'windows-job.mjs', 'data-location.mjs', 'release-paths.mjs']) await writeFile(path.join(scripts, name), 'synthetic');
      await writeFile(path.join(plan.resources, 'reception-launch.json'), 'synthetic policy');
      if (missing) await rm(path.join(scripts, 'merchant-mutex.mjs'));
      await writeFile(path.join(plan.resources, 'reception-installed.json'), JSON.stringify({ format: 1, app_id: INSTALL_APP_ID, version: plan.previousVersion }));
      const recovered = path.join(plan.root, 'recovered.json');
      const helper = await startUpdateRestart({ ...plan, args: ['-e', 'require("node:fs").writeFileSync(process.argv[1],"{}")', '--', recovered] });
      await writeFile(plan.installerPath, 'tampered'); await helper.commit();
      if (missing) expect(await readJson(plan.resultFile)).toMatchObject({ status: 'failed', pid: null });
      else {
        expect(await readJson(plan.resultFile)).toMatchObject({ status: 'started', code: 'previous_version_restored' });
        expect(await readJson(recovered)).toEqual({});
      }
    }
  }, 60000);
  it.skipIf(process.platform !== 'win32')('does not restore when a startup component changes', async () => {
    const plan = { ...await setup(), previousVersion: '0.2.20', version: '0.2.21' };
    const scripts = path.join(plan.root, 'scripts'); await mkdir(scripts);
    for (const name of ['connect-live.mjs', 'merchant-profiles.mjs', 'merchant-mutex.mjs', 'launch-identity.mjs', 'windows-job.mjs', 'data-location.mjs', 'release-paths.mjs']) await writeFile(path.join(scripts, name), 'synthetic');
    await writeFile(path.join(plan.resources, 'reception-launch.json'), 'synthetic policy');
    await writeFile(path.join(plan.resources, 'reception-installed.json'), JSON.stringify({ format: 1, app_id: INSTALL_APP_ID, version: plan.previousVersion }));
    const helper = await startUpdateRestart(plan);
    await writeFile(path.join(scripts, 'connect-live.mjs'), 'changed');
    await writeFile(plan.installerPath, 'tampered'); await helper.commit();
    expect(await readJson(plan.resultFile)).toMatchObject({ status: 'failed', code: 'installation_failed', pid: null });
  }, 60000);
  it.skipIf(process.platform !== 'win32')('does not restore after installer tampering when another backend appears', async () => {
    const plan = await setup(); const backend = path.join(plan.resources, 'backend', 'reception-backend.exe');
    await copyFile(process.execPath, backend);
    await writeFile(path.join(plan.resources, 'reception-installed.json'), JSON.stringify({ format: 1, app_id: INSTALL_APP_ID, version: plan.previousVersion }));
    const helper = await startUpdateRestart(plan);
    const child = spawn(backend, ['-e', 'setInterval(() => {}, 1000)'], { windowsHide: true, stdio: 'ignore' });
    const closed = new Promise<void>(resolve => child.once('close', () => resolve()));
    await new Promise<void>((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
    try {
      await writeFile(plan.installerPath, 'tampered'); await helper.commit();
      expect(await readJson(plan.resultFile)).toMatchObject({ status: 'failed', code: 'installation_failed', pid: null });
    } finally { child.kill(); await closed; }
  }, 60000);
  it.skipIf(process.platform !== 'win32')('holds the real storage mutex until cancellation finishes', async () => {
    const plan = await setup(); const helper = await startUpdateRestart(plan);
    try { await expect(acquireStorageOperation(plan.stateDirectory)).rejects.toThrow(); }
    finally { await Promise.all([helper.cancel(), helper.cancel()]); }
    const release = await acquireStorageOperation(plan.stateDirectory); await release();
  }, 60000);
  it.skipIf(process.platform !== 'win32')('waits for a short-lived installer and its child job, then inherits environment and literal arguments', async () => {
    const plan = await compileInstaller(await setup(), 'success');
    const echoed = path.join(plan.root, 'echoed.json');
    const literals = ['space 中文', 'literal"quote', '$(NO_EXPANSION)&value', "apostrophe'", 'trailing\\'];
    const code = 'require("node:fs").writeFileSync(process.argv[1],JSON.stringify({args:process.argv.slice(3),childDone:require("node:fs").existsSync(process.argv[2]),inherited:process.env.ACCIO_GATEWAY_TOKEN==="SYNTHETIC_MEMORY_ONLY"}))';
    const previous = process.env.ACCIO_GATEWAY_TOKEN; process.env.ACCIO_GATEWAY_TOKEN = 'SYNTHETIC_MEMORY_ONLY';
    try {
      const helper = await startUpdateRestart({ ...plan, args: ['-e', code, '--', echoed, plan.childDone, ...literals] });
      await Promise.all([helper.commit(), helper.commit()]);
      const result = await readJson(plan.resultFile);
      expect(result).toMatchObject({ status: 'started', code: 'verify_identity_and_reading', automatic_start_requested: false });
      expect(await readJson(echoed)).toEqual({ args: literals, inherited: true, childDone: true });
      expect(JSON.stringify(result)).not.toContain('SYNTHETIC_MEMORY_ONLY');
    } finally { if (previous === undefined) delete process.env.ACCIO_GATEWAY_TOKEN; else process.env.ACCIO_GATEWAY_TOKEN = previous; }
  }, 60000);
  it.skipIf(process.platform !== 'win32')('recovers an unchanged old program after an installer exits with failure', async () => {
    const plan = await compileInstaller(await setup(), 'fail');
    await writeFile(path.join(plan.resources, 'reception-installed.json'), JSON.stringify({ format: 1, app_id: INSTALL_APP_ID, version: plan.previousVersion }));
    const echoed = path.join(plan.root, 'recovered.json');
    const helper = await startUpdateRestart({ ...plan, args: ['-e', 'require("node:fs").writeFileSync(process.argv[1],JSON.stringify({recovered:true}))', '--', echoed] });
    await helper.commit();
    expect(await readJson(plan.resultFile)).toMatchObject({ status: 'started', code: 'previous_version_restored', automatic_start_requested: false });
    expect(await readJson(echoed)).toEqual({ recovered: true });
  }, 60000);
});
