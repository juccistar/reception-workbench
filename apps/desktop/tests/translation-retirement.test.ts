// @vitest-environment node
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, expect, it } from 'vitest';
import { removeLegacyTranslationFiles } from '../src/main/translation-retirement';

const roots: string[] = [];
afterEach(async () => { for (const root of roots.splice(0)) await rm(root, { recursive: true, force: true }); });
async function directory() { const root = await mkdtemp(path.join(os.tmpdir(), 'translation-retirement-')); roots.push(root); return root; }

it('removes only old local credential names and preserves other mode and modern files', async () => {
  const root = await directory();
  const live = path.join(root, 'live'); const demo = path.join(root, 'demo');
  await mkdir(live); await mkdir(demo);
  const credential = 'a'.repeat(64);
  await writeFile(path.join(live, credential), 'encrypted');
  await writeFile(path.join(live, `.${'b'.repeat(64)}.tmp`), 'encrypted');
  await writeFile(path.join(live, 'models.json'), 'modern');
  await writeFile(path.join(demo, credential), 'other mode');
  await removeLegacyTranslationFiles(live, false);
  await expect(readFile(path.join(live, credential))).rejects.toMatchObject({ code: 'ENOENT' });
  await expect(readFile(path.join(live, `.${'b'.repeat(64)}.tmp`))).rejects.toMatchObject({ code: 'ENOENT' });
  expect(await readFile(path.join(live, 'models.json'), 'utf8')).toBe('modern');
  expect(await readFile(path.join(demo, credential), 'utf8')).toBe('other mode');
});

it('removes only the fixed shared reference', async () => {
  const root = await directory();
  const name = createHash('sha256').update('reception.shared.translation.v1').digest('hex');
  await writeFile(path.join(root, name), 'encrypted');
  await writeFile(path.join(root, 'c'.repeat(64)), 'unrelated');
  await removeLegacyTranslationFiles(root, true);
  await expect(readFile(path.join(root, name))).rejects.toMatchObject({ code: 'ENOENT' });
  expect(await readFile(path.join(root, 'c'.repeat(64)), 'utf8')).toBe('unrelated');
});

it('rejects linked directories before deleting files', async () => {
  const root = await directory();
  const actual = path.join(root, 'actual'); await mkdir(actual);
  const name = 'd'.repeat(64); await writeFile(path.join(actual, name), 'encrypted');
  const link = path.join(root, 'link'); await symlink(actual, link, 'junction');
  await expect(removeLegacyTranslationFiles(link, false)).rejects.toThrow();
  expect(await readFile(path.join(actual, name), 'utf8')).toBe('encrypted');
});
