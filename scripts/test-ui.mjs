import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

// Vite/Vitest can load duplicate runtime identities when Windows drive casing differs.
let root = path.resolve(fileURLToPath(new URL('../', import.meta.url)));
if (process.platform === 'win32') root = root[0].toUpperCase() + root.slice(1);
const result = spawnSync(process.execPath, [path.join(root, 'node_modules/vitest/vitest.mjs'),
  'run', '--config', 'apps/desktop/vitest.config.ts', ...process.argv.slice(2)],
  { cwd: root, env: process.env, stdio: 'inherit' });
process.exitCode = result.status ?? 2;
