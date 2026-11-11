import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
export const root = fileURLToPath(new URL('../', import.meta.url));
export function pythonPath() {
 const candidates = [process.env.RECEPTION_PYTHON, path.join(root,'services/reception/.venv/Scripts/python.exe'), path.join(root,'services/reception/.venv/bin/python')];
 const found = candidates.find(p=>p && existsSync(p));
 if (!found) throw new Error('Python environment missing. Run the setup instructions in README.md.');
 return found;
}
export function cleanElectronEnv() { const env={...process.env}; delete env.ELECTRON_RUN_AS_NODE; return env; }
