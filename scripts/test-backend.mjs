import { spawnSync } from 'node:child_process';
import { readdirSync } from 'node:fs';
import path from 'node:path';
import { root, pythonPath } from './paths.mjs';

const tests = path.join(root, 'services/reception/tests');
const runner = path.join(root, 'services/reception/test_support/run_legacy_tests.py');
const env = {...process.env, PYTHONPATH: path.join(root, 'services/reception/src'), PYTHONIOENCODING: 'utf-8'};
// Run each file in a fresh interpreter: old tests explicitly opt into the
// synthetic service, while new production-entry tests retain the real class.
const [requested, ...filters] = process.argv.slice(2);
const files = requested ? [requested] : readdirSync(tests).filter(name => /^test_.*\.py$/.test(name)).sort();
let failed = false;
for (const file of files) {
  if (!/^test_[^/\\]*\.py$/.test(file)) throw new Error(`Invalid test filename: ${file}`);
  const direct = file.includes('040') || file === 'test_cached_media.py';
  const args = direct
    ? ['-B', '-m', 'unittest', 'discover', '-s', tests, '-p', file, '-v', ...filters]
    : ['-B', runner, file, ...filters];
  const result = spawnSync(pythonPath(), args, {cwd: root, env, stdio: 'inherit'});
  if (result.status !== 0) failed = true;
}
process.exitCode = failed ? 1 : 0;
