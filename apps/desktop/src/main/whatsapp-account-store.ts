import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { lstat, mkdir, open, rename, unlink } from 'node:fs/promises';
import { resolveModelSettingsDirectory } from './model-settings-store';
// @ts-ignore Existing Windows kernel mutex helper.
import { acquireMerchantRegistryMutex } from '../../../../scripts/merchant-mutex.mjs';

export interface WhatsAppAccountEntry { id: string; label: string; account_tail: string; selected: boolean }
type DiskEntry = { id: string; label: string; account_tail: string; account_digest?: string };
type Index = { format: 'whatsapp-accounts-v1'; selected: string | null; accounts: DiskEntry[] };
type Lock = (key: string) => Promise<() => Promise<void>>;
const validId = (id: unknown): id is string => typeof id === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id);
const validLabel = (label: unknown): label is string => typeof label === 'string' && !!label.trim() && [...label].length <= 100 && !/[\x00-\x1f\x7f]/.test(label);
const validTail = (tail: unknown): tail is string => typeof tail === 'string' && /^(?:[0-9]{1,4})?$/.test(tail);

async function checkParents(directory: string): Promise<void> {
  const root = path.parse(directory).root;
  let current = root;
  for (const part of directory.slice(root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    try { const entry = await lstat(current); if (!entry.isDirectory() || entry.isSymbolicLink()) throw new Error('whatsapp_account_path_invalid'); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return; throw error; }
  }
}

/** Routing metadata only. Browser credentials remain in the dedicated Chrome profile. */
export class WhatsAppAccountStore {
  private readonly metadata: string;
  private readonly profiles: string;
  private readonly data: string;
  private readonly lock: Lock;
  constructor(root: string, lock: Lock = key => acquireMerchantRegistryMutex(key, 1000)) {
    if (!path.isAbsolute(root) || path.resolve(root) !== root || root === path.parse(root).root || /^(?:\\\\|\/\/)/.test(root) || /[\x00-\x1f]/.test(root)) throw new Error('whatsapp_account_path_invalid');
    this.metadata = path.join(root, 'ARWData', 'whatsapp');
    this.profiles = path.join(root, 'ARWBrowser', 'wa');
    this.data = path.join(root, 'ARWData', 'whatsapp', 'workspaces');
    this.lock = lock;
  }
  private async prepare(directory: string): Promise<void> {
    await checkParents(directory);
    await mkdir(directory, { recursive: true, mode: 0o700 });
    await checkParents(directory);
  }
  private async read(): Promise<Index> {
    const empty: Index = { format: 'whatsapp-accounts-v1', selected: null, accounts: [] };
    await checkParents(this.metadata);
    const file = path.join(this.metadata, 'accounts.json');
    try {
      const before = await lstat(file);
      if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1 || before.size > 128 * 1024) throw new Error('invalid');
      const handle = await open(file, 'r');
      let bytes: Buffer;
      try {
        const actual = await handle.stat();
        if (!actual.isFile() || actual.nlink !== 1 || actual.size > 128 * 1024 || actual.ino !== before.ino || actual.dev !== before.dev) throw new Error('invalid');
        bytes = await handle.readFile();
      } finally { await handle.close(); }
      if (bytes.length > 128 * 1024) throw new Error('invalid');
      const value = JSON.parse(bytes.toString('utf8')) as Index;
      if (!value || Object.keys(value).sort().join(',') !== 'accounts,format,selected' || value.format !== 'whatsapp-accounts-v1' || !Array.isArray(value.accounts) || value.accounts.length > 512 || (value.selected !== null && !validId(value.selected))) throw new Error('invalid');
      const ids = new Set<string>();
      for (const entry of value.accounts) {
        if (!entry || Object.keys(entry).sort().join(',') !== (entry.account_digest === undefined ? 'account_tail,id,label' : 'account_digest,account_tail,id,label') || (entry.account_digest !== undefined && !/^[a-f0-9]{64}$/.test(entry.account_digest)) || !validId(entry.id) || !validLabel(entry.label) || !validTail(entry.account_tail) || ids.has(entry.id)) throw new Error('invalid');
        ids.add(entry.id);
      }
      if (value.selected !== null && !ids.has(value.selected)) throw new Error('invalid');
      return value;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return empty;
      throw new Error('whatsapp_accounts_unavailable');
    }
  }
  private async write(index: Index): Promise<void> {
    const bytes = Buffer.from(JSON.stringify(index));
    if (bytes.length > 128 * 1024 || index.accounts.length > 512) throw new Error('whatsapp_accounts_limit');
    const temp = path.join(this.metadata, `.accounts-${randomUUID()}.tmp`);
    try {
      const handle = await open(temp, 'wx', 0o600);
      try { await handle.writeFile(bytes); await handle.sync(); } finally { await handle.close(); }
      await rename(temp, path.join(this.metadata, 'accounts.json'));
    } finally { await unlink(temp).catch(() => {}); }
  }
  async list(): Promise<WhatsAppAccountEntry[]> {
    const index = await this.read();
    return index.accounts.map(entry => ({ id: entry.id, label: entry.label, account_tail: entry.account_tail, selected: entry.id === index.selected }));
  }
  async selected(): Promise<WhatsAppAccountEntry | null> { return (await this.list()).find(entry => entry.selected) ?? null; }
  async create(label: string): Promise<WhatsAppAccountEntry> {
    if (!validLabel(label)) throw new Error('whatsapp_account_label_invalid');
    await this.prepare(this.metadata);
    const release = await this.lock(path.join(this.metadata, 'metadata-lock'));
    try {
      const index = await this.read();
      const entry: DiskEntry = { id: randomUUID(), label: label.trim(), account_tail: '' };
      index.accounts.push(entry);
      await this.write(index);
      return { ...entry, selected: false };
    } finally { await release(); }
  }
  async select(workspaceId: string): Promise<WhatsAppAccountEntry> {
    if (!validId(workspaceId)) throw new Error('whatsapp_account_invalid');
    await this.prepare(this.metadata);
    const release = await this.lock(path.join(this.metadata, 'metadata-lock'));
    try {
      const index = await this.read();
      const entry = index.accounts.find(item => item.id === workspaceId);
      if (!entry) throw new Error('whatsapp_account_invalid');
      index.selected = workspaceId;
      await this.write(index);
      return { ...entry, selected: true };
    } finally { await release(); }
  }
  async directory(workspaceId: string): Promise<string> {
    if (!validId(workspaceId) || !(await this.read()).accounts.some(item => item.id === workspaceId)) throw new Error('whatsapp_account_invalid');
    const directory = path.join(this.data, workspaceId);
    await checkParents(directory);
    return directory;
  }
  async profileDirectory(workspaceId: string): Promise<string> {
    if (!validId(workspaceId) || !(await this.read()).accounts.some(item => item.id === workspaceId)) throw new Error('whatsapp_account_invalid');
    const directory = path.join(this.profiles, workspaceId.replaceAll('-', ''));
    await checkParents(directory);
    return directory;
  }
  /** Bind a verified browser identity once; a second workspace cannot bypass its ledger. */
  async bindAccount(workspaceId: string, accountId: string): Promise<void> {
    if (!validId(workspaceId) || typeof accountId !== 'string' || !/^\d+(?::\d+)?@(c\.us|lid)$/.test(accountId)) throw new Error('WhatsApp 账号标识无效。');
    await this.prepare(this.metadata);
    const release = await this.lock(path.join(this.metadata, 'metadata-lock'));
    try {
      const index = await this.read();
      const item = index.accounts.find(entry => entry.id === workspaceId);
      const digest = createHash('sha256').update(accountId).digest('hex');
      if (!item) throw new Error('账号环境不存在。');
      if (item.account_digest && item.account_digest !== digest) throw new Error('当前登录账号与此环境原账号不同，请使用相应的原账号环境。');
      if (index.accounts.some(entry => entry.id !== workspaceId && entry.account_digest === digest)) throw new Error('此 WhatsApp 账号已有保存的环境，请选择原环境以保留消息和发送记录。');
      item.account_digest = digest;
      item.account_tail = accountId.split('@')[0].split(':')[0].slice(-4);
      await this.write(index);
    } finally { await release(); }
  }
  /** Holds the per-user single-consumer mutex until the browser has actually exited. */
  async acquire(workspaceId: string): Promise<() => Promise<void>> {
    if (!validId(workspaceId) || !(await this.read()).accounts.some(item => item.id === workspaceId)) throw new Error('whatsapp_account_invalid');
    await this.prepare(this.metadata);
    const release = await this.lock(path.join(this.metadata, 'active-owner'));
    try {
      if (!(await this.read()).accounts.some(item => item.id === workspaceId)) throw new Error('whatsapp_account_invalid');
      await this.profileDirectory(workspaceId);
      return release;
    } catch (error) { await release(); throw error; }
  }
}

export async function createWhatsAppAccountStore(): Promise<WhatsAppAccountStore> {
  const models = await resolveModelSettingsDirectory();
  return new WhatsAppAccountStore(path.dirname(path.dirname(models)));
}
