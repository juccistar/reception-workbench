import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { lstat, mkdir, open, readFile, rename, unlink } from 'node:fs/promises';
import { resolveModelSettingsDirectory } from './model-settings-store';
// @ts-ignore Existing Windows mutex helper.
import { acquireMerchantRegistryMutex } from '../../../../scripts/merchant-mutex.mjs';

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
  if (!item || !/^[a-f0-9-]{36}$/.test(item.record_id) || !/^[1-9][0-9]{0,127}$/.test(item.seller_id) || typeof item.name !== 'string' || !item.name.trim() || item.name.length > 200 || /[\x00-\x1f]/.test(item.name) || !validPath(item.registry_directory) || !validPath(item.state_directory) || !validPath(item.data_root) || !Number.isSafeInteger(item.verified_at) || item.verified_at < 1 || Object.keys(item).sort().join(',') !== 'data_root,name,record_id,registry_directory,seller_id,state_directory,verified_at') throw new Error('launcher_registry_invalid');
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
    await checkParents(this.directory); await mkdir(this.directory, { recursive: true, mode: 0o700 }); await checkParents(this.directory); const st = await lstat(this.directory); if (!st.isDirectory() || st.isSymbolicLink()) throw new Error('launcher_registry_invalid');
    const release = await this.acquireLock();
    try { const merchants = await this.list(); const old = merchants.find(m => m.seller_id === input.seller_id && m.registry_directory === input.registry_directory);
      const item: LauncherMerchant = { ...input, record_id: old?.record_id ?? randomUUID() }; validate(item);
      const next = [...merchants.filter(m => m.record_id !== item.record_id), item]; if (next.length > 512) throw new Error('launcher_registry_invalid');
      const bytes = Buffer.from(JSON.stringify({ format: 'launcher-registry-v1', merchants: next })); if (bytes.length > 1024 * 1024) throw new Error('launcher_registry_invalid');
      const temp = path.join(this.directory, `.merchant-${randomUUID()}.tmp`);
      try { const f = await open(temp, 'wx', 0o600); try { await f.writeFile(bytes); await f.sync(); } finally { await f.close(); } await rename(temp, this.file); } finally { await unlink(temp).catch(() => {}); }
      return structuredClone(item);
    } finally { await release(); }
  }
}
export async function createLauncherRegistry(): Promise<LauncherRegistry> {
  const base = await resolveModelSettingsDirectory(); const directory = path.join(path.dirname(base), 'launcher');
  return new LauncherRegistry(directory, () => acquireMerchantRegistryMutex(directory, 10000));
}
