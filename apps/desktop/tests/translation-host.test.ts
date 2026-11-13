// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { TranslationHost } from '../src/main/translation-host';
import { SharedTranslationStore } from '../src/main/shared-translation';
import { TranslationCredentialStore } from '../src/main/translation-credentials';
import { normalizeTranslationBaseUrl, TRANSLATION_ENDPOINT_ERROR } from '../src/shared/translation-endpoint';
import type { TranslationInfo } from '../src/shared/translation';

const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });
const encryption = {
  isEncryptionAvailable: () => true,
  encryptString: (value: string) => Buffer.from(value).map(byte => byte ^ 0xaa),
  decryptString: (value: Buffer) => value.map(byte => byte ^ 0xaa).toString('utf8'),
};
function mutex() {
  let previous = Promise.resolve();
  return async () => {
    const wait = previous;
    let release!: () => void;
    previous = new Promise<void>(resolve => { release = resolve; });
    await wait;
    return async () => release();
  };
}
function backend() {
  const state = { config: { enabled: false, base_url: 'https://api.deepseek.com/v1', model: '', revision: 0,
    credential_ref: null as string | null, enabled_data_root: null as string | null }, key: '' };
  const invoke = vi.fn(async (method: string, params: Record<string, any> = {}): Promise<any> => {
    if (method === 'get_translation_config') return { ...state.config };
    if (method === 'get_translation_info') return { ...state.config, has_key: !!state.key,
      status: !state.key ? 'not_configured' : state.config.enabled ? 'ready' : 'disabled', message: '',
      usage: { requests: 0, prompt_tokens: 0, completion_tokens: 0, unknown_requests: 0, cache_hits: 0 } };
    if (method === 'validate_translation_settings') {
      if (params.base_url === 'https://10.0.0.1/v1') throw new Error(TRANSLATION_ENDPOINT_ERROR);
      if (/[\x00-\x1f]/.test(params.model)) throw new Error('翻译设置无效。');
      return { base_url: normalizeTranslationBaseUrl(params.base_url), model: params.model.trim() };
    }
    if (method === 'configure_translation_credential') { state.key = params.api_key; return {}; }
    if (method === 'save_translation_settings') {
      if (params.revision !== state.config.revision) throw new Error('翻译设置已变化，请保留输入并重新读取。');
      state.config = { enabled: params.enabled, base_url: params.base_url, model: params.model,
        revision: state.config.revision + 1, credential_ref: params.credential_ref,
        enabled_data_root: params.enabled_data_root ?? null };
      state.key = params.api_key;
      return {};
    }
    throw new Error('Unexpected business operation: ' + method);
  });
  return { state, invoke };
}
async function setup() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'translation-host-'));
  roots.push(root);
  const lock = mutex();
  const store = () => new SharedTranslationStore(path.join(root, 'shared/live'), encryption, lock);
  const a = backend(), b = backend();
  const confirmA = vi.fn(async () => true), confirmB = vi.fn(async () => true);
  const options = (id: string, service: ReturnType<typeof backend>, confirm: typeof confirmA) => ({
    mode: 'live' as const, dataRoot: path.join(root, id, 'data'), legacyDirectory: path.join(root, id, 'credentials'),
    encryption, invoke: service.invoke, confirm, canWrite: () => true, storeFactory: async () => store(),
  });
  const aOptions = options('merchant-a', a, confirmA), bOptions = options('merchant-b', b, confirmB);
  return { root, store, a, b, aOptions, bOptions, confirmA, confirmB,
    first: new TranslationHost(aOptions), second: new TranslationHost(bOptions) };
}
const input = (info: TranslationInfo, patch: Record<string, unknown> = {}) => ({ enabled: info.enabled,
  base_url: info.base_url, model: info.model, revision: info.revision, shared_revision: info.shared_revision, ...patch });

async function configure(first: TranslationHost) {
  return first.save(input(await first.get(), { enabled: true, base_url: 'http://127.0.0.1:3425/v1',
    model: 'shared-model', api_key: 'synthetic-private-key' }));
}

