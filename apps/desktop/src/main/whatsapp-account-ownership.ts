import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { lstat, mkdir, open, rename, unlink } from 'node:fs/promises';
import { resolveWindowsUserLocalAppData } from './windows-user-directories';

export type OwnershipLock = (key: string) => Promise<() => Promise<void>>;

/** Only a device suffix on the same SDK identity is removed; PN and LID remain distinct. */
export function accountKey(accountId: string): string {
  if (!/^\d+(?::\d+)?@(c\.us|lid)$/.test(accountId)) throw new Error('WhatsApp 账号标识无效。');
  return accountId.replace(/:\d+(?=@)/, '');
}
export function accountDigest(accountId: string): string {
  return createHash('sha256').update(accountId).digest('hex');
}
export async function ownershipDirectory(): Promise<string> {
  return path.join(await resolveWindowsUserLocalAppData(), 'AlibabaReceptionWorkbenchHost', 'whatsapp-ownership');
}
async function prepare(directory: string): Promise<void> {
  if (!path.isAbsolute(directory) || path.resolve(directory) !== directory || /^(?:\\\\|\/\/)/.test(directory)) throw new Error('whatsapp_ownership_path_invalid');
  let current = path.parse(directory).root;
  for (const part of directory.slice(current.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    try { await mkdir(current, { mode: 0o700 }); } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error; }
    const info = await lstat(current);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('whatsapp_ownership_path_invalid');
  }
}

/** The permanent identity mapping and its process lifetime lock share a user-level authority. */
export class WhatsAppAccountOwnership {
  constructor(private readonly root: string, private readonly lock: OwnershipLock) {}
  async acquire(accountId: string, workspaceId: string, ledgerRoot: string): Promise<() => Promise<void>> {
    return this.acquireDigest(accountDigest(accountKey(accountId)), workspaceId, ledgerRoot);
  }
  async acquireDigest(digest: string, workspaceId: string, ledgerRoot: string): Promise<() => Promise<void>> {
    if (!/^[a-f0-9]{64}$/.test(digest)) throw new Error('whatsapp_ownership_invalid');
    await prepare(this.root);
    const releaseIdentity = await this.lock(path.join(this.root, `account-${digest}`));
    try {
      const releaseMetadata = await this.lock(path.join(this.root, 'metadata-lock'));
      try {
        const file = path.join(this.root, `${digest}.json`);
        const canonicalRoot = process.platform === 'win32' ? path.resolve(ledgerRoot).toLowerCase() : path.resolve(ledgerRoot);
        const expected = { format: 'wa-owner-v1', workspace_id: workspaceId, ledger_root: canonicalRoot };
        try {
          const info = await lstat(file);
          if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size > 8192) throw new Error('whatsapp_ownership_invalid');
          const handle = await open(file, 'r');
          let data: string;
          try { const actual = await handle.stat(); if (actual.ino !== info.ino || actual.dev !== info.dev || actual.nlink !== 1 || actual.size > 8192) throw new Error('whatsapp_ownership_invalid'); data = await handle.readFile('utf8'); } finally { await handle.close(); }
          const value = JSON.parse(data);
          if (value.format !== expected.format || value.workspace_id !== workspaceId || value.ledger_root !== canonicalRoot || Object.keys(value).sort().join(',') !== 'format,ledger_root,workspace_id') throw new Error('此 WhatsApp 账号已有保存的环境，请选择原环境以保留消息和发送记录。');
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
          const temp = path.join(this.root, `.${randomUUID()}.tmp`);
          try { const handle = await open(temp, 'wx', 0o600); try { await handle.writeFile(JSON.stringify(expected)); await handle.sync(); } finally { await handle.close(); } await rename(temp, file); }
          finally { await unlink(temp).catch(() => {}); }
        }
      } finally { await releaseMetadata(); }
      return releaseIdentity;
    } catch (error) { await releaseIdentity(); throw error; }
  }
}
