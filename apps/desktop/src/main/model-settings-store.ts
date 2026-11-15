import path from 'node:path';
import { randomBytes } from 'node:crypto';
import { lstat, mkdir, open, readFile, rename, unlink } from 'node:fs/promises';
import { resolveWindowsUserLocalAppData } from './windows-user-directories';
// @ts-ignore Existing mutex implementation has no declaration.
import { acquireMerchantRegistryMutex } from '../../../../scripts/merchant-mutex.mjs';
import { MODEL_CAPABILITIES, type ModelCapability, type ModelCapabilityState, type ModelProfile, type ModelRuntimePacket, type ModelSettingsSaveInput, type ModelSettingsSnapshot } from '../shared/model-settings';
import type { CredentialEncryption } from './translation-credentials';
import { createWindowsUserEncryption } from './windows-user-encryption';

type Disk = ModelSettingsSnapshot & { encrypted_keys: Record<string, string> };
const LIMIT = 1024 * 1024;
const idPattern = /^[A-Za-z0-9_-]{1,128}$/;
const refPattern = /^[a-f0-9]{64}$/;
const purposes = ['reception', 'translation'] as const;
const fail = (): never => { throw new Error('model_settings_invalid'); };
const rev = (v: unknown): v is number => Number.isSafeInteger(v) && Number(v) >= 0 && Number(v) < 2147483647;
const exact = (value: object, keys: string[]) => Object.keys(value).sort().join(',') === keys.sort().join(',');
const emptyCaps = () => Object.fromEntries(MODEL_CAPABILITIES.map(k => [k, { status: 'not_checked', checked_at: null }])) as ModelProfile['capabilities'];
const blank = (): Disk => ({ format: 'model-settings-v1', directory_revision: 0, profiles: [], routes: { reception: { profile_id: null, route_revision: 0 }, translation: { profile_id: null, route_revision: 0 } }, encrypted_keys: {} });
const cleanString = (v: unknown, max: number): v is string => typeof v === 'string' && v.length > 0 && v.length <= max && v === v.trim() && !/[\x00-\x1f\x7f]/.test(v);
function endpoint(v: unknown): v is string {
  if (!cleanString(v, 2048)) return false;
  try { const u = new URL(v); return ['http:', 'https:'].includes(u.protocol) && !u.username && !u.password && !u.hash && !u.search; } catch { return false; }
}
function validate(d: Disk): void {
  if (!d || typeof d !== 'object' || !exact(d, ['format', 'directory_revision', 'profiles', 'routes', 'encrypted_keys']) || d.format !== 'model-settings-v1' || !rev(d.directory_revision) || !Array.isArray(d.profiles) || d.profiles.length > 32 || !d.routes || !exact(d.routes, ['reception', 'translation']) || !d.encrypted_keys || typeof d.encrypted_keys !== 'object' || Array.isArray(d.encrypted_keys)) fail();
  const ids = new Set<string>();
  for (const [ref, value] of Object.entries(d.encrypted_keys)) if (!refPattern.test(ref) || typeof value !== 'string' || !value || !/^[A-Za-z0-9+/]+={0,2}$/.test(value) || Buffer.from(value, 'base64').toString('base64') !== value) fail();
  for (const p of d.profiles) {
    if (!p || !exact(p, ['profile_id', 'name', 'protocol', 'base_url', 'model', 'credential_ref', 'has_key', 'config_revision', 'credential_revision', 'capability_revision', 'capabilities']) || !idPattern.test(p.profile_id) || ids.has(p.profile_id) || !cleanString(p.name, 200) || !['chat-completions', 'anthropic'].includes(p.protocol) || !endpoint(p.base_url) || !cleanString(p.model, 200) || p.credential_ref !== null && !refPattern.test(p.credential_ref) || typeof p.has_key !== 'boolean' || p.has_key !== Boolean(p.credential_ref && d.encrypted_keys[p.credential_ref]) || !rev(p.config_revision) || !rev(p.credential_revision) || !rev(p.capability_revision) || !p.capabilities || Object.keys(p.capabilities).sort().join(',') !== [...MODEL_CAPABILITIES].sort().join(',')) fail();
    ids.add(p.profile_id);
    for (const c of MODEL_CAPABILITIES) { const item = p.capabilities[c]; if (!item || !['not_checked', 'passed', 'failed'].includes(item.status) || (item.status === 'not_checked' ? item.checked_at !== null : !Number.isSafeInteger(item.checked_at) || Number(item.checked_at) < 0)) fail(); }
  }
  for (const purpose of purposes) { const r = d.routes[purpose]; if (!r || !rev(r.route_revision) || r.profile_id !== null && !ids.has(r.profile_id)) fail(); }
}
function publicSnapshot(d: Disk): ModelSettingsSnapshot { const { encrypted_keys: _keys, ...settings } = d; return structuredClone(settings); }
async function secureDirectory(directory: string): Promise<void> {
  if (!path.isAbsolute(directory) || /^(?:\\\\|\/\/)/.test(directory)) fail();
  const root = path.parse(directory).root; let current = root;
  for (const part of directory.slice(root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    try { await mkdir(current, { mode: 0o700 }); } catch (e) { if ((e as NodeJS.ErrnoException).code !== 'EEXIST') throw e; }
    const s = await lstat(current); if (!s.isDirectory() || s.isSymbolicLink()) fail();
  }
}
export class ModelSettingsStore {
  readonly authorityFile: string;
  constructor(readonly directory: string, private readonly encryption: CredentialEncryption, private readonly acquireLock: () => Promise<() => Promise<void>>) { if (!path.isAbsolute(directory)) fail(); this.authorityFile = path.join(directory, 'settings.json'); }
  private async readDisk(): Promise<Disk> {
    try {
      await secureDirectory(this.directory);
      const stat = await lstat(this.authorityFile); if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size > LIMIT) fail();
      const bytes = await readFile(this.authorityFile); if (bytes.length > LIMIT) fail();
      const parsed = JSON.parse(bytes.toString('utf8')) as Disk; validate(parsed); return parsed;
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code === 'ENOENT') {
        try { await lstat(path.join(this.directory, '.initialized')); } catch (markerError) { if ((markerError as NodeJS.ErrnoException).code === 'ENOENT') return blank(); }
      }
      throw new Error('model_settings_unavailable');
    }
  }
  private async publish(d: Disk, isCurrent: () => boolean = () => true): Promise<void> {
    validate(d); const data = Buffer.from(JSON.stringify(d)); if (data.length > LIMIT) fail();
    await secureDirectory(this.directory);
    const marker = path.join(this.directory, '.initialized');
    try { const f = await open(marker, 'wx', 0o600); try { await f.writeFile('model-settings-v1'); await f.sync(); } finally { await f.close(); } }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error; const s = await lstat(marker); if (!s.isFile() || s.isSymbolicLink() || s.nlink !== 1) fail(); }
    const temp = path.join(this.directory, `.settings-${randomBytes(16).toString('hex')}.tmp`);
    try { const f = await open(temp, 'wx', 0o600); try { await f.writeFile(data); await f.sync(); } finally { await f.close(); } if (!isCurrent()) throw new Error('model_check_cancelled'); await rename(temp, this.authorityFile); }
    finally { await unlink(temp).catch(() => {}); }
  }
  async read(): Promise<ModelSettingsSnapshot> { return publicSnapshot(await this.readDisk()); }
  async save(input: ModelSettingsSaveInput): Promise<ModelSettingsSnapshot> {
    if (!input || !rev(input.expected_directory_revision) || !Array.isArray(input.profiles) || input.profiles.length > 32 || !input.routes || !input.credential_changes || typeof input.credential_changes !== 'object') fail();
    await secureDirectory(this.directory); const release = await this.acquireLock();
    try {
      const old = await this.readDisk(); if (old.directory_revision !== input.expected_directory_revision) throw new Error('model_settings_conflict');
      const d = structuredClone(old); const ids = new Set<string>(); const changedRefs = new Set<string>();
      const profiles: ModelProfile[] = [];
      for (const edit of input.profiles) {
        if (!edit || !idPattern.test(edit.profile_id) || ids.has(edit.profile_id) || !cleanString(edit.name, 200) || !['chat-completions', 'anthropic'].includes(edit.protocol) || !endpoint(edit.base_url) || !cleanString(edit.model, 200) || edit.credential_ref !== null && !refPattern.test(edit.credential_ref)) fail();
        ids.add(edit.profile_id); const prior = old.profiles.find(p => p.profile_id === edit.profile_id);
        let ref = edit.credential_ref;
        if (ref && !old.encrypted_keys[ref]) fail();
        const action = input.credential_changes[edit.profile_id] ?? { action: 'keep' };
        if (!action || !['keep', 'replace', 'clear'].includes(action.action)) fail();
        if (action.action === 'replace') {
          if (!cleanString(action.key, 7000) || Buffer.byteLength(action.key) > 7000 || !this.encryption.isEncryptionAvailable()) fail();
          ref = ref ?? randomBytes(32).toString('hex'); if (changedRefs.has(ref)) fail();
          const encrypted = await this.encryption.encryptString(action.key); if (!Buffer.isBuffer(encrypted) || !encrypted.length) fail(); d.encrypted_keys[ref] = encrypted.toString('base64'); changedRefs.add(ref);
        } else if (action.action === 'clear') { ref = null; }
        const configChanged = !prior || prior.protocol !== edit.protocol || prior.base_url !== edit.base_url || prior.model !== edit.model;
        const credentialChanged = !prior || prior.credential_ref !== ref;
        profiles.push({ ...edit, credential_ref: ref, has_key: !!ref, config_revision: (prior?.config_revision ?? 0) + Number(configChanged), credential_revision: (prior?.credential_revision ?? 0) + Number(credentialChanged), capability_revision: prior?.capability_revision ?? 0, capabilities: prior?.capabilities ?? emptyCaps() });
      }
      if (Object.keys(input.credential_changes).some(id => !ids.has(id))) fail();
      for (const p of profiles) {
        const prior = old.profiles.find(item => item.profile_id === p.profile_id);
        const refChanged = !!p.credential_ref && changedRefs.has(p.credential_ref);
        if (refChanged && prior && prior.credential_ref === p.credential_ref) p.credential_revision++;
        if (!prior || p.config_revision !== prior.config_revision || p.credential_revision !== prior.credential_revision) { p.capabilities = emptyCaps(); p.capability_revision++; }
      }
      for (const purpose of purposes) { const selected = input.routes[purpose]; if (selected !== null && (typeof selected !== 'string' || !ids.has(selected))) fail(); d.routes[purpose] = { profile_id: selected, route_revision: old.routes[purpose].route_revision + Number(selected !== old.routes[purpose].profile_id) }; }
      d.profiles = profiles; d.directory_revision++;
      for (const ref of Object.keys(d.encrypted_keys)) if (!profiles.some(p => p.credential_ref === ref)) delete d.encrypted_keys[ref];
      await this.publish(d); return publicSnapshot(d);
    } finally { await release(); }
  }
  async runtime(): Promise<ModelRuntimePacket> {
    await secureDirectory(this.directory);
    const release = await this.acquireLock();
    let d: Disk;
    try {
      d = await this.readDisk();
      try { await lstat(this.authorityFile); } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; await this.publish(d); }
    } finally { await release(); }
    const credentials: Record<string, string> = {};
    for (const id of new Set(purposes.map(p => d.routes[p].profile_id).filter((v): v is string => !!v))) {
      const profile = d.profiles.find(p => p.profile_id === id)!;
      if (profile.credential_ref) { if (!this.encryption.isEncryptionAvailable()) fail(); credentials[id] = await this.encryption.decryptString(Buffer.from(d.encrypted_keys[profile.credential_ref], 'base64')); }
    }
    return { format: 'model-runtime-v1', authority_file: this.authorityFile, settings: publicSnapshot(d), credentials };
  }
  /** Private check-process input, including only the requested profile's credential. */
  async checkProfile(profileId: string): Promise<{ profile: ModelProfile; api_key: string }> {
    const d = await this.readDisk(); const profile = d.profiles.find(p => p.profile_id === profileId);
    if (!profile || !profile.credential_ref || !this.encryption.isEncryptionAvailable()) throw new Error('model_check_not_configured');
    const api_key = await this.encryption.decryptString(Buffer.from(d.encrypted_keys[profile.credential_ref], 'base64'));
    return { profile: structuredClone(profile), api_key };
  }
  async recordCapabilities(profileId: string, configRevision: number, credentialRevision: number, capabilities: Partial<Record<ModelCapability, ModelCapabilityState>>, isCurrent: () => boolean = () => true): Promise<ModelSettingsSnapshot> {
    await secureDirectory(this.directory); const release = await this.acquireLock();
    try { const d = await this.readDisk(); const p = d.profiles.find(x => x.profile_id === profileId); if (!p || p.config_revision !== configRevision || p.credential_revision !== credentialRevision) throw new Error('model_settings_stale_check');
      if (!capabilities || Object.keys(capabilities).some(k => !MODEL_CAPABILITIES.includes(k as ModelCapability))) fail();
      if (!isCurrent()) throw new Error('model_check_cancelled');
      p.capabilities = { ...p.capabilities, ...capabilities }; p.capability_revision++; d.directory_revision++; await this.publish(d, isCurrent); return publicSnapshot(d);
    } finally { await release(); }
  }
}
let modelDirectoryPromise: Promise<string> | undefined;
export function resolveModelSettingsDirectory(): Promise<string> {
  return modelDirectoryPromise ??= (async () => {
    try { return path.join(await resolveWindowsUserLocalAppData(), 'AlibabaReceptionWorkbenchShared', 'models'); }
    catch { modelDirectoryPromise = undefined; throw new Error('model_settings_unavailable'); }
  })();
}
export async function createModelSettingsStore(): Promise<ModelSettingsStore> {
  const directory = await resolveModelSettingsDirectory(); return new ModelSettingsStore(directory, createWindowsUserEncryption(), () => acquireMerchantRegistryMutex(directory, 10000));
}
