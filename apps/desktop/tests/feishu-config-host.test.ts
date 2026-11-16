// @vitest-environment node
import path from 'node:path';
import { expect, it, vi } from 'vitest';
import { FeishuConfigHost } from '../src/main/feishu-config-host';
import type { FeishuConfiguration } from '../src/shared/feishu';

const settings: FeishuConfiguration = { enabled: true, app_id: 'cli_synthetic', table_url: 'https://example.feishu.cn/base/baseSynthetic?table=tblSynthetic', fields: {}, account_option: 'A', initial_status: 'New' };
function fixture() {
  let revision = 0;
  const store = { stage: vi.fn().mockResolvedValue('a'.repeat(64)), read: vi.fn().mockResolvedValue('encrypted-store-result') };
  const calls: { method: string; params?: Record<string, unknown> }[] = [];
  const invoke = vi.fn(async (method: string, params?: Record<string, unknown>): Promise<unknown> => {
    calls.push({ method, params });
    if (method === 'get_snapshot') return { app: { mode: 'live', seller_id: '123', account_verified: true }, settings: { feishu: { ...settings, revision } } };
    if (method === '_save_feishu_configuration') return { saved: true, revision: ++revision };
    if (method === '_grant_feishu_write') return { enabled: params!.enabled };
    if (method === '_feishu_credential_requests') return { credentials: [{ app_id: 'cli_synthetic', credential_ref: 'a'.repeat(64) }, { app_id: 'cli_old', credential_ref: 'b'.repeat(64) }] };
    return { configured: true };
  });
  const host = new FeishuConfigHost(store, invoke as never, 'live', path.resolve('synthetic-data'), () => {});
  return { store, calls, host };
}

it('revokes before storing a credential and never includes it in public settings or return values', async () => {
  const { host, calls, store } = fixture();
  const result = await host.save({ settings, expected_revision: 0, app_secret: 'synthetic-private-value' });
  expect(result).toBe(true);
  expect(calls.findIndex(call => call.method === '_grant_feishu_write')).toBeLessThan(calls.findIndex(call => call.method === '_save_feishu_configuration'));
  expect(store.stage).toHaveBeenCalledWith(expect.stringMatching(/^[a-f0-9]{64}$/), 'cli_synthetic', 'synthetic-private-value');
  const saved = calls.find(call => call.method === '_save_feishu_configuration');
  expect(JSON.stringify(saved)).not.toContain('synthetic-private-value');
  expect(calls.find(call => call.method === '_configure_feishu_credential')?.params?.app_secret).toBe('synthetic-private-value');
});

it('rejects a stale save before changing credentials and retains blank-key configuration', async () => {
  const { host, store } = fixture();
  await host.save({ settings, expected_revision: 0, app_secret: '' });
  expect(store.stage).not.toHaveBeenCalled();
  expect(store.read).toHaveBeenCalledWith(expect.any(String), 'cli_synthetic', null);
  await expect(host.save({ settings, expected_revision: 0, app_secret: 'new-key' })).rejects.toThrow('已变化');
  expect(store.stage).not.toHaveBeenCalled();
});

it('never activates a staged secret when configuration validation rejects the save', async () => {
  const oldRef = 'c'.repeat(64), newRef = 'd'.repeat(64);
  const injected: Record<string, unknown>[] = [];
  const store = { stage: vi.fn().mockResolvedValue(newRef), read: vi.fn().mockResolvedValue('original-secret') };
  const invoke = async (method: string, params?: Record<string, unknown>): Promise<unknown> => {
    if (method === 'get_snapshot') return { app: { mode: 'live', seller_id: '123', account_verified: true }, settings: { feishu: { ...settings, revision: 0, credential_ref: oldRef } } };
    if (method === '_save_feishu_configuration') throw new Error('invalid table URL');
    if (method === '_feishu_credential_requests') return { credentials: [{ app_id: settings.app_id, credential_ref: oldRef }] };
    if (method === '_configure_feishu_credential') injected.push(params!);
    return {};
  };
  const host = new FeishuConfigHost(store, invoke as never, 'live', path.resolve('synthetic-data'), () => {});
  await expect(host.save({ settings: { ...settings, table_url: 'invalid' }, expected_revision: 0, app_secret: 'replacement' })).rejects.toThrow();
  expect(injected).toHaveLength(0);
  await host.synchronize();
  expect(store.read).toHaveBeenCalledWith(expect.any(String), settings.app_id, oldRef);
  expect(injected[0]).toMatchObject({ app_secret: 'original-secret', credential_ref: oldRef });
  expect(JSON.stringify(injected)).not.toContain('replacement');
});

it('restores only the application credentials requested by owned unresolved operations', async () => {
  const { host, store, calls } = fixture();
  await host.synchronize();
  await host.synchronize();
  expect(store.read.mock.calls.map(call => call[1])).toEqual(['cli_synthetic', 'cli_old']);
  expect(calls.filter(call => call.method === '_configure_feishu_credential').map(call => call.params?.app_id)).toEqual(['cli_synthetic', 'cli_old']);
  expect(calls.some(call => call.method === '_grant_feishu_write')).toBe(false);
});
