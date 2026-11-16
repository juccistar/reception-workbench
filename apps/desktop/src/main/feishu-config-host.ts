import { createHash } from 'node:crypto';
import path from 'node:path';
import type { FeishuBridge, FeishuConfiguration } from '../shared/feishu';
import type { Snapshot } from '../shared/api';
import type { FeishuCredentialStore } from './feishu-credentials';

type Invoke = <T>(method: string, params?: Record<string, unknown>) => Promise<T>;
type Store = Pick<FeishuCredentialStore, 'save' | 'read'>;
const configKeys = ['account_option', 'app_id', 'enabled', 'fields', 'initial_status', 'table_url'];
const failure = () => new Error('飞书配置未能保存，请保留输入后重试。');

/** Renderer never receives a credential or a general private service invocation. */
export class FeishuConfigHost {
  private serial: Promise<unknown> = Promise.resolve();
  private synchronized = '';
  constructor(private readonly store: Store, private readonly invoke: Invoke,
    private readonly mode: 'live' | 'demo', private readonly dataRoot: string,
    private readonly ready: () => void) {}

  private exclusive<T>(task: () => Promise<T>): Promise<T> {
    const result = this.serial.then(task, task);
    this.serial = result.catch(() => {});
    return result;
  }
  private async target(expected?: number) {
    this.ready();
    const snapshot = await this.invoke<Snapshot>('get_snapshot');
    this.ready();
    if (!snapshot.app.account_verified || !snapshot.app.seller_id || snapshot.app.mode !== this.mode) throw failure();
    const cfg = snapshot.settings.feishu;
    const revision = Number.isSafeInteger(cfg.revision) ? Number(cfg.revision) : 0;
    if (expected !== undefined && expected !== revision) throw new Error('飞书设置已变化，请保留输入并重新读取。');
    const seller_id = snapshot.app.seller_id;
    const data_root = path.resolve(this.dataRoot);
    const merchant = createHash('sha256').update(JSON.stringify([this.mode, seller_id, data_root.toLowerCase()])).digest('hex');
    return { snapshot, cfg, revision, seller_id, data_root, merchant };
  }
  private checkSettings(value: unknown): asserts value is FeishuConfiguration {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw failure();
    const cfg = value as Record<string, unknown>;
    if (Object.keys(cfg).sort().join(',') !== configKeys.join(',') || typeof cfg.enabled !== 'boolean'
      || ['app_id', 'table_url', 'account_option', 'initial_status'].some(key => typeof cfg[key] !== 'string' || (cfg[key] as string).length > 4096 || /[\0\r\n]/.test(cfg[key] as string))
      || !cfg.fields || typeof cfg.fields !== 'object' || Array.isArray(cfg.fields) || JSON.stringify(cfg.fields).length > 16384) throw failure();
    if (cfg.enabled && (!/^[A-Za-z0-9_-]{1,128}$/.test(String(cfg.app_id)) || !cfg.table_url)) throw new Error('请填写飞书应用 ID、密钥和表格链接。');
  }
  save(input: Parameters<FeishuBridge['saveFeishuConfiguration']>[0]): Promise<boolean> {
    return this.exclusive(async () => {
      if (!input || typeof input !== 'object' || Object.keys(input).sort().join(',') !== 'app_secret,expected_revision,settings'
        || !Number.isSafeInteger(input.expected_revision) || input.expected_revision < 0
        || typeof input.app_secret !== 'string' || input.app_secret.length > 4096 || /[\0\r\n]/.test(input.app_secret)) throw failure();
      this.checkSettings(input.settings);
      const target = await this.target(input.expected_revision);
      const identity = { seller_id: target.seller_id, data_root: target.data_root };
      // Revocation precedes disk changes, including failed encryption or validation.
      await this.invoke('_grant_feishu_write', { ...identity, expected_revision: target.revision, enabled: false });
      const appId = input.settings.app_id;
      let secret: string | null = null;
      if (appId) {
        if (input.app_secret.trim()) { await this.store.save(target.merchant, appId, input.app_secret); secret = input.app_secret; }
        else secret = await this.store.read(target.merchant, appId);
      } else if (input.app_secret) throw failure();
      if (input.settings.enabled && !secret) throw new Error('请填写飞书应用密钥。');
      await this.target(input.expected_revision);
      const result = await this.invoke<{ saved: boolean; revision: number }>('_save_feishu_configuration', {
        ...identity, expected_revision: target.revision, settings: input.settings,
      });
      if (!result.saved) throw failure();
      if (secret) await this.invoke('_configure_feishu_credential', { ...identity, app_id: appId, app_secret: secret });
      this.synchronized = secret ? JSON.stringify([target.merchant, appId, result.revision]) : '';
      return true;
    });
  }
  grant(input: Parameters<FeishuBridge['grantFeishuWrite']>[0]): Promise<boolean> {
    return this.exclusive(async () => {
      if (!input || Object.keys(input).sort().join(',') !== 'enabled,expected_revision' || typeof input.enabled !== 'boolean'
        || !Number.isSafeInteger(input.expected_revision) || input.expected_revision < 0) throw failure();
      const target = await this.target(input.expected_revision);
      const result = await this.invoke<{ enabled: boolean }>('_grant_feishu_write', { seller_id: target.seller_id,
        data_root: target.data_root, expected_revision: target.revision, enabled: input.enabled });
      return result.enabled === input.enabled;
    });
  }
  synchronize(): Promise<void> {
    return this.exclusive(async () => {
      const target = await this.target();
      const requested = await this.invoke<{ app_ids: string[] }>('_feishu_credential_requests', { seller_id: target.seller_id, data_root: target.data_root });
      if (!requested || !Array.isArray(requested.app_ids) || requested.app_ids.some(id => typeof id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(id))) throw failure();
      const key = JSON.stringify([target.merchant, target.revision, requested.app_ids]);
      if (key === this.synchronized) return;
      for (const appId of requested.app_ids) {
        const secret = await this.store.read(target.merchant, appId);
        if (!secret) continue;
        this.ready();
        await this.invoke('_configure_feishu_credential', { seller_id: target.seller_id, data_root: target.data_root,
          app_id: appId, app_secret: secret });
      }
      this.synchronized = key;
    });
  }
}
