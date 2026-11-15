import { app, session } from 'electron';
import path from 'node:path';
import { mkdir } from 'node:fs/promises';
import { runLauncher } from '../../apps/desktop/src/main/launcher-host';
import { createLauncherRegistry } from '../../apps/desktop/src/main/launcher-registry';

const temporary = process.env.HARNESS_TEMP!;
if (!temporary || !path.isAbsolute(temporary)) throw new Error('isolated fixture directory required');
(globalThis as any).seedCompanyNames = async () => {
  const registry = await createLauncherRegistry();
  for (const [seller_id, name] of [['10001', '合成公司甲'], ['10002', '合成公司乙' + '长名称'.repeat(60)]]) {
    const directories = { registry_directory: path.join(temporary, 'merchants', seller_id), state_directory: path.join(temporary, 'states', seller_id), data_root: path.join(temporary, 'data', seller_id) };
    await Promise.all(Object.values(directories).map(directory => mkdir(directory, { recursive: true })));
    await registry.registerVerified({ seller_id, name, ...directories, verified_at: 1700000000000 });
  }
};
(globalThis as any).quitProbe = () => app.quit();
void app.whenReady().then(() => {
  session.defaultSession.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !details.url.startsWith('file:') && !details.url.startsWith('devtools:') }));
});
void runLauncher().catch(error => { console.error(error instanceof Error ? error.message : 'isolated launcher failed'); app.exit(1); });
