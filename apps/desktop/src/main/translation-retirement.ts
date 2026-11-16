import { createHash } from 'node:crypto';
import { lstat, readdir, unlink } from 'node:fs/promises';
import path from 'node:path';
import { resolveWindowsUserLocalAppData } from './windows-user-directories';
// @ts-ignore The Windows mutex helper is JavaScript without a declaration.
import { acquireMerchantRegistryMutex } from '../../../../scripts/merchant-mutex.mjs';

const sharedRef = createHash('sha256').update('reception.shared.translation.v1').digest('hex');
const ref = /^[a-f0-9]{64}$/;
const temporary = /^\.[a-f0-9]{64}\.tmp$/;
const missing = (error: unknown) => (error as NodeJS.ErrnoException)?.code === 'ENOENT';

async function checkedDirectory(directory: string): Promise<boolean> {
  const resolved = path.resolve(directory);
  const root = path.parse(resolved).root;
  let current = root;
  for (const part of resolved.slice(root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    try {
      const info = await lstat(current);
      if (info.isSymbolicLink() || !info.isDirectory()) throw new Error('旧翻译目录不可安全清理。');
    } catch (error) { if (missing(error)) return false; throw error; }
  }
  return true;
}

/** Removes only files created by the old credential store, without opening them. */
export async function removeLegacyTranslationFiles(directory: string, shared: boolean): Promise<void> {
  if (!await checkedDirectory(directory)) return;
  const names = (await readdir(directory)).filter(name => temporary.test(name) || (shared ? name === sharedRef : ref.test(name)));
  for (const name of names) {
    const info = await lstat(path.join(directory, name));
    if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1) throw new Error('旧翻译凭据不可安全清理。');
  }
  for (const name of names) {
    if (!await checkedDirectory(directory)) return;
    const filename = path.join(directory, name);
    const info = await lstat(filename).catch(error => { if (missing(error)) return null; throw error; });
    if (!info) continue;
    if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1) throw new Error('旧翻译凭据不可安全清理。');
    await unlink(filename);
  }
}

/** Caller holds the storage operation lock for this state directory. */
export async function retireLegacyTranslation(stateDir: string, mode: 'demo' | 'live'): Promise<void> {
  const shared = path.join(await resolveWindowsUserLocalAppData(), 'AlibabaReceptionWorkbenchShared', 'translation', mode);
  const release = await acquireMerchantRegistryMutex(shared, 10000);
  try {
    await removeLegacyTranslationFiles(shared, true);
    await removeLegacyTranslationFiles(path.join(stateDir, 'translation-credentials', mode), false);
  } finally { await release(); }
}
