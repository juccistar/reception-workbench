import { randomBytes } from 'node:crypto';
import type { ModelSettingsStore } from './model-settings-store';
import type { SharedTranslationStore } from './shared-translation';

/** Explicit import of the old shared translation connection; never changes the reception route. */
export async function importLegacyTranslation(models: ModelSettingsStore, legacy: SharedTranslationStore, confirm: (title: string, message: string, detail: string, action: string) => Promise<boolean>): Promise<void> {
  const old = await legacy.read();
  if (!old?.api_key || !old.model) throw new Error('没有可迁入的旧共享翻译配置。');
  const settings = await models.read();
  if (settings.routes.translation.profile_id) throw new Error('已有翻译模型，请先在启动台管理配置。');
  if (!await confirm('迁入旧翻译配置', '将旧共享翻译连接迁入全局模型配置？', `服务：${new URL(old.base_url).origin}\n模型：${old.model}\n迁入不会开启任何商家的翻译，也不会修改接待模型。`, '迁入')) throw new Error('已取消迁入。');
  const current = await legacy.read();
  if (!current || current.revision !== old.revision || current.base_url !== old.base_url || current.model !== old.model || current.api_key !== old.api_key) throw new Error('旧翻译配置已变化，请重新操作。');
  const profileId = `legacy-${randomBytes(16).toString('hex')}`;
  await models.save({ expected_directory_revision: settings.directory_revision,
    profiles: [...settings.profiles.map(({ profile_id, name, protocol, base_url, model, credential_ref }) => ({ profile_id, name, protocol, base_url, model, credential_ref })),
      { profile_id: profileId, name: '旧聊天翻译', protocol: 'chat-completions', base_url: old.base_url, model: old.model, credential_ref: null }],
    routes: { reception: settings.routes.reception.profile_id, translation: profileId },
    credential_changes: { [profileId]: { action: 'replace', key: old.api_key } } });
}
