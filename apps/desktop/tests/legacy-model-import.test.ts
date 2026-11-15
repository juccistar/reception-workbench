// @vitest-environment node
import { expect, it, vi } from 'vitest';
import { importLegacyTranslation } from '../src/main/legacy-model-import';

it('仅经确认迁入翻译路由并保留接待路由', async () => {
  const old = { revision: 1, base_url: 'https://example.com/v1', model: 'old', api_key: 'secret', credential_ref: 'ref' };
  const legacy = { read: vi.fn(async () => old) };
  const models = { read: vi.fn(async () => ({ directory_revision: 7, profiles: [], routes: { reception: { profile_id: 'existing' }, translation: { profile_id: null } } })), save: vi.fn(async () => ({})) };
  await importLegacyTranslation(models as never, legacy as never, async () => true);
  expect(models.save).toHaveBeenCalledWith(expect.objectContaining({ expected_directory_revision: 7, routes: expect.objectContaining({ reception: 'existing', translation: expect.stringMatching(/^legacy-/) }) }));
  expect(legacy.read).toHaveBeenCalledTimes(2);
});
it('已有翻译路由时拒绝覆盖', async () => {
  const legacy = { read: vi.fn(async () => ({ revision: 1, base_url: 'https://example.com', model: 'old', api_key: 'secret' })) };
  const models = { read: vi.fn(async () => ({ profiles: [], routes: { translation: { profile_id: 'selected' } } })), save: vi.fn() };
  await expect(importLegacyTranslation(models as never, legacy as never, async () => true)).rejects.toThrow();
  expect(models.save).not.toHaveBeenCalled();
});
