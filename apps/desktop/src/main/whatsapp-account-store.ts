import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { lstat, mkdir, open, rename, unlink } from 'node:fs/promises';
import { resolveModelSettingsDirectory } from './model-settings-store';
import { accountDigest, accountKey, ownershipDirectory, WhatsAppAccountOwnership } from './whatsapp-account-ownership';
// @ts-ignore Existing Windows kernel mutex helper.
import { acquireMerchantRegistryMutex } from '../../../../scripts/merchant-mutex.mjs';

export interface WhatsAppAccountEntry { id: string; label: string; account_tail: string; revision: number }
type DiskEntry = { id: string; label: string; account_tail: string; account_digest?: string; revision?: number; identity_version?: 1 };
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
  private readonly ownershipRoot: string | undefined;
  private readonly holders = new Map<string, { identity?: string; identityLocks: Map<string, () => Promise<void>> }>();
  constructor(root: string, lock: Lock = key => acquireMerchantRegistryMutex(key, /[\\/]metadata-lock$/.test(key) ? 10000 : 1000), ownershipRoot?: string) {
    if (!path.isAbsolute(root) || path.resolve(root) !== root || root === path.parse(root).root || /^(?:\\\\|\/\/)/.test(root) || /[\x00-\x1f]/.test(root)) throw new Error('whatsapp_account_path_invalid');
    this.metadata = path.join(root, 'ARWData', 'whatsapp');
    this.profiles = path.join(root, 'ARWBrowser', 'wa');
    this.data = path.join(root, 'ARWData', 'whatsapp', 'workspaces');
    this.lock = lock;
    this.ownershipRoot = ownershipRoot;
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
        if (!entry || Object.keys(entry).some(key => !['id', 'label', 'account_tail', 'account_digest', 'revision', 'identity_version'].includes(key)) || (entry.identity_version !== undefined && (entry.identity_version !== 1 || !entry.account_digest)) || (entry.revision !== undefined && (!Number.isSafeInteger(entry.revision) || entry.revision < 0)) || (entry.account_digest !== undefined && !/^[a-f0-9]{64}$/.test(entry.account_digest)) || !validId(entry.id) || !validLabel(entry.label) || !validTail(entry.account_tail) || ids.has(entry.id)) throw new Error('invalid');
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
    return index.accounts.map(entry => ({ id: entry.id, label: entry.label, account_tail: entry.account_tail, revision: entry.revision ?? 0 }));
  }
  async create(label: string): Promise<WhatsAppAccountEntry> {
    if (!validLabel(label)) throw new Error('whatsapp_account_label_invalid');
    await this.prepare(this.metadata);
    const release = await this.lock(path.join(this.metadata, 'metadata-lock'));
    try {
      const index = await this.read();
      const entry = { id: randomUUID(), label: label.trim(), account_tail: '', revision: 0 };
      index.accounts.push(entry);
      await this.write(index);
      return entry;
    } finally { await release(); }
  }
  async rename(workspaceId: string, label: string, expectedRevision: number): Promise<WhatsAppAccountEntry> {
    if (!validId(workspaceId)) throw new Error('whatsapp_account_invalid');
    if (!validLabel(label) || !Number.isSafeInteger(expectedRevision) || expectedRevision < 0) throw new Error('whatsapp_account_label_invalid');
    await this.prepare(this.metadata);
    const release = await this.lock(path.join(this.metadata, 'metadata-lock'));
    try {
      const index = await this.read();
      const entry = index.accounts.find(item => item.id === workspaceId);
      if (!entry) throw new Error('whatsapp_account_invalid');
      if ((entry.revision ?? 0) !== expectedRevision) throw new Error('whatsapp_account_revision_conflict');
      entry.label = label.trim();
      entry.revision = expectedRevision + 1;
      await this.write(index);
      return { id: entry.id, label: entry.label, account_tail: entry.account_tail, revision: entry.revision };
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
    const holder = this.holders.get(workspaceId);
    if (!holder) throw new Error('账号环境未持有运行锁。');
    const key = accountKey(accountId);
    if (holder.identity && holder.identity !== key) throw new Error('当前登录账号与此环境原账号不同，请使用相应的原账号环境。');
    if (holder.identity === key) return;
    await this.prepare(this.metadata);
    const existing = await this.read();
    const existingItem = existing.accounts.find(entry => entry.id === workspaceId);
    const canonicalDigest = accountDigest(key), originalDigest = accountDigest(accountId);
    if (!existingItem || existingItem.account_digest && existingItem.account_digest !== canonicalDigest && existingItem.account_digest !== originalDigest) throw new Error('当前登录账号与此环境原账号不同，请使用相应的原账号环境。');
    if (existing.accounts.some(entry => entry.id !== workspaceId && (entry.account_digest === canonicalDigest || entry.account_digest === originalDigest))) throw new Error('此 WhatsApp 账号已有保存的环境，请选择原环境以保留消息和发送记录。');
    if (!existingItem.account_digest && existing.accounts.some(entry => entry.id !== workspaceId && entry.account_digest && !entry.identity_version)) throw new Error('旧账号身份尚未核验，请先连接原有账号环境。');
    const ownership = new WhatsAppAccountOwnership(this.ownershipRoot ?? await ownershipDirectory(), this.lock);
    const heldIdentity = holder.identityLocks.has(canonicalDigest);
    const releaseIdentity = heldIdentity ? undefined : await ownership.acquire(accountId, workspaceId, await this.directory(workspaceId));
    try {
      const release = await this.lock(path.join(this.metadata, 'metadata-lock'));
      try {
        const index = await this.read();
        const item = index.accounts.find(entry => entry.id === workspaceId);
        const digest = accountDigest(key), rawDigest = accountDigest(accountId);
        if (!item) throw new Error('账号环境不存在。');
        if (item.account_digest && item.account_digest !== digest && item.account_digest !== rawDigest) throw new Error('当前登录账号与此环境原账号不同，请使用相应的原账号环境。');
        if (index.accounts.some(entry => entry.id !== workspaceId && (entry.account_digest === digest || entry.account_digest === rawDigest))) throw new Error('此 WhatsApp 账号已有保存的环境，请选择原环境以保留消息和发送记录。');
        if (!item.account_digest && index.accounts.some(entry => entry.id !== workspaceId && entry.account_digest && !entry.identity_version)) throw new Error('旧账号身份尚未核验，请先连接原有账号环境。');
        item.account_digest = digest;
        item.identity_version = 1;
        item.account_tail = key.split('@')[0].slice(-4);
        await this.write(index);
        holder.identity = key;
        if (releaseIdentity) holder.identityLocks.set(canonicalDigest, releaseIdentity);
      } finally { await release(); }
    } catch (error) { await releaseIdentity?.(); throw error; }
  }
  /** Holds the per-user single-consumer mutex until the browser has actually exited. */
  async acquire(workspaceId: string): Promise<() => Promise<void>> {
    if (!validId(workspaceId) || !(await this.read()).accounts.some(item => item.id === workspaceId)) throw new Error('whatsapp_account_invalid');
    await this.prepare(this.metadata);
    const release = await this.lock(path.join(this.metadata, 'active-owner', workspaceId));
    try {
      const entry = (await this.read()).accounts.find(item => item.id === workspaceId);
      if (!entry || this.holders.has(workspaceId)) throw new Error('whatsapp_account_invalid');
      await this.profileDirectory(workspaceId);
      const holder: { identity?: string; identityLocks: Map<string, () => Promise<void>> } = { identityLocks: new Map() };
      if (entry.account_digest) {
        const ownership = new WhatsAppAccountOwnership(this.ownershipRoot ?? await ownershipDirectory(), this.lock);
        const releaseIdentity = await ownership.acquireDigest(entry.account_digest, workspaceId, await this.directory(workspaceId));
        holder.identityLocks.set(entry.account_digest, releaseIdentity);
      }
      this.holders.set(workspaceId, holder);
      return async () => {
        for (const [digest, releaseIdentity] of holder.identityLocks) { await releaseIdentity(); holder.identityLocks.delete(digest); }
        await release();
        if (this.holders.get(workspaceId) === holder) this.holders.delete(workspaceId);
      };
    } catch (error) { await release(); throw error; }
  }
}

export async function createWhatsAppAccountStore(): Promise<WhatsAppAccountStore> {
  const models = await resolveModelSettingsDirectory();
  return new WhatsAppAccountStore(path.dirname(path.dirname(models)));
}
