import { spawnSync, spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { root, cleanElectronEnv } from './paths.mjs';

try {
  if (!existsSync(path.join(root, 'artifacts/app/main/index.js'))) {
    const build = spawnSync(process.execPath, [path.join(root, 'scripts/build.mjs')], { cwd: root, stdio: 'ignore', windowsHide: true });
    if (build.status !== 0) throw new Error('build_failed');
  }
  const electron = createRequire(import.meta.url)('electron');
  const child = spawn(electron, [path.join(root, 'artifacts/app/main/index.js'), ...process.argv.slice(2)], {
    cwd: root, env: cleanElectronEnv(), stdio: 'ignore', detached: true, windowsHide: false
  });
  await new Promise((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
  child.unref();
  console.log(JSON.stringify({ status: 'launched_read_check_pending' }));
} catch (error) {
  console.error(JSON.stringify({ status: 'blocked', code: error?.message === 'build_failed' ? 'build_failed' : 'launch_failed' }));
  process.exitCode = 2;
}
