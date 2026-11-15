// @vitest-environment node
import { describe, expect, it, vi } from 'vitest';
import { TranslationActivationHost } from '../src/main/translation-activation-host';
import type { ModelSettingsSnapshot } from '../src/shared/model-settings';

const settings = () => ({ format: 'model-settings-v1', directory_revision: 1, routes: { reception: { profile_id: null, route_revision: 0 }, translation: { profile_id: 'p', route_revision: 1 } }, profiles: [{ profile_id: 'p', name: '翻译', protocol: 'chat-completions', base_url: 'https://example.com/v1', model: 'm', has_key: true, config_revision: 1, credential_revision: 1, capability_revision: 1, capabilities: { text: { status: 'passed' }, translation: { status: 'passed' } } }] }) as unknown as ModelSettingsSnapshot;
function setup() {
  const model = settings();
  const invoke = vi.fn(async (method: string) => method === 'get_snapshot' ? { app: { seller_id: '123', account_verified: true } } : method === 'get_translation_info' ? { enabled: false, revision: 2 } : { enabled: true, revision: 3 });
  const confirm = vi.fn(async () => true);
  const host = new TranslationActivationHost({ dataRoot: 'C:/work', invoke: invoke as never, readModels: async () => model, canWrite: () => true, confirm });
  return { model, invoke, confirm, host };
}
describe('商家翻译授权', () => {
  it('确认当前商家及数据根后调用私有接口，不传密钥', async () => {
    const { host, invoke, confirm } = setup();
    await host.set(true);
    expect(confirm).toHaveBeenCalledOnce();
    expect(invoke).toHaveBeenLastCalledWith('_set_translation_enabled', expect.objectContaining({ enabled: true, seller_id: '123', revision: 2, data_root: expect.any(String) }));
    expect(JSON.stringify(invoke.mock.calls)).not.toContain('api_key');
  });
  it('确认期间路由变化时拒绝开启', async () => {
    const { model, host, invoke, confirm } = setup();
    confirm.mockImplementation(async () => { model.routes.translation.route_revision++; return true; });
    await expect(host.set(true)).rejects.toThrow();
    expect(invoke.mock.calls.some(([method]) => method === '_set_translation_enabled')).toBe(false);
  });
  it('关闭无需模型确认', async () => {
    const { host, confirm, invoke } = setup();
    await host.set(false);
    expect(confirm).not.toHaveBeenCalled();
    expect(invoke).toHaveBeenLastCalledWith('_set_translation_enabled', expect.objectContaining({ enabled: false }));
  });
});
