import { lstatSync, readFileSync, openSync, writeFileSync, fsyncSync, closeSync, renameSync, unlinkSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { randomBytes } from 'node:crypto';

const MAX_CONFIG = 64 * 1024;
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

function localPath(value, label, mustExist = false, directory = true) {
  if (typeof value !== 'string' || !value.trim() || value !== value.trim() || value.includes('\0') || !path.isAbsolute(value) || /^(?:\\\\|\/\/)/.test(value)) throw new Error(`invalid_${label}`);
  const resolved = path.resolve(value);
  if (resolved === path.parse(resolved).root) throw new Error(`invalid_${label}`);
  if (process.platform === 'win32') {
    const parts = resolved.slice(path.parse(resolved).root.length).split(path.sep);
    if (parts.some(part => /[<>:"|?*\x00-\x1f]/.test(part) || /[. ]$/.test(part) || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(part))) throw new Error(`invalid_${label}`);
  }
  let cursor = path.parse(resolved).root;
  let missing = false;
  for (const part of resolved.slice(cursor.length).split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    if (missing) continue;
    try {
      const stat = lstatSync(cursor);
      if (stat.isSymbolicLink()) throw new Error(`invalid_${label}`);
      if (cursor !== resolved && !stat.isDirectory()) throw new Error(`invalid_${label}`);
      if (cursor === resolved && directory && !stat.isDirectory()) throw new Error(`invalid_${label}`);
    } catch (error) {
      if (error.code === 'ENOENT') { missing = true; continue; }
      throw error;
    }
  }
  if (missing && mustExist) throw new Error(`unavailable_${label}`);
  return resolved;
}

export function getBootstrapPaths(env = process.env) {
  const base = env.LOCALAPPDATA;
  if (!base && !env.RECEPTION_BOOTSTRAP_DIR) throw new Error('localappdata_missing');
  const stateDir = localPath(env.RECEPTION_BOOTSTRAP_DIR || path.join(base, 'AlibabaReceptionWorkbenchHost'), 'state_dir');
  const defaultRoot = localPath(path.join(base || path.dirname(stateDir), 'AlibabaReceptionWorkbenchV2'), 'default_root');
  return { configFile: path.join(stateDir, 'storage.json'), stateDir, defaultRoot };
}

function validateConfig(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || value.version !== 1 || Object.keys(value).some(k => !['version','dataRoot','previousRoot','lastBackup','lastResult'].includes(k))) throw new Error('invalid_bootstrap');
  const result = {version: 1};
  for (const key of ['dataRoot', 'previousRoot']) if (own(value, key)) result[key] = localPath(value[key], key);
  if (own(value, 'lastBackup')) {
    const b = value.lastBackup;
    if (!b || typeof b !== 'object' || Array.isArray(b) || Object.keys(b).some(k => !['path','completedAt'].includes(k)) || !Number.isFinite(b.completedAt) || b.completedAt < 0) throw new Error('invalid_bootstrap');
    result.lastBackup = {path: localPath(b.path, 'backup_path', false, false), completedAt: b.completedAt};
  }
  if (own(value, 'lastResult')) {
    const r = value.lastResult;
    if (!r || typeof r !== 'object' || Array.isArray(r) || Object.keys(r).some(k => !['action','status','message','completedAt'].includes(k)) || !['action','status','message'].every(k => typeof r[k] === 'string' && r[k].length <= 4096) || !Number.isFinite(r.completedAt) || r.completedAt < 0) throw new Error('invalid_bootstrap');
    result.lastResult = {action:r.action, status:r.status, message:r.message, completedAt:r.completedAt};
  }
  return result;
}

export function readBootstrap(configFile) {
  const file = localPath(configFile, 'config_file', false, false);
  try {
    const stat = lstatSync(file);
    if (!stat.isFile() || stat.size > MAX_CONFIG) throw new Error('invalid_bootstrap');
    const bytes = readFileSync(file);
    if (bytes.length > MAX_CONFIG) throw new Error('invalid_bootstrap');
    return validateConfig(JSON.parse(bytes.toString('utf8')));
  } catch (error) {
    if (error.code === 'ENOENT') return {version: 1};
    throw error;
  }
}

export function writeBootstrap(configFile, next, expectedCurrent) {
  const file = localPath(configFile, 'config_file', false, false);
  const data = JSON.stringify(validateConfig(next), null, 2) + '\n';
  if (Buffer.byteLength(data) > MAX_CONFIG) throw new Error('invalid_bootstrap');
  if (expectedCurrent !== undefined && JSON.stringify(readBootstrap(file)) !== JSON.stringify(validateConfig(expectedCurrent))) throw new Error('bootstrap_changed');
  mkdirSync(path.dirname(file), {recursive: true});
  localPath(path.dirname(file), 'state_dir', true);
  const temp = path.join(path.dirname(file), `.storage-${process.pid}-${randomBytes(12).toString('hex')}.tmp`);
  let fd;
  try {
    fd = openSync(temp, 'wx', 0o600);
    writeFileSync(fd, data, 'utf8');
    fsyncSync(fd);
    closeSync(fd); fd = undefined;
    if (expectedCurrent !== undefined && JSON.stringify(readBootstrap(file)) !== JSON.stringify(validateConfig(expectedCurrent))) throw new Error('bootstrap_changed');
    localPath(file, 'config_file', false, false);
    renameSync(temp, file);
  } finally {
    if (fd !== undefined) closeSync(fd);
    try { unlinkSync(temp); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
}

function pathKey(value) { const normalized = path.resolve(value); return process.platform === 'win32' ? normalized.toLowerCase() : normalized; }
function samePath(a, b) { return pathKey(a) === pathKey(b); }
function containsPath(a, b) { const relative = path.relative(pathKey(a), pathKey(b)); return !relative || (relative !== '..' && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative)); }
function selectedPath(root, stateDir, env) {
  if (containsPath(root, stateDir) || containsPath(stateDir, root)) throw new Error('data_directory_overlaps_bootstrap');
  for (const protectedRoot of [env.WINDIR, env.ProgramFiles, env['ProgramFiles(x86)']].filter(Boolean)) {
    if (containsPath(protectedRoot, root)) throw new Error('reserved_data_directory');
  }
  return root;
}
export function resolveDataLocation(argv, env = process.env, fallbackRoot) {
  const {configFile, defaultRoot, stateDir} = getBootstrapPaths(env);
  const config = readBootstrap(configFile);
  let argument;
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--data-dir' || arg.startsWith('--data-dir=')) {
      if (argument !== undefined) throw new Error('duplicate_data_dir');
      argument = arg === '--data-dir' ? argv[++i] : arg.slice('--data-dir='.length);
      if (!argument || argument.startsWith('--')) throw new Error('invalid_data_dir');
    }
  }
  const configuredRoot = config.dataRoot ?? null;
  const checked = value => selectedPath(value, stateDir, env);
  if (argument !== undefined) {
    const candidate = localPath(argument, 'data_dir');
    if (configuredRoot && config.previousRoot && samePath(candidate, config.previousRoot) && !samePath(candidate, configuredRoot) && (argv.includes('--live') || argv.includes('--connect-from-accio'))) throw new Error('retired_data_directory');
    const currentRoot = localPath(candidate, 'data_dir', !!configuredRoot && samePath(candidate, configuredRoot));
    return {currentRoot: checked(currentRoot), defaultRoot, configuredRoot, configFile, source:'argument'};
  }
  if (configuredRoot !== null) return {currentRoot: checked(localPath(configuredRoot, 'data_root', true)), defaultRoot, configuredRoot, configFile, source:'configured'};
  if (fallbackRoot !== undefined) return {currentRoot: checked(localPath(fallbackRoot, 'legacy_root', true)), defaultRoot, configuredRoot, configFile, source:'legacy'};
  return {currentRoot: checked(defaultRoot), defaultRoot, configuredRoot, configFile, source:'default'};
}

export function restartArguments(argv, root) {
  const result = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--data-dir') { if (i + 1 < argv.length && !argv[i + 1].startsWith('--')) i++; continue; }
    if (argv[i].startsWith('--data-dir=')) continue;
    result.push(argv[i]);
  }
  return [...result, '--data-dir', localPath(root, 'data_dir')];
}
