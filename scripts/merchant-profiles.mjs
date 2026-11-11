import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { lstatSync, realpathSync, readFileSync, mkdirSync, openSync, writeFileSync, fsyncSync, closeSync, renameSync, unlinkSync, readdirSync } from 'node:fs';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readBootstrap } from './data-location.mjs';
import { acquireMerchantRegistryMutex } from './merchant-mutex.mjs';

// Routing metadata is never authorization. The business service verifies again.
export const AUTO_LAUNCH_CONTEXT_ENV = 'RECEPTION_AUTO_LAUNCH_CONTEXT';
const ID = /^[1-9][0-9]{0,127}$/;
const samePath = (a, b) => process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b;
const exactKeys = (value, keys) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
function safePath(value, mustExist = false, directory = true) {
  if (typeof value !== 'string' || !value || value.length > 32768 || value !== value.trim() || /[\x00-\x1f]/.test(value) || !path.isAbsolute(value) || /^(?:\\\\|\/\/)/.test(value)) throw new Error('merchant_path_invalid');
  const resolved = path.resolve(value);
  if (resolved === path.parse(resolved).root || !samePath(value, resolved)) throw new Error('merchant_path_invalid');
  let cursor = path.parse(resolved).root, missing = false;
  const parts = resolved.slice(cursor.length).split(path.sep).filter(Boolean);
  for (const [index, part] of parts.entries()) {
    if (process.platform === 'win32' && (/[<>:"|?*]/.test(part) || /[. ]$/.test(part) || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(part))) throw new Error('merchant_path_invalid');
    cursor = path.join(cursor, part);
    if (missing) continue;
    try {
      const info = lstatSync(cursor);
      if (info.isSymbolicLink() || !samePath(path.resolve(realpathSync.native(cursor)), path.resolve(cursor))) throw new Error('merchant_path_invalid');
      if ((index < parts.length - 1 || directory) && !info.isDirectory()) throw new Error('merchant_path_invalid');
      if (index === parts.length - 1 && !directory && !info.isFile()) throw new Error('merchant_path_invalid');
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      missing = true;
    }
  }
  if (mustExist && missing) throw new Error('merchant_workspace_missing');
  return resolved;
}
function exists(value) { try { lstatSync(value); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; } }
function overlaps(left, right) {
  const inside = (a, b) => { const relative = path.relative(a, b); return !relative || (!path.isAbsolute(relative) && relative !== '..' && !relative.startsWith('..' + path.sep)); };
  const a = process.platform === 'win32' ? left.toLowerCase() : left, b = process.platform === 'win32' ? right.toLowerCase() : right;
  return inside(a, b) || inside(b, a);
}
function readJSON(file, limit = 1024 * 1024) {
  safePath(file, true, false);
  if (lstatSync(file).size > limit) throw new Error('merchant_registry_invalid');
  const bytes = readFileSync(file);
  if (bytes.length > limit) throw new Error('merchant_registry_invalid');
  return JSON.parse(bytes.toString('utf8'));
}
function atomicJSON(file, value) {
  safePath(file, false, false);
  const temporary = path.join(path.dirname(file), '.merchant-' + randomUUID() + '.tmp');
  const bytes = JSON.stringify(value, null, 2) + '\n';
  if (Buffer.byteLength(bytes) > 1024 * 1024) throw new Error('merchant_registry_invalid');
  let handle;
  try {
    handle = openSync(temporary, 'wx', 0o600); writeFileSync(handle, bytes, 'utf8'); fsyncSync(handle); closeSync(handle); handle = undefined;
    safePath(file, false, false); renameSync(temporary, file);
  } finally {
    if (handle !== undefined) closeSync(handle);
    try { unlinkSync(temporary); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
}
function ensureDirectory(value) { safePath(value); mkdirSync(value, { recursive: true, mode: 0o700 }); return safePath(value, true); }
export function merchantRegistryDirectory(env) {
  if (env.RECEPTION_MERCHANT_REGISTRY_DIR) return safePath(env.RECEPTION_MERCHANT_REGISTRY_DIR);
  if (!env.LOCALAPPDATA) throw new Error('merchant_registry_unavailable');
  return safePath(path.join(env.LOCALAPPDATA, 'AlibabaReceptionWorkbenchHost'));
}
export function merchantSupportPaths(registryDirectory) {
  const resolved = path.resolve(registryDirectory);
  const namespace = createHash('sha256').update(process.platform === 'win32' ? resolved.toLowerCase() : resolved).digest('hex').slice(0, 16);
  return { dataDirectory: path.join(path.dirname(resolved), 'ARWData', namespace), backupDirectory: path.join(path.dirname(resolved), 'ARWBackups', namespace) };
}
function profilePaths(directory, sellerId) {
  const home = path.join(directory, 'profiles', createHash('sha256').update(sellerId).digest('hex'));
  return { home, stateDirectory: path.join(home, 'state'), legacyDataRoot: path.join(home, 'data'), oldDataRoot: path.join(directory, 'data', sellerId), dataRoot: path.join(merchantSupportPaths(directory).dataDirectory, sellerId) };
}
function loadRegistry(directory) {
  const file = path.join(directory, 'merchants.json');
  if (!exists(file)) return { format: 1, profiles: {} };
  const registry = readJSON(file);
  if (!exactKeys(registry, ['format', 'profiles']) || registry.format !== 1 || !registry.profiles || Array.isArray(registry.profiles) || typeof registry.profiles !== 'object' || Object.keys(registry.profiles).length > 512) throw new Error('merchant_registry_invalid');
  for (const [key, record] of Object.entries(registry.profiles)) {
    if (!ID.test(key) || !exactKeys(record, ['sellerId', 'stateDirectory', 'initialDataRoot', 'status']) || record.sellerId !== key || !['created', 'verified'].includes(record.status)) throw new Error('merchant_registry_invalid');
    const expected = profilePaths(directory, key).stateDirectory;
    if (!samePath(safePath(record.stateDirectory), expected)) throw new Error('merchant_registry_invalid');
    safePath(record.initialDataRoot);
  }
  return registry;
}
function currentDataRoot(record) {
  const config = path.join(safePath(record.stateDirectory, true), 'storage.json');
  const saved = readBootstrap(config);
  if (!saved.dataRoot) throw new Error('merchant_profile_unavailable');
  return safePath(saved.dataRoot, true);
}
function validateSeparation(directory, sellerId, dataRoot, registry, programDirectory) {
  const own = profilePaths(directory, sellerId);
  if (overlaps(dataRoot, own.stateDirectory) || samePath(dataRoot, directory) || overlaps(dataRoot, path.join(directory, 'merchants.json'))) throw new Error('merchant_path_conflict');
  if (programDirectory && overlaps(dataRoot, safePath(programDirectory, true))) throw new Error('merchant_path_conflict');
  // Managed profile subtrees cannot be appropriated by a different seller.
  const profilesBase = path.join(directory, 'profiles');
  if (overlaps(dataRoot, profilesBase) && !(samePath(dataRoot, own.legacyDataRoot) || path.relative(own.legacyDataRoot, dataRoot).startsWith('..') === false && !path.isAbsolute(path.relative(own.legacyDataRoot, dataRoot)))) throw new Error('merchant_path_conflict');
  const managedData = path.join(directory, 'data');
  if (overlaps(dataRoot, managedData) && !samePath(dataRoot, own.oldDataRoot)) throw new Error('merchant_path_conflict');
  const newManagedData = merchantSupportPaths(directory).dataDirectory;
  const registeredRoot = registry.profiles[sellerId] && currentDataRoot(registry.profiles[sellerId]);
  if (overlaps(dataRoot, newManagedData) && !samePath(dataRoot, own.dataRoot) && !(registeredRoot && samePath(dataRoot, registeredRoot) && path.dirname(dataRoot) === newManagedData && new RegExp(`^${sellerId}-recovery-[0-9a-f]{8}$`).test(path.basename(dataRoot)))) throw new Error('merchant_path_conflict');
  for (const [otherId, record] of Object.entries(registry.profiles)) {
    if (otherId === sellerId) continue;
    const state = safePath(record.stateDirectory, true);
    const current = currentDataRoot(record);
    if (overlaps(dataRoot, state) || overlaps(dataRoot, current) || overlaps(dataRoot, safePath(record.initialDataRoot))) throw new Error('merchant_path_conflict');
    const config = readBootstrap(path.join(state, 'storage.json'));
    if (config.previousRoot && overlaps(dataRoot, safePath(config.previousRoot))) throw new Error('merchant_path_conflict');
  }
}
export function maintenanceEnvironment(env = {}) {
  const allowed = new Set(['systemroot', 'windir', 'systemdrive', 'temp', 'tmp', 'path', 'pathext', 'comspec', 'localappdata', 'userprofile']);
  const result = {};
  for (const [key, value] of Object.entries(env)) if (allowed.has(key.toLowerCase()) && typeof value === 'string') result[key] = value;
  result.PYTHONIOENCODING = 'utf-8';
  return result;
}
async function inspectOwnership(dataRoot, options) {
  if (!exists(dataRoot)) return { status: 'missing' };
  safePath(dataRoot, true);
  let result;
  if (options.inspectOwnership) result = await options.inspectOwnership(dataRoot);
  else {
    const backend = options.backend;
    if (!backend || typeof backend.executable !== 'string' || !Array.isArray(backend.prefix) || !backend.prefix.every(item => typeof item === 'string')) throw new Error('merchant_ownership_unavailable');
    try {
      const output = await promisify(execFile)(backend.executable, [...backend.prefix, '--inspect-launch-owner', '--data-dir', dataRoot], { env: maintenanceEnvironment(backend.env), cwd: backend.cwd, windowsHide: true, timeout: 30000, maxBuffer: 16384, encoding: 'utf8' });
      result = JSON.parse(output.stdout.trim());
    } catch { throw new Error('merchant_ownership_unavailable'); }
  }
  if (!result || !['missing', 'empty', 'owned', 'unknown', 'multiple'].includes(result.status) || (result.status === 'owned' && (typeof result.sellerId !== 'string' || !ID.test(result.sellerId)))) throw new Error('merchant_ownership_unavailable');
  return result;
}
// Windows APIs count UTF-16 code units, including the terminating NUL.
export function workspacePathUnits(dataRoot) {
  return path.join(dataRoot, 'live', 'sellers', 'a'.repeat(64), 'workspace.sqlite3-journal').length + 1;
}
function checkWorkspacePath(dataRoot, budget = 260) {
  if (workspacePathUnits(dataRoot) > budget) throw new Error('merchant_workspace_path_too_long');
}
function ownershipMatches(result, sellerId, allowEmpty) {
  if (result.status === 'owned') { if (result.sellerId !== sellerId) throw new Error('merchant_workspace_mismatch'); return 'verified'; }
  if (allowEmpty && result.status === 'empty') return 'created';
  throw new Error('merchant_workspace_ownership_unknown');
}
export async function resolveMerchantProfile(sellerId, env, options = {}) {
  if (typeof sellerId !== 'string' || !ID.test(sellerId)) throw new Error('merchant_identity_invalid');
  const directory = merchantRegistryDirectory(env); ensureDirectory(directory);
  const release = await acquireMerchantRegistryMutex(directory);
  try {
    const registry = loadRegistry(directory), registryFile = path.join(directory, 'merchants.json');
    let record = registry.profiles[sellerId];
    if (record) {
      const dataRoot = currentDataRoot(record);
      if (options.dataRoot && !samePath(safePath(options.dataRoot, true), dataRoot)) throw new Error('merchant_profile_root_conflict');
      validateSeparation(directory, sellerId, dataRoot, registry, options.programDirectory);
      const owner = await inspectOwnership(dataRoot, options);
      if (record.status === 'created' && samePath(dataRoot, record.initialDataRoot) && owner.status === 'unknown' && owner.reason === 'zero_byte_workspace') {
        throw Object.assign(new Error('merchant_initialization_incomplete'), { context: { registry: directory, seller: sellerId, dataRoot, state: record.stateDirectory, reason: owner.reason } });
      }
      checkWorkspacePath(dataRoot);
      record.status = ownershipMatches(owner, sellerId, record.status === 'created' && samePath(dataRoot, record.initialDataRoot));
      atomicJSON(registryFile, registry);
      return { sellerId, dataRoot, stateDirectory: record.stateDirectory };
    }
    const paths = profilePaths(directory, sellerId);
    const legacyFile = path.join(directory, 'storage.json');
    let legacyConfig, legacyRoot, preferredRoot;
    if (exists(legacyFile)) { legacyConfig = readBootstrap(legacyFile); legacyRoot = legacyConfig.dataRoot; }
    if (!legacyRoot && env.LOCALAPPDATA) { const candidate = path.join(env.LOCALAPPDATA, 'AlibabaReceptionWorkbenchV2'); if (exists(candidate)) legacyRoot = candidate; }
    if (legacyRoot) {
      safePath(legacyRoot, true);
      const owner = await inspectOwnership(legacyRoot, options);
      if (owner.status === 'owned' && owner.sellerId === sellerId) preferredRoot = legacyRoot;
      else if (!['owned', 'empty'].includes(owner.status)) throw new Error('merchant_legacy_ownership_unknown');
    }
    let dataRoot = paths.dataRoot, status = 'created';
    // Recover only our own prepared registration, never overwrite unknown files.
    const intentFile = path.join(paths.stateDirectory, 'profile.json');
    if (exists(paths.stateDirectory) && readdirSync(safePath(paths.stateDirectory, true)).length) {
      if (!exists(intentFile)) throw new Error('merchant_registration_conflict');
      const intent = readJSON(intentFile, 65536);
      if (!exactKeys(intent, ['format', 'sellerId', 'dataRoot', 'stateDirectory']) || intent.format !== 1 || intent.sellerId !== sellerId || !samePath(safePath(intent.stateDirectory, true), paths.stateDirectory)) throw new Error('merchant_registration_conflict');
      dataRoot = safePath(intent.dataRoot, true);
      const saved = readBootstrap(path.join(paths.stateDirectory, 'storage.json'));
      if (saved.dataRoot && !samePath(safePath(saved.dataRoot, true), dataRoot)) throw new Error('merchant_registration_conflict');
      status = ownershipMatches(await inspectOwnership(dataRoot, options), sellerId, true);
    } else if (options.dataRoot) {
      dataRoot = safePath(options.dataRoot);
      if (exists(dataRoot)) status = ownershipMatches(await inspectOwnership(dataRoot, options), sellerId, true);
    } else if (preferredRoot) { dataRoot = preferredRoot; status = 'verified'; }
    if (preferredRoot && !samePath(dataRoot, preferredRoot)) throw new Error('merchant_profile_root_conflict');
    if (options.dataRoot && !samePath(safePath(options.dataRoot), dataRoot)) throw new Error('merchant_profile_root_conflict');
    validateSeparation(directory, sellerId, dataRoot, registry, options.programDirectory);
    // Keep extra room for newly selected defaults; explicit and existing roots use the hard limit.
    checkWorkspacePath(dataRoot, !options.dataRoot && !preferredRoot && !exists(intentFile) ? 240 : 260);
    if (!options.dataRoot && !preferredRoot && !exists(intentFile) && exists(dataRoot)) throw new Error('merchant_registration_conflict');
    if (exists(dataRoot)) {
      const owner = await inspectOwnership(dataRoot, options);
      status = ownershipMatches(owner, sellerId, status === 'created');
    } else ensureDirectory(dataRoot);
    ensureDirectory(paths.stateDirectory);
    const intent = { format: 1, sellerId, dataRoot, stateDirectory: paths.stateDirectory };
    if (!exists(intentFile)) atomicJSON(intentFile, intent);
    const configFile = path.join(paths.stateDirectory, 'storage.json');
    if (!exists(configFile)) atomicJSON(configFile, { ...(legacyConfig && preferredRoot && samePath(dataRoot, preferredRoot) ? legacyConfig : { version: 1 }), dataRoot });
    else if (!samePath(safePath(readBootstrap(configFile).dataRoot, true), dataRoot)) throw new Error('merchant_registration_conflict');
    record = { sellerId, stateDirectory: paths.stateDirectory, initialDataRoot: dataRoot, status };
    registry.profiles[sellerId] = record; atomicJSON(registryFile, registry);
    return { sellerId, dataRoot, stateDirectory: paths.stateDirectory };
  } finally { await release(); }
}
function option(args, name) {
  const positions = args.flatMap((arg, index) => arg === name ? [index] : []);
  if (positions.length !== 1 || typeof args[positions[0] + 1] !== 'string' || args[positions[0] + 1].startsWith('--')) throw new Error('auto_launch_context_invalid');
  return args[positions[0] + 1];
}
export function consumeAutoLaunchContext(env, args) {
  const raw = env[AUTO_LAUNCH_CONTEXT_ENV]; delete env[AUTO_LAUNCH_CONTEXT_ENV];
  if (raw === undefined) return null;
  try {
    if (!args.includes('--connect-from-accio') || typeof raw !== 'string' || Buffer.byteLength(raw, 'utf8') > 100000) throw new Error();
    const value = JSON.parse(raw);
    if (!exactKeys(value, ['sellerId', 'dataRoot', 'stateDirectory']) || typeof value.sellerId !== 'string' || !ID.test(value.sellerId)) throw new Error();
    const dataRoot = safePath(value.dataRoot, true), stateDirectory = safePath(value.stateDirectory, true);
    if (overlaps(dataRoot, stateDirectory) || option(args, '--expect-seller') !== value.sellerId || !samePath(safePath(option(args, '--data-dir'), true), dataRoot) || !samePath(safePath(env.RECEPTION_BOOTSTRAP_DIR, true), stateDirectory)) throw new Error();
    return { sellerId: value.sellerId, dataRoot, stateDirectory };
  } catch { throw new Error('auto_launch_context_invalid'); }
}
