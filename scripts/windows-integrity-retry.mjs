import { createRequire } from 'node:module';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const require = createRequire(import.meta.url);

export async function retryIntegrityWrite(action, executable, wait = delay) {
  for (let attempt = 0; ; attempt++) {
    try { return await action(); }
    catch (error) {
      const sameFile = typeof error.path === 'string'
        && path.resolve(error.path).toLowerCase() === path.resolve(executable).toLowerCase();
      const retryable = sameFile && error.syscall === 'open'
        && ['UNKNOWN', 'EBUSY', 'EPERM', 'EACCES'].includes(error.code);
      if (!retryable || attempt >= 4) throw error;
      await wait([500, 1500, 3000, 6000][attempt]);
    }
  }
}

export function installWindowsIntegrityRetry(output) {
  // electron-builder 26.18 calls this exported helper immediately after renaming
  // the EXE. Keep the complete official integrity update; retry only a failed
  // open of this build's exact EXE, before any write has started.
  const module = require('app-builder-lib/out/electron/electronWin.js');
  const original = module.addWinAsarIntegrity;
  const expected = path.resolve(output, 'win-unpacked/ReceptionWorkbench.exe');
  module.addWinAsarIntegrity = async (file, integrity) => {
    if (path.resolve(file).toLowerCase() !== expected.toLowerCase()) return original(file, integrity);
    return retryIntegrityWrite(() => original(file, integrity), file);
  };
  return () => { module.addWinAsarIntegrity = original; };
}
