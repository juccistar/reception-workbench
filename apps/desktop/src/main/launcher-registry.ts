import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { lstat, mkdir, open, readFile, rename, unlink } from 'node:fs/promises';
import { resolveModelSettingsDirectory } from './model-settings-store';
import { companyName } from './launcher-company-name';
// @ts-ignore Existing Windows mutex helper.
import { acquireMerchantRegistryMutex } from '../../../../scripts/merchant-mutex.mjs';
// @ts-ignore Existing Windows path budget helper.
import { workspacePathUnits } from '../../../../scripts/merchant-profiles.mjs';

/** Routing metadata only; it never grants authority or contains an Accio token. */
export interface LauncherMerchant {
  record_id: string; seller_id: string; name: string; registry_directory: string;
  state_directory: string; data_root: string; verified_at: number;
}
type Index = { format: 'launcher-registry-v1'; merchants: LauncherMerchant[] };
async function checkParents(directory: string): Promise<void> {
  const root = path.parse(directory).root;
  let current = root;
  for (const part of directory.slice(root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    try { const entry = await lstat(current); if (!entry.isDirectory() || entry.isSymbolicLink()) throw new Error('launcher_registry_invalid'); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return; throw error; }
  }
}
const validPath = (v: unknown): v is string => typeof v === 'string' && path.isAbsolute(v) && path.resolve(v) === v && !/^(?:\\\\|\/\/)/.test(v) && !/[\x00-\x1f]/.test(v) && v !== path.parse(v).root;
function validate(item: LauncherMerchant): void {
  if (!item || !/^[a-f0-9-]{36}$/.test(item.record_id) || !/^[1-9][0-9]{0,127}$/.test(item.seller_id) || typeof item.name !== 'string' || !item.name.trim() || [...item.name].length > 400 || /[\x00-\x1f]/.test(item.name) || !validPath(item.registry_directory) || !validPath(item.state_directory) || !validPath(item.data_root) || !Number.isSafeInteger(item.verified_at) || item.verified_at < 0 || Object.keys(item).sort().join(',') !== 'data_root,name,record_id,registry_directory,seller_id,state_directory,verified_at') throw new Error('launcher_registry_invalid');
}
export class LauncherRegistry {
  private readonly file: string;
  constructor(private readonly directory: string, private readonly acquireLock: () => Promise<() => Promise<void>>) { if (!validPath(directory)) throw new Error('launcher_registry_invalid'); this.file = path.join(directory, 'merchants.json'); }
  async list(): Promise<LauncherMerchant[]> {
    try { await checkParents(this.directory); const st = await lstat(this.file); if (!st.isFile() || st.isSymbolicLink() || st.nlink !== 1 || st.size > 1024 * 1024) throw new Error();
      const bytes = await readFile(this.file); if (bytes.length > 1024 * 1024) throw new Error(); const data = JSON.parse(bytes.toString('utf8')) as Index;
      if (data.format !== 'launcher-registry-v1' || !Array.isArray(data.merchants) || data.merchants.length > 512) throw new Error();
      const ids = new Set<string>(); for (const item of data.merchants) { validate(item); if (ids.has(item.record_id)) throw new Error(); ids.add(item.record_id); }
      return structuredClone(data.merchants);
    } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return []; throw new Error('launcher_registry_unavailable'); }
  }
  /** Call only after native identity and registry/data-root ownership verification. */
  async registerVerified(input: Omit<LauncherMerchant, 'record_id'>): Promise<LauncherMerchant> {
    if (!Number.isSafeInteger(input.verified_at) || input.verified_at < 1) throw new Error('launcher_registry_invalid');
    return this.save(input, false);
  }
  /** Store an untrusted historical routing hint without replacing a verified record. */
  async importHistorical(item: LauncherMerchant): Promise<void> {
    if (item.verified_at !== 0) throw new Error('launcher_registry_invalid');
    validate(item);
    await this.save(item, true);
  }
  private async save(input: Omit<LauncherMerchant, 'record_id'>, historical: boolean): Promise<LauncherMerchant> {
    await checkParents(this.directory); await mkdir(this.directory, { recursive: true, mode: 0o700 }); await checkParents(this.directory); const st = await lstat(this.directory); if (!st.isDirectory() || st.isSymbolicLink()) throw new Error('launcher_registry_invalid');
    const release = await this.acquireLock();
    try { const merchants = await this.list(); const old = merchants.find(m => m.seller_id === input.seller_id && m.registry_directory === input.registry_directory);
      if (historical && old && old.verified_at > 0) return old;
      const sameRoot = old && old.data_root === input.data_root && old.state_directory === input.state_directory;
      const name = !historical && sameRoot && !companyName(input.name, input.seller_id) && companyName(old.name, old.seller_id) || input.name;
      const item: LauncherMerchant = { ...input, name, record_id: old?.record_id ?? (historical ? (input as LauncherMerchant).record_id : randomUUID()) }; validate(item);
      await this.write([...merchants.filter(m => m.record_id !== item.record_id), item]);
      return structuredClone(item);
    } finally { await release(); }
  }
  /** Update only the exact verified registration; never renew its verification. */
  async updateCompanyName(expected: LauncherMerchant, value: unknown): Promise<boolean> {
    validate(expected);
    const name = companyName(value, expected.seller_id);
    if (!name || expected.verified_at < 1) return false;
    const release = await this.acquireLock();
    try {
      const merchants = await this.list();
      const item = merchants.find(m => m.record_id === expected.record_id);
      if (!item || item.seller_id !== expected.seller_id || item.registry_directory !== expected.registry_directory || item.state_directory !== expected.state_directory || item.data_root !== expected.data_root || item.verified_at !== expected.verified_at) return false;
      if (item.name !== name) { item.name = name; await this.write(merchants); }
      return true;
    } finally { await release(); }
  }
  private async write(merchants: LauncherMerchant[]): Promise<void> {
    if (merchants.length > 512) throw new Error('launcher_registry_invalid');
    const bytes = Buffer.from(JSON.stringify({ format: 'launcher-registry-v1', merchants })); if (bytes.length > 1024 * 1024) throw new Error('launcher_registry_invalid');
    const temp = path.join(this.directory, `.merchant-${randomUUID()}.tmp`);
    try { const f = await open(temp, 'wx', 0o600); try { await f.writeFile(bytes); await f.sync(); } finally { await f.close(); } await rename(temp, this.file); } finally { await unlink(temp).catch(() => {}); }
  }
}
// Historical profiles are routing hints, not proof of a current authorization.
export async function readHistoricalMerchants(directory: string): Promise<LauncherMerchant[]> {
  if (!validPath(directory)) return [];
  const read = async (file: string, limit: number) => {
    await checkParents(path.dirname(file));
    const st = await lstat(file);
    if (!st.isFile() || st.isSymbolicLink() || st.nlink !== 1 || st.size > limit) throw new Error('invalid');
    const f = await open(file, 'r');
    try {
      const actual = await f.stat();
      if (!actual.isFile() || actual.nlink !== 1 || actual.size > limit || actual.ino !== st.ino || actual.dev !== st.dev) throw new Error('invalid');
      const bytes = await f.readFile(); if (bytes.length > limit) throw new Error('invalid');
      return JSON.parse(bytes.toString('utf8'));
    } finally { await f.close(); }
  };
  try {
    const registry = await read(path.join(directory, 'merchants.json'), 1024 * 1024);
    if (!registry || registry.format !== 1 || Object.keys(registry).sort().join(',') !== 'format,profiles' || !registry.profiles || typeof registry.profiles !== 'object' || Array.isArray(registry.profiles) || Object.keys(registry.profiles).length > 512) return [];
    const result: LauncherMerchant[] = [];
    for (const [seller, raw] of Object.entries(registry.profiles)) {
      try {
        const record = raw as Record<string, unknown>;
        const state = path.join(directory, 'profiles', createHash('sha256').update(seller).digest('hex'), 'state');
        const same = (a: unknown, b: string) => typeof a === 'string' && (process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b);
        if (!/^[1-9][0-9]{0,127}$/.test(seller) || !record || Object.keys(record).sort().join(',') !== 'initialDataRoot,sellerId,stateDirectory,status' || record.sellerId !== seller || !['created', 'verified'].includes(String(record.status)) || !same(record.stateDirectory, state) || !validPath(record.initialDataRoot)) continue;
        const storage = await read(path.join(state, 'storage.json'), 65536);
        if (!storage || storage.version !== 1 || Object.keys(storage).some(k => !['version', 'dataRoot', 'previousRoot', 'lastBackup', 'lastResult'].includes(k)) || !validPath(storage.dataRoot) || workspacePathUnits(storage.dataRoot) > 260) continue;
        await checkParents(storage.dataRoot);
        const root = await lstat(storage.dataRoot); if (!root.isDirectory() || root.isSymbolicLink()) continue;
        const hash = createHash('sha256').update((process.platform === 'win32' ? directory.toLowerCase() : directory) + '\0' + seller).digest('hex');
        result.push({ record_id: `${hash.slice(0, 8)}-${hash.slice(8, 12)}-${hash.slice(12, 16)}-${hash.slice(16, 20)}-${hash.slice(20, 32)}`, seller_id: seller, name: `商家 ${seller.slice(-4)}`, registry_directory: directory, state_directory: state, data_root: storage.dataRoot, verified_at: 0 });
      } catch { /* Invalid profiles do not hide other valid profiles. */ }
    }
    return result;
  } catch { return []; }
}

