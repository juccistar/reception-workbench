import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { lstat, mkdir, open, rename, unlink } from 'node:fs/promises';
import { resolveModelSettingsDirectory } from './model-settings-store';
// @ts-ignore Existing current-user metadata mutex.
import { acquireMerchantRegistryMutex } from '../../../../scripts/merchant-mutex.mjs';

type Alias = { merchant_id: string; name: string; revision: number };
type Document = { format: 'launcher-display-v1'; merchants: Alias[] };
const merchantId = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value);
const displayName = (value: unknown): value is string => typeof value === 'string' && !!value.trim() && [...value].length <= 100 && !/[\x00-\x1f\x7f]/.test(value);
const LIMIT = 256 * 1024;
async function safeDirectory(directory: string): Promise<void> {
  if (!path.isAbsolute(directory) || path.resolve(directory) !== directory || /^(?:\\\\|\/\/)/.test(directory) || directory === path.parse(directory).root) throw new Error('账号显示设置路径无效。');
  let current = path.parse(directory).root;
  for (const component of directory.slice(current.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, component);
    try { const entry = await lstat(current); if (!entry.isDirectory() || entry.isSymbolicLink()) throw new Error('账号显示设置路径无效。'); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return; throw error; }
  }
}
/** Local labels only; verified merchant identity and registry are never rewritten. */
export class LauncherPreferences {
  private readonly file: string;
  constructor(private readonly directory: string, private readonly lock: () => Promise<() => Promise<void>>) {
    this.file = path.join(directory, 'display-names.json');
  }
  async list(): Promise<Alias[]> {
    try {
      await safeDirectory(this.directory);
      const before = await lstat(this.file);
      if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1 || before.size > LIMIT) throw new Error();
      const handle = await open(this.file, 'r');
      let data: Buffer;
      try {
        const actual = await handle.stat();
        if (!actual.isFile() || actual.nlink !== 1 || actual.ino !== before.ino || actual.dev !== before.dev || actual.size > LIMIT) throw new Error();
        data = await handle.readFile();
      } finally { await handle.close(); }
      if (data.length > LIMIT) throw new Error();
      const value = JSON.parse(data.toString('utf8')) as Document;
      if (!value || Object.keys(value).sort().join(',') !== 'format,merchants' || value.format !== 'launcher-display-v1' || !Array.isArray(value.merchants) || value.merchants.length > 512) throw new Error();
      const seen = new Set<string>();
      for (const alias of value.merchants) {
        if (!alias || Object.keys(alias).sort().join(',') !== 'merchant_id,name,revision' || !merchantId(alias.merchant_id) || !displayName(alias.name) || !Number.isSafeInteger(alias.revision) || alias.revision < 1 || seen.has(alias.merchant_id)) throw new Error();
        seen.add(alias.merchant_id);
      }
      return value.merchants;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return [];
      throw new Error('账号显示设置读取失败，请检查后重试。');
    }
  }
  async rename(input: unknown): Promise<void> {
    const row = input as Alias & { expected_revision?: number };
    if (!row || typeof row !== 'object' || Object.keys(row).sort().join(',') !== 'expected_revision,merchant_id,name' || !merchantId(row.merchant_id) || !displayName(row.name) || !Number.isSafeInteger(row.expected_revision) || row.expected_revision! < 0) throw new Error('请填写有效的账号名称。');
    await safeDirectory(this.directory);
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    await safeDirectory(this.directory);
    const release = await this.lock();
    try {
      const aliases = await this.list();
      const previous = aliases.find(alias => alias.merchant_id === row.merchant_id);
      if ((previous?.revision ?? 0) !== row.expected_revision) throw new Error('账号名称已变化，请刷新后重试。');
      const name = row.name.trim();
      if (previous?.name === name) return;
      if ((previous?.revision ?? 0) >= Number.MAX_SAFE_INTEGER - 1) throw new Error('账号名称修订不可用。');
      const next: Alias = { merchant_id: row.merchant_id, name, revision: (previous?.revision ?? 0) + 1 };
      const merchants = [...aliases.filter(alias => alias.merchant_id !== row.merchant_id), next];
      const bytes = Buffer.from(JSON.stringify({ format: 'launcher-display-v1', merchants }));
      if (merchants.length > 512 || bytes.length > LIMIT) throw new Error('账号显示设置已达上限。');
      const temporary = path.join(this.directory, `.display-${randomUUID()}.tmp`);
      try {
        const handle = await open(temporary, 'wx', 0o600);
        try { await handle.writeFile(bytes); await handle.sync(); } finally { await handle.close(); }
        await rename(temporary, this.file);
      } finally { await unlink(temporary).catch(() => {}); }
    } finally { await release(); }
  }
}
export async function createLauncherPreferences(): Promise<LauncherPreferences> {
  const directory = path.join(path.dirname(await resolveModelSettingsDirectory()), 'launcher-preferences');
  return new LauncherPreferences(directory, () => acquireMerchantRegistryMutex(directory, 10000));
}
