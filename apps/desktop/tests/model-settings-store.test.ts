// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { mkdtemp, readFile, rm, writeFile, symlink } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { ModelSettingsStore } from '../src/main/model-settings-store';
import type { ModelSettingsSaveInput } from '../src/shared/model-settings';

const encrypt = { isEncryptionAvailable: () => true, encryptString: (s: string) => Buffer.from(`sealed:${s}`), decryptString: (b: Buffer) => b.toString().replace(/^sealed:/, '') };
const profile = (id: string) => ({ profile_id: id, name: id, protocol: 'chat-completions' as const, base_url: 'https://example.invalid/v1', model: 'model', credential_ref: null });
const save = (profiles: ModelSettingsSaveInput['profiles'], revision = 0): ModelSettingsSaveInput => ({ expected_directory_revision: revision, profiles, routes: { reception: 'a', translation: null }, credential_changes: { a: { action: 'replace', key: 'test-secret' } } });
describe('model settings store', () => {
  it('stores one atomic snapshot, keeps secrets private, and ignores name changes for execution revisions', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'model-settings-'));
    try {
      const store = new ModelSettingsStore(dir, encrypt, async () => async () => {});
      const first = await store.save(save([profile('a'), profile('b')]));
      expect(JSON.stringify(first)).not.toContain('test-secret');
      expect(JSON.stringify(await store.runtime())).toContain('test-secret');
      const disk = JSON.parse(await readFile(store.authorityFile, 'utf8'));
      expect(disk.encrypted_keys[first.profiles[0].credential_ref!]).toBeTruthy();
      const edit = first.profiles.map(({ profile_id, name, protocol, base_url, model, credential_ref }) => ({ profile_id, name: profile_id === 'a' ? 'renamed' : name, protocol, base_url, model, credential_ref }));
      const second = await store.save({ expected_directory_revision: 1, profiles: edit, routes: { reception: 'a', translation: null }, credential_changes: {} });
      expect(second.profiles[0].config_revision).toBe(first.profiles[0].config_revision);
      expect(second.profiles[0].credential_revision).toBe(first.profiles[0].credential_revision);
      await expect(store.save(save([profile('a')]))).rejects.toThrow('model_settings_conflict');
      await expect(store.recordCapabilities('a', 999, 1, { text: { status: 'passed', checked_at: 1 } })).rejects.toThrow('model_settings_stale_check');
      await expect(store.save({ expected_directory_revision: 2, profiles: [], routes: { reception: 'a', translation: null }, credential_changes: {} })).rejects.toThrow();
      await rm(store.authorityFile);
      await expect(store.read()).rejects.toThrow('model_settings_unavailable');
    } finally { await rm(dir, { recursive: true, force: true }); }
  });
  it('serializes concurrent instances and invalidates all profiles sharing a replaced credential', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'model-settings-'));
    let queue = Promise.resolve();
    const lock = async () => { let release!: () => void; const next = new Promise<void>(r => { release = r; }); const prior = queue; queue = next; await prior; return async () => release(); };
    try {
      const a = new ModelSettingsStore(dir, encrypt, lock); const b = new ModelSettingsStore(dir, encrypt, lock);
      const first = await a.save(save([profile('a'), profile('b')]));
      const edits = first.profiles.map(({ profile_id, name, protocol, base_url, model, credential_ref }) => ({ profile_id, name, protocol, base_url, model, credential_ref: profile_id === 'b' ? first.profiles[0].credential_ref : credential_ref }));
      const input = { expected_directory_revision: first.directory_revision, profiles: edits, routes: { reception: 'a', translation: 'b' }, credential_changes: {} };
      const results = await Promise.allSettled([a.save(input), b.save(input)]);
      expect(results.filter(r => r.status === 'fulfilled')).toHaveLength(1);
      expect(results.filter(r => r.status === 'rejected')).toHaveLength(1);
      const shared = await a.read();
      const replaced = await b.save({ ...input, expected_directory_revision: shared.directory_revision, credential_changes: { a: { action: 'replace', key: 'new' } } });
      expect(replaced.profiles.map(p => p.credential_revision)).toEqual(shared.profiles.map(p => p.credential_revision + 1));
      expect((await a.runtime()).credentials).toEqual({ a: 'new', b: 'new' });
      await expect(a.recordCapabilities('b', shared.profiles[1].config_revision, shared.profiles[1].credential_revision, { text: { status: 'passed', checked_at: 1 } })).rejects.toThrow('model_settings_stale_check');
      await expect(a.recordCapabilities('a', replaced.profiles[0].config_revision, replaced.profiles[0].credential_revision, { text: { status: 'passed', checked_at: 1 } }, () => false)).rejects.toThrow('model_check_cancelled');
      expect((await a.read()).directory_revision).toBe(replaced.directory_revision);
    } finally { await rm(dir, { recursive: true, force: true }); }
  });
  it('fails closed for corrupt and linked authority files', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'model-settings-'));
    try {
      const store = new ModelSettingsStore(dir, encrypt, async () => async () => {});
      await store.save(save([profile('a')]));
      await writeFile(store.authorityFile, '{invalid');
      await expect(store.read()).rejects.toThrow('model_settings_unavailable');
      await rm(store.authorityFile);
      const target = path.join(dir, 'target'); await writeFile(target, '{}');
      try { await symlink(target, store.authorityFile, 'file'); } catch (e) { if ((e as NodeJS.ErrnoException).code === 'EPERM') return; throw e; }
      await expect(store.read()).rejects.toThrow('model_settings_unavailable');
    } finally { await rm(dir, { recursive: true, force: true }); }
  });
  it('checks an unrouted profile, preserves and clears keys, and rejects stale results', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'model-settings-'));
    try {
      const store = new ModelSettingsStore(dir, encrypt, async () => async () => {});
      const initial = await store.save({ ...save([profile('a'), profile('b')]), credential_changes: { a: { action: 'replace', key: 'first' }, b: { action: 'replace', key: 'second' } } });
      expect((await store.checkProfile('b')).api_key).toBe('second');
      expect((await store.runtime()).credentials).toEqual({ a: 'first' });
      const edits = initial.profiles.map(({ profile_id, name, protocol, base_url, model, credential_ref }) => ({ profile_id, name, protocol, base_url, model, credential_ref }));
      const next = await store.save({ expected_directory_revision: initial.directory_revision, profiles: edits, routes: { reception: 'a', translation: null }, credential_changes: { b: { action: 'replace', key: 'replacement' } } });
      await expect(store.recordCapabilities('b', initial.profiles[1].config_revision, initial.profiles[1].credential_revision, { text: { status: 'passed', checked_at: 1 } })).rejects.toThrow('model_settings_stale_check');
      expect((await store.checkProfile('b')).api_key).toBe('replacement');
      await store.save({ expected_directory_revision: next.directory_revision, profiles: next.profiles.map(({ profile_id, name, protocol, base_url, model, credential_ref }) => ({ profile_id, name, protocol, base_url, model, credential_ref })), routes: { reception: 'a', translation: null }, credential_changes: { b: { action: 'clear' } } });
      await expect(store.checkProfile('b')).rejects.toThrow('model_check_not_configured');
      expect((await store.checkProfile('a')).api_key).toBe('first');
    } finally { await rm(dir, { recursive: true, force: true }); }
  });
});
