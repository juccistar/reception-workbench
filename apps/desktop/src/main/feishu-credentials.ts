import { createHash } from 'node:crypto';
import { TranslationCredentialStore, type CredentialEncryption } from './translation-credentials';

// Instantiate with windowsUserEncryption and a directory outside business SQLite.
// Callers serialize saves for a merchant and never expose read() to renderer IPC.
export class FeishuCredentialStore {
  private readonly store: TranslationCredentialStore;
  constructor(directory: string, encryption: CredentialEncryption) {
    this.store = new TranslationCredentialStore(directory, encryption);
  }
  private ref(merchant: string, appId: string): string {
    if (typeof merchant !== 'string' || !merchant.trim() || merchant.length > 32768 || !/^[A-Za-z0-9_-]{1,128}$/.test(appId)) throw new Error('Invalid merchant or application');
    return createHash('sha256').update('feishu-credential-v1\0').update(merchant).update('\0').update(appId).digest('hex');
  }
  async save(merchant: string, appId: string, secret: string): Promise<void> {
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(appId) || typeof secret !== 'string' || !secret.trim() || secret.length > 4096 || /[\0\r\n]/.test(secret)) throw new Error('Invalid Feishu credential');
    await this.store.replace(this.ref(merchant, appId), JSON.stringify({ appId, secret }));
  }
  async stage(merchant: string, appId: string, secret: string): Promise<string> {
    this.ref(merchant, appId);
    if (typeof secret !== 'string' || !secret.trim() || secret.length > 4096 || /[\0\r\n]/.test(secret)) throw new Error('Invalid Feishu credential');
    return this.store.put(JSON.stringify({ merchant, appId, secret }));
  }
  async read(merchant: string, appId: string, credentialRef?: string | null): Promise<string | null> {
    const value = await this.store.get(credentialRef || this.ref(merchant, appId));
    if (!value) return null;
    try {
      const record: unknown = JSON.parse(value);
      if (!record || typeof record !== 'object' || !('appId' in record) || !('secret' in record) || record.appId !== appId || typeof record.secret !== 'string' || !record.secret.trim() || credentialRef && (!('merchant' in record) || record.merchant !== merchant)) return null;
      return record.secret;
    } catch { throw new Error('Unable to read Feishu credential'); }
  }
}