describe('shared connection with merchant-owned translation enable state', () => {
  it('shares credentials across merchants while keeping new merchants disabled and returning no key', async () => {
    const { first, second, a, b, confirmA, confirmB } = await setup();
    const one = await configure(first);
    const two = await second.get();
    expect(one.enabled).toBe(true);
    expect(two).toMatchObject({ enabled: false, has_key: true, model: 'shared-model',
      base_url: 'http://127.0.0.1:3425/v1', shared_revision: one.shared_revision });
    expect(b.state.key).toBe(a.state.key);
    expect(JSON.stringify(two)).not.toContain('synthetic-private-key');
    expect(confirmA).toHaveBeenCalledTimes(1);
    expect(confirmB).not.toHaveBeenCalled();
    const enabled = await second.save(input(two, { enabled: true }));
    expect(enabled.enabled).toBe(true);
    expect(enabled.shared_revision).toBe(one.shared_revision);
    expect(confirmB).toHaveBeenCalledTimes(1);
  });

  it('synchronizes edits and clears without re-enabling another merchant or resurrecting a legacy key', async () => {
    const { first, second, a, b, bOptions } = await setup();
    await configure(first);
    const secondBefore = await second.get();
    const changed = await first.save(input(await first.get(), { model: 'updated-model', api_key: 'rotated-private-key' }));
    const updated = await second.get();
    expect(updated).toMatchObject({ enabled: false, has_key: true, model: 'updated-model', shared_revision: changed.shared_revision });
    expect(b.state.key).toBe('rotated-private-key');
    await expect(second.save(input(secondBefore, { model: 'stale-model' }))).rejects.toThrow(/已变化/);
    await first.save(input(await first.get(), { enabled: false, clear_key: true }));
    const oldRef = await new TranslationCredentialStore(bOptions.legacyDirectory, encryption).put(
      JSON.stringify({ api_key: 'old-legacy-private-key', data_root: bOptions.dataRoot }));
    b.state.config = { ...b.state.config, credential_ref: oldRef, enabled: true, enabled_data_root: null };
    expect(await second.get()).toMatchObject({ enabled: false, has_key: false });
    expect(b.state.key).toBe('');
    expect(a.state.key).toBe('');
  });

  it('disables other merchants after endpoint changes and after restoring settings into a new data root', async () => {
    const { first, second, confirmB, b, bOptions } = await setup();
    await configure(first);
    await second.save(input(await second.get(), { enabled: true }));
    expect(confirmB).toHaveBeenCalledTimes(1);
    await first.save(input(await first.get(), { base_url: 'https://new-provider.example.com/v1' }));
    expect(await second.get()).toMatchObject({ enabled: false, has_key: true, base_url: 'https://new-provider.example.com/v1' });
    expect(confirmB).toHaveBeenCalledTimes(1);
    await second.save(input(await second.get(), { enabled: true }));
    const restored = new TranslationHost({ ...bOptions, dataRoot: path.join(bOptions.dataRoot, 'restored') });
    expect(await restored.get()).toMatchObject({ enabled: false, has_key: true });
    expect(b.state.config.enabled_data_root).toBeNull();
  });

  it('imports a valid root-bound legacy key once without taking business ownership', async () => {
    const { first, second, a, aOptions } = await setup();
    const oldRef = await new TranslationCredentialStore(aOptions.legacyDirectory, encryption).put(
      JSON.stringify({ api_key: 'legacy-private-key', data_root: aOptions.dataRoot }));
    a.state.config = { enabled: true, base_url: 'http://127.0.0.1:3425/v1', model: 'legacy-model',
      revision: 4, credential_ref: oldRef, enabled_data_root: null };
    expect(await first.get()).toMatchObject({ enabled: true, has_key: true, model: 'legacy-model', shared_revision: 1 });
    expect(a.state.config.enabled_data_root).toBe(path.resolve(aOptions.dataRoot));
    expect(await second.get()).toMatchObject({ enabled: false, has_key: true, model: 'legacy-model' });
    expect(a.invoke.mock.calls.every(([method]) => method.includes('translation'))).toBe(true);
  });

  it('does not import a legacy key that belongs to a different root', async () => {
    const { first, a, aOptions, store } = await setup();
    const oldRef = await new TranslationCredentialStore(aOptions.legacyDirectory, encryption).put(
      JSON.stringify({ api_key: 'wrong-root-key', data_root: path.join(aOptions.dataRoot, 'old') }));
    a.state.config = { enabled: true, base_url: 'https://example.com/v1', model: 'legacy-model',
      revision: 4, credential_ref: oldRef, enabled_data_root: null };
    expect(await first.get()).toMatchObject({ has_key: false, enabled: false });
    expect(await store().read()).toBeNull();
    expect(a.state.key).toBe('');
  });

  it('validates the endpoint before publishing a global change and honors cancellation', async () => {
    const { first, store, confirmA } = await setup();
    const before = await configure(first);
    const connection = await store().read();
    await expect(first.save(input(before, { base_url: 'https://10.0.0.1/v1' }))).rejects.toThrow(TRANSLATION_ENDPOINT_ERROR);
    expect(await store().read()).toEqual(connection);
    confirmA.mockResolvedValueOnce(false);
    await expect(first.save(input(await first.get(), { base_url: 'https://other.example.com/v1' }))).rejects.toThrow('已取消');
    expect(await store().read()).toEqual(connection);
  });

  it('fails translation closed when shared storage is unavailable without repeated writes or business calls', async () => {
    const { first, a, aOptions } = await setup();
    await configure(first);
    const unavailable = new TranslationHost({ ...aOptions, storeFactory: async () => { throw new Error('private-error-body'); } });
    const info = await unavailable.get();
    expect(info).toMatchObject({ enabled: false, has_key: false });
    expect(info.message).toContain('无法读取本机共享翻译配置');
    expect(JSON.stringify(info)).not.toContain('private-error-body');
    const writes = a.invoke.mock.calls.filter(([method]) => method === 'save_translation_settings').length;
    await unavailable.get();
    expect(a.invoke.mock.calls.filter(([method]) => method === 'save_translation_settings')).toHaveLength(writes);
    expect(a.invoke.mock.calls.every(([method]) => method.includes('translation'))).toBe(true);
  });
});
