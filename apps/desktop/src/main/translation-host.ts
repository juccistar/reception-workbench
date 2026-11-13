import path from 'node:path';
import type { TranslationInfo, TranslationSettingsInput } from '../shared/translation';
import { normalizeTranslationBaseUrl } from '../shared/translation-endpoint';
import { TranslationCredentialStore, type CredentialEncryption } from './translation-credentials';
import { createWindowsUserEncryption } from './windows-user-encryption';
import { createSharedTranslationStore, type SharedTranslationConnection, type SharedTranslationStore } from './shared-translation';

interface HostConfig {
  enabled: boolean; base_url: string; model: string; revision: number;
  credential_ref: string | null; enabled_data_root?: string | null;
}
type BackendCall = <T>(method: string, params?: Record<string, unknown>) => Promise<T>;
interface Options {
  mode: 'demo' | 'live'; dataRoot: string; legacyDirectory: string; encryption: CredentialEncryption;
  invoke: BackendCall;
  confirm: (title: string, message: string, detail: string, action: string) => Promise<boolean>;
  canWrite: () => boolean;
  storeFactory?: () => Promise<SharedTranslationStore>;
}
const CONFLICT = '共享翻译配置已变化，请保留输入并重新读取。';
const UNAVAILABLE = '无法读取本机共享翻译配置，请检查系统加密和目录权限；已有译文仍可查看。';
const rootKey = (value: string) => process.platform === 'win32' ? path.resolve(value).toLowerCase() : path.resolve(value);

/** Shares connection credentials only; every backend retains its own enable grant and cache. */
export class TranslationHost {
  private readonly legacy: TranslationCredentialStore;
  private storePromise?: Promise<SharedTranslationStore>;
  private applied: SharedTranslationConnection | null = null;
  private queue: Promise<unknown> = Promise.resolve();
  private timer?: ReturnType<typeof setInterval>;
  private polling = false;
  private unavailable = false;

  constructor(private readonly options: Options) {
    this.legacy = new TranslationCredentialStore(options.legacyDirectory, options.encryption);
  }

  private store() {
    if (!this.storePromise) {
      this.storePromise = (this.options.storeFactory?.() ?? createSharedTranslationStore(createWindowsUserEncryption(), this.options.mode))
        .catch(error => { this.storePromise = undefined; throw error; });
    }
    return this.storePromise;
  }

  private serialize<T>(action: () => Promise<T>): Promise<T> {
    const operation = this.queue.catch(() => {}).then(action);
    this.queue = operation;
    return operation;
  }

  private async legacyKey(config: HostConfig): Promise<string> {
    if (!config.credential_ref) return '';
    try {
      const value = await this.legacy.get(config.credential_ref);
      if (!value) return '';
      const stored = JSON.parse(value) as { api_key?: unknown; data_root?: unknown };
      return typeof stored.data_root === 'string' && rootKey(stored.data_root) === rootKey(this.options.dataRoot)
        && typeof stored.api_key === 'string' ? stored.api_key : '';
    } catch { return ''; }
  }

  private async apply(connection: SharedTranslationConnection, config: HostConfig, enabled: boolean): Promise<void> {
    const bound = enabled ? path.resolve(this.options.dataRoot) : null;
    const changed = config.base_url !== connection.base_url || config.model !== connection.model
      || config.credential_ref !== connection.credential_ref || config.enabled !== enabled
      || (config.enabled_data_root ?? null) !== bound || this.applied?.revision !== connection.revision;
    if (changed) {
      await this.options.invoke('save_translation_settings', {
        enabled, base_url: connection.base_url, model: connection.model, revision: config.revision,
        credential_ref: connection.credential_ref, api_key: connection.api_key, enabled_data_root: bound,
      });
    }
    this.applied = connection;
    this.unavailable = false;
  }

  private async sync(): Promise<void> {
    const config = await this.options.invoke<HostConfig>('get_translation_config');
    const store = await this.store();
    let connection = await store.read();
    let oldKey = '';
    if (!connection || config.enabled && config.credential_ref !== connection.credential_ref && !config.enabled_data_root) {
      oldKey = await this.legacyKey(config);
    }
    if (!connection && oldKey && config.model.trim()) {
      connection = await store.importIfEmpty({ base_url: config.base_url, model: config.model, api_key: oldKey });
    }
    if (!connection) {
      if (this.options.mode === 'live' && config.enabled) {
        await this.options.invoke('save_translation_settings', {
          enabled: false, base_url: config.base_url, model: config.model, revision: config.revision,
          credential_ref: config.credential_ref, api_key: '', enabled_data_root: null,
        });
      } else {
        await this.options.invoke('configure_translation_credential', {
          credential_ref: config.credential_ref, revision: config.revision, api_key: '',
        });
      }
      this.applied = null;
      this.unavailable = false;
      return;
    }
    const sameEndpoint = normalizeTranslationBaseUrl(config.base_url) === connection.base_url;
    const boundHere = typeof config.enabled_data_root === 'string'
      && rootKey(config.enabled_data_root) === rootKey(this.options.dataRoot);
    const existingConsent = this.options.mode === 'demo' || boundHere || !!oldKey;
    // A new merchant, restored root or changed endpoint requires its own enable confirmation.
    const enabled = config.enabled && sameEndpoint && existingConsent && !!connection.api_key;
    await this.apply(connection, config, enabled);
  }

