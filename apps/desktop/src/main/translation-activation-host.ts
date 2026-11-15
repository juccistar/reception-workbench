import path from 'node:path';
import type { ModelSettingsSnapshot } from '../shared/model-settings';
import type { Snapshot } from '../shared/api';
import type { TranslationInfo } from '../shared/translation';

type Invoke = <T>(method: string, params?: Record<string, unknown>) => Promise<T>;
export class TranslationActivationHost {
  constructor(private readonly options: { dataRoot: string; invoke: Invoke; readModels: () => Promise<ModelSettingsSnapshot>; canWrite: () => boolean; confirm: (title: string, message: string, detail: string, action: string) => Promise<boolean> }) {}
  async get(): Promise<TranslationInfo> {
    if (!this.options.canWrite()) throw new Error('后台尚未就绪。');
    await this.options.readModels();
    return this.options.invoke<TranslationInfo>('get_translation_info');
  }
  async set(enabled: unknown): Promise<TranslationInfo> {
    if (typeof enabled !== 'boolean' || !this.options.canWrite()) throw new Error('翻译设置不可用。');
    const snapshot = await this.options.invoke<Snapshot>('get_snapshot');
    if (!snapshot.app.account_verified || !snapshot.app.seller_id) throw new Error('请先确认当前商家身份。');
    const info = await this.options.invoke<TranslationInfo>('get_translation_info');
    const root = path.resolve(this.options.dataRoot);
    if (enabled) {
      const models = structuredClone(await this.options.readModels());
      const route = models.routes.translation;
      const profile = models.profiles.find(p => p.profile_id === route.profile_id);
      if (!profile?.has_key || profile.capabilities.text.status !== 'passed' || profile.capabilities.translation.status !== 'passed') throw new Error('请先在启动台选择并检查翻译模型。');
      if (!await this.options.confirm('启用聊天翻译', '允许当前商家将聊天文字及人工回复建议发送给所选翻译服务？', `商家：${snapshot.app.seller_id}\n数据目录：${root}\n服务：${new URL(profile.base_url).origin}\n模型：${profile.name}（${profile.model}）\n翻译可能包含有限的聊天上下文。`, '允许并启用')) throw new Error('已取消启用翻译。');
      if (!this.options.canWrite()) throw new Error('工作区状态已变化，请重新操作。');
      const [latest, current] = await Promise.all([this.options.readModels(), this.options.invoke<Snapshot>('get_snapshot')]);
      const p = latest.profiles.find(item => item.profile_id === latest.routes.translation.profile_id);
      if (!current.app.account_verified || current.app.seller_id !== snapshot.app.seller_id || latest.routes.translation.route_revision !== route.route_revision || !p || p.profile_id !== profile.profile_id || p.config_revision !== profile.config_revision || p.credential_revision !== profile.credential_revision || p.capability_revision !== profile.capability_revision) throw new Error('商家或翻译模型已变化，请重新确认。');
    }
    if (!this.options.canWrite()) throw new Error('工作区状态已变化，请重新操作。');
    return this.options.invoke<TranslationInfo>('_set_translation_enabled', { enabled, seller_id: snapshot.app.seller_id, data_root: root, revision: info.revision });
  }
}