export async function listLauncherCandidates(registry: LauncherRegistry, directories: string[]): Promise<LauncherMerchant[]> {
  const current = await registry.list();
  const sources = [...new Set([...directories, ...current.map(item => item.registry_directory)])];
  const historical = (await Promise.all(sources.map(readHistoricalMerchants))).flat();
  const key = (item: LauncherMerchant) => `${process.platform === 'win32' ? item.registry_directory.toLowerCase() : item.registry_directory}\0${item.seller_id}`;
  const merged = new Map<string, LauncherMerchant>();
  for (const item of historical) merged.set(key(item), item);
  for (const item of current) {
    const prior = merged.get(key(item));
    // A moved root invalidates the old activation binding until a fresh verified launch.
    if (item.verified_at > 0 && (!prior || (process.platform === 'win32' ? prior.data_root.toLowerCase() === item.data_root.toLowerCase() : prior.data_root === item.data_root))) merged.set(key(item), item);
  }
  return [...merged.values()];
}

/** Automatic reception needs a complete declared scope, including malformed hints. */
export async function assertCompleteLauncherSources(registry: LauncherRegistry, directories: string[]): Promise<void> {
  const records = await registry.list();
  const key = (value: string) => process.platform === 'win32' ? value.toLowerCase() : value;
  const known = new Set(records.map(record => key(record.registry_directory)));
  const sources = new Map([...directories, ...records.map(record => record.registry_directory)].map(directory => [key(directory), directory]));
  for (const [sourceKey, directory] of sources) {
    try {
      if (!validPath(directory)) throw new Error('invalid');
      await checkParents(directory);
      const file = path.join(directory, 'merchants.json');
      let st;
      try { st = await lstat(file); }
      catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT' && !known.has(sourceKey)) continue; throw error; }
      if (!st.isFile() || st.isSymbolicLink() || st.nlink !== 1 || st.size > 1024 * 1024) throw new Error('invalid');
      const bytes = await readFile(file);
      if (bytes.length > 1024 * 1024) throw new Error('invalid');
      const data = JSON.parse(bytes.toString('utf8'));
      if (!data || data.format !== 1 || Object.keys(data).sort().join(',') !== 'format,profiles' || !data.profiles || typeof data.profiles !== 'object' || Array.isArray(data.profiles)) throw new Error('invalid');
      const rows = await readHistoricalMerchants(directory);
      if (rows.length !== Object.keys(data.profiles).length) throw new Error('incomplete');
    } catch { throw new Error('旧商家登记未能完整核查，请保留原目录并重新导入登记；自动接待继续暂停。'); }
  }
}

export async function createLauncherRegistry(): Promise<LauncherRegistry> {
  const base = await resolveModelSettingsDirectory(); const directory = path.join(path.dirname(base), 'launcher');
  return new LauncherRegistry(directory, () => acquireMerchantRegistryMutex(directory, 10000));
}

export async function historicalRegistryDirectories(): Promise<string[]> {
  const base = await resolveModelSettingsDirectory();
  const known = path.join(path.dirname(path.dirname(base)), 'AlibabaReceptionWorkbenchHost');
  const explicit = process.env.RECEPTION_MERCHANT_REGISTRY_DIR;
  const local = process.env.LOCALAPPDATA && path.isAbsolute(process.env.LOCALAPPDATA) ? path.join(process.env.LOCALAPPDATA, 'AlibabaReceptionWorkbenchHost') : '';
  return [...new Set([known, ...(local && validPath(local) ? [local] : []), ...(explicit && validPath(explicit) ? [explicit] : [])])];
}