  private async publicInfo(): Promise<TranslationInfo> {
    const info = await this.options.invoke<TranslationInfo>('get_translation_info');
    return { ...info, connection_scope: 'shared', shared_revision: this.applied?.revision ?? 0,
      ...(this.unavailable ? { message: UNAVAILABLE } : {}) };
  }

  get(): Promise<TranslationInfo> {
    return this.serialize(async () => {
      if (!this.options.canWrite()) return this.publicInfo();
      try { await this.sync(); }
      catch {
        this.unavailable = true;
        // Translation storage failures must not disconnect the business service.
        const config = await this.options.invoke<HostConfig>('get_translation_config');
        if (config.enabled) {
          await this.options.invoke('save_translation_settings', {
            enabled: false, base_url: config.base_url, model: config.model, revision: config.revision,
            credential_ref: config.credential_ref, api_key: '', enabled_data_root: null,
          });
        } else {
          await this.options.invoke('configure_translation_credential', {
            credential_ref: config.credential_ref, revision: config.revision, api_key: '',
          });
        }
        this.applied = null;
      }
      return this.publicInfo();
    });
  }

  save(input: unknown): Promise<TranslationInfo> {
    return this.serialize(async () => {
      if (!this.options.canWrite()) throw new Error('请在数据管理或退出结束后修改翻译设置。');
      const value = input as Record<string, unknown>;
      const allowed = ['enabled', 'base_url', 'model', 'revision', 'shared_revision', 'api_key', 'clear_key'];
      if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(key => !allowed.includes(key))
          || typeof value.enabled !== 'boolean' || typeof value.base_url !== 'string' || value.base_url.length > 2048
          || typeof value.model !== 'string' || value.model.length > 200 || !Number.isSafeInteger(value.revision)
          || value.api_key !== undefined && typeof value.api_key !== 'string'
          || value.clear_key !== undefined && typeof value.clear_key !== 'boolean'
          || value.shared_revision !== undefined && (!Number.isSafeInteger(value.shared_revision) || Number(value.shared_revision) < 0)) {
        throw new Error('翻译设置格式无效。');
      }
      const settings = value as unknown as TranslationSettingsInput;
      const baseUrl = normalizeTranslationBaseUrl(settings.base_url);
      const key = settings.api_key?.trim();
      if (key && settings.clear_key) throw new Error('翻译设置格式无效。');
      if (key && (/[\u0000\r\n]/.test(key) || Buffer.byteLength(key, 'utf8') > 7000)) throw new Error('API Key 格式无效。');
      await this.sync();
      const previous = await this.options.invoke<HostConfig>('get_translation_config');
      if (settings.revision !== previous.revision) throw new Error('翻译设置已变化，请保留输入并重新读取。');
      const store = await this.store();
      const current = await store.read();
      const expectedShared = settings.shared_revision ?? 0;
      if (expectedShared !== (current?.revision ?? 0)) throw new Error(CONFLICT);
      const validated = await this.options.invoke<{ base_url: string; model: string }>('validate_translation_settings', {
        base_url: baseUrl, model: settings.model,
      });
      const selectedKey = key || (settings.clear_key ? '' : current?.api_key || '');
      if (this.options.mode === 'live' && settings.enabled && (!validated.model || !selectedKey)) {
        throw new Error('启用前请填写模型并配置 API Key。');
      }
      if (settings.enabled && (!previous.enabled || normalizeTranslationBaseUrl(previous.base_url) !== validated.base_url)) {
        if (!(await this.options.confirm('启用第三方聊天翻译', '允许当前商家将聊天文字及人工回复建议发送给翻译服务？',
          `服务：${new URL(validated.base_url).origin}\n模型：${validated.model}\n服务地址、模型和 Key 在本机各商家间共享；启用仅针对当前商家。每条最多附带两条聊天前文。人工建议包含润色结果，译文仅供显示。需求整理和回复生成继续由 Accio 处理。`, '允许并保存'))) {
          throw new Error('已取消保存翻译设置。');
        }
      }
      if (!this.options.canWrite()) throw new Error('请在数据管理或退出结束后修改翻译设置。');
      const connectionChanged = !current || current.base_url !== validated.base_url || current.model !== validated.model
        || selectedKey !== current.api_key;
      let connection = current;
      if (connectionChanged) {
        connection = await store.save({ revision: expectedShared, base_url: validated.base_url, model: validated.model,
          ...(key ? { api_key: key } : {}), clear_key: settings.clear_key });
      } else if ((await store.read())?.revision !== expectedShared) {
        throw new Error(CONFLICT);
      }
      if (!connection) throw new Error('翻译设置格式无效。');
      try { await this.apply(connection, previous, settings.enabled); }
      catch { throw new Error('共享翻译配置已保存，当前窗口尚未确认，请重新读取。'); }
      return this.publicInfo();
    });
  }

  async start(): Promise<void> {
    await this.get().catch(() => {});
    if (this.timer) return;
    this.timer = setInterval(() => {
      if (this.polling || !this.options.canWrite()) return;
      this.polling = true;
      void this.get().catch(() => {}).finally(() => { this.polling = false; });
    }, 2000);
    this.timer.unref();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
  }
}
