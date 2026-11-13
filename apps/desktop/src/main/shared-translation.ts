import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { promisify } from 'node:util';
// @ts-ignore The existing Windows mutex helper is JavaScript without a declaration.
import { acquireMerchantRegistryMutex } from '../../../../scripts/merchant-mutex.mjs';
import { normalizeTranslationBaseUrl } from '../shared/translation-endpoint';
import { type CredentialEncryption, TranslationCredentialStore } from './translation-credentials';

export interface SharedTranslationConnection {
  revision: number;
  base_url: string;
  model: string;
  api_key: string;
  credential_ref: string | null;
}

const SCOPE = 'reception.shared.translation.v1';
const REF = createHash('sha256').update(SCOPE).digest('hex');
const MAX_REVISION = 2_147_483_647;
const MAX_PAYLOAD_BYTES = 16 * 1024;
const CONFLICT = '共享翻译配置已变化，请保留输入并重新读取。';
const INVALID = '共享翻译配置无效。';
const UNAVAILABLE = '无法读取共享翻译配置。';

type StoredConnection = {
  scope: typeof SCOPE;
  revision: number;
  base_url: string;
  model: string;
  api_key: string;
};

function baseUrl(value: unknown): string {
  if (typeof value !== 'string' || Buffer.byteLength(value, 'utf8') > 4096) throw new Error(INVALID);
  try { return normalizeTranslationBaseUrl(value); } catch { throw new Error(INVALID); }
}

function modelName(value: unknown): string {
  if (typeof value !== 'string') throw new Error(INVALID);
  const trimmed = value.trim();
  if (!trimmed || value.length > 200 || /[\x00-\x1f\x7f]/.test(value)) throw new Error(INVALID);
  return trimmed;
}

function apiKey(value: unknown, allowEmpty = false): string {
  if (typeof value !== 'string' || /[\0\r\n]/.test(value)) throw new Error(INVALID);
  const trimmed = value.trim();
  if (!allowEmpty && !trimmed || Buffer.byteLength(trimmed, 'utf8') > 7000) throw new Error(INVALID);
  return trimmed;
}

function parseStored(value: string): StoredConnection {
  try {
    const record: unknown = JSON.parse(value);
    if (!record || typeof record !== 'object' || Array.isArray(record)) throw new Error();
    const item = record as Record<string, unknown>;
    if (Object.keys(item).sort().join(',') !== 'api_key,base_url,model,revision,scope' || item.scope !== SCOPE ||
      !Number.isSafeInteger(item.revision) || (item.revision as number) < 1 || (item.revision as number) > MAX_REVISION ||
      item.base_url !== baseUrl(item.base_url) || item.model !== modelName(item.model) || item.api_key !== apiKey(item.api_key, true)) throw new Error();
    return item as StoredConnection;
  } catch { throw new Error(UNAVAILABLE); }
}

function present(record: StoredConnection): SharedTranslationConnection {
  return { revision: record.revision, base_url: record.base_url, model: record.model, api_key: record.api_key,
    credential_ref: record.api_key ? REF : null };
}

function safeDirectory(value: string): string {
  if (!path.isAbsolute(value) || /^(?:\\\\|\/\/)/.test(value) || /[\x00-\x1f\x7f]/.test(value)) throw new Error('共享翻译配置目录不可用。');
  return path.resolve(value);
}

/** The override is for isolated tests; production derives the real Windows known folder. */
export async function resolveSharedTranslationDirectory(mode: 'demo' | 'live'): Promise<string> {
  if (mode !== 'demo' && mode !== 'live') throw new Error('共享翻译配置目录不可用。');
  if (process.env.RECEPTION_SHARED_SETTINGS_DIR !== undefined) {
    return path.join(safeDirectory(process.env.RECEPTION_SHARED_SETTINGS_DIR), mode);
  }
  if (process.platform !== 'win32') throw new Error('共享翻译配置目录不可用。');
  try {
    const program = "$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);[Console]::Out.Write([Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData))";
    const powershell = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe');
    const { stdout } = await promisify(execFile)(powershell, ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(program, 'utf16le').toString('base64')],
      { windowsHide: true, timeout: 10000, maxBuffer: 4096, encoding: 'utf8' });
    const localAppData = safeDirectory(stdout);
    return path.join(localAppData, 'AlibabaReceptionWorkbenchShared', 'translation', mode);
  } catch { throw new Error('共享翻译配置目录不可用。'); }
}

export class SharedTranslationStore {
  private readonly credentials: TranslationCredentialStore;

  constructor(directory: string, private readonly encryption: CredentialEncryption, private readonly acquireLock: () => Promise<() => Promise<void>>) {
    this.credentials = new TranslationCredentialStore(safeDirectory(directory), encryption, MAX_PAYLOAD_BYTES);
  }

  async read(): Promise<SharedTranslationConnection | null> {
    try { if (!this.encryption.isEncryptionAvailable()) throw new Error(); }
    catch { throw new Error(UNAVAILABLE); }
    const raw = await this.credentials.get(REF);
    return raw === null ? null : present(parseStored(raw));
  }

  async save(input: {revision: number; base_url: string; model: string; api_key?: string; clear_key?: boolean}): Promise<SharedTranslationConnection> {
    if (!input || !Number.isSafeInteger(input.revision) || input.revision < 0 || input.revision >= MAX_REVISION ||
      input.clear_key === true && input.api_key !== undefined || input.clear_key !== undefined && typeof input.clear_key !== 'boolean') throw new Error(INVALID);
    const endpoint = baseUrl(input.base_url);
    const model = modelName(input.model);
    const newKey = input.api_key === undefined ? undefined : apiKey(input.api_key);
    const release = await this.acquireLock();
    try {
      const previous = await this.read();
      if (input.revision !== (previous?.revision ?? 0)) throw new Error(CONFLICT);
      const record: StoredConnection = { scope: SCOPE, revision: input.revision + 1, base_url: endpoint, model,
        api_key: input.clear_key ? '' : newKey ?? previous?.api_key ?? '' };
      await this.credentials.replace(REF, JSON.stringify(record));
      return present(record);
    } finally { await release(); }
  }

  async importIfEmpty(input: {base_url: string; model: string; api_key: string}): Promise<SharedTranslationConnection> {
    const release = await this.acquireLock();
    try {
      const previous = await this.read();
      if (previous) return previous;
      const record: StoredConnection = { scope: SCOPE, revision: 1, base_url: baseUrl(input.base_url),
        model: modelName(input.model), api_key: apiKey(input.api_key) };
      await this.credentials.replace(REF, JSON.stringify(record));
      return present(record);
    } finally { await release(); }
  }
}

export async function createSharedTranslationStore(encryption: CredentialEncryption, mode: 'demo' | 'live'): Promise<SharedTranslationStore> {
  const directory = await resolveSharedTranslationDirectory(mode);
  return new SharedTranslationStore(directory, encryption, () => acquireMerchantRegistryMutex(directory, 10000));
}
