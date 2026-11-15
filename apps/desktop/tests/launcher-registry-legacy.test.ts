// @vitest-environment node
import { afterEach, expect, it } from 'vitest';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { LauncherRegistry, listLauncherCandidates, readHistoricalMerchants, assertCompleteLauncherSources } from '../src/main/launcher-registry';
import { createLauncherActivation } from '../src/main/launcher-activation';

const dirs: string[] = [];
afterEach(async () => { for (const dir of dirs.splice(0)) await rm(dir, { recursive: true, force: true }); });
async function fixture() {
  const base = await mkdtemp(path.join(os.tmpdir(), 'launcher-legacy-')); dirs.push(base);
  const directory = path.join(base, 'registry'), root = path.join(base, 'data');
  const state = path.join(directory, 'profiles', createHash('sha256').update('123456').digest('hex'), 'state');
  await mkdir(state, { recursive: true }); await mkdir(root);
  const record = { sellerId: '123456', stateDirectory: state, initialDataRoot: root, status: 'verified' };
  await writeFile(path.join(directory, 'merchants.json'), JSON.stringify({ format: 1, profiles: { '123456': record } }));
  await writeFile(path.join(state, 'storage.json'), JSON.stringify({ version: 1, dataRoot: root }));
  const index = new LauncherRegistry(path.join(base, 'launcher'), async () => async () => {});
  return { base, directory, root, state, record, index };
}
it('reads historical routing hints without persisting or claiming verification', async () => {
  const { directory, root, index } = await fixture();
  const before = await readFile(path.join(directory, 'merchants.json'), 'utf8');
  const items = await listLauncherCandidates(index, [directory, directory]);
  expect(items).toHaveLength(1); expect(items[0]).toMatchObject({ seller_id: '123456', data_root: root, verified_at: 0 });
  expect(await index.list()).toEqual([]);
  expect(await readFile(path.join(directory, 'merchants.json'), 'utf8')).toBe(before);
});
it('persists imported hints, revalidates their source and never replaces a verified entry', async () => {
  const { directory, root, state, index } = await fixture();
  const [hint] = await readHistoricalMerchants(directory);
  await index.importHistorical(hint);
  expect((await listLauncherCandidates(index, []))[0]).toMatchObject({ seller_id: '123456', verified_at: 0 });
  const verified = await index.registerVerified({ seller_id: '123456', name: '商家', registry_directory: directory, state_directory: state, data_root: root, verified_at: 123 });
  await index.importHistorical(hint);
  expect((await index.list())[0]).toEqual(verified);
  await rm(path.join(directory, 'merchants.json'));
  expect((await listLauncherCandidates(index, []))[0]).toEqual(verified);
});
it('hides imported hints whose source disappears', async () => {
  const { directory, index } = await fixture();
  const [hint] = await readHistoricalMerchants(directory);
  await index.importHistorical(hint);
  await rm(path.join(directory, 'merchants.json'));
  expect(await listLauncherCandidates(index, [])).toEqual([]);
});
it('uses verified identity for matching current roots and invalidates it on a move', async () => {
  const { base, directory, root, state, index } = await fixture();
  const verified = await index.registerVerified({ seller_id: '123456', name: '商家', registry_directory: directory, state_directory: state, data_root: root, verified_at: 123 });
  expect((await listLauncherCandidates(index, [directory]))[0]).toEqual(verified);
  const moved = path.join(base, 'moved'); await mkdir(moved);
  await writeFile(path.join(state, 'storage.json'), JSON.stringify({ version: 1, dataRoot: moved }));
  expect((await listLauncherCandidates(index, [directory]))[0]).toMatchObject({ data_root: moved, verified_at: 0 });
});
it('keeps historical hints inactive until the matching verified window completes a handshake', async () => {
  const { base, directory, root, state, index } = await fixture();
  const [historical] = await listLauncherCandidates(index, [directory]);
  const activation = createLauncherActivation({ directory: path.join(base, 'activation'), encryption: {
    isEncryptionAvailable: () => true,
    encryptString: async value => Buffer.from(value),
    decryptString: async value => value.toString(),
  }, timeoutMs: 1000 });
  expect(await activation.probe(historical)).toBe(false);
  const current = await index.registerVerified({ seller_id: '123456', name: '商家', registry_directory: directory, state_directory: state, data_root: root, verified_at: 123 });
  let activations = 0;
  const close = await activation.publishVerifiedWindow(current, () => { activations++; });
  try {
    expect(await activation.activate(historical)).toBe(false);
    const [candidate] = await listLauncherCandidates(index, [directory]);
    expect(candidate).toEqual(current);
    expect(await activation.probe(candidate)).toBe(true);
    expect(await activation.activate(candidate)).toBe(true);
    expect(activations).toBe(1);
    const moved = path.join(base, 'moved'); await mkdir(moved);
    await writeFile(path.join(state, 'storage.json'), JSON.stringify({ version: 1, dataRoot: moved }));
    const [changed] = await listLauncherCandidates(index, [directory]);
    expect(changed.verified_at).toBe(0);
    expect(await activation.activate(changed)).toBe(false);
    expect(activations).toBe(1);
  } finally { await close(); }
});
it('does not treat corrupted or missing known sources as an empty reception scope', async () => {
  const { base, directory, index } = await fixture();
  await expect(assertCompleteLauncherSources(index, [directory, path.join(base, 'unused')])).resolves.toBeUndefined();
  const [hint] = await readHistoricalMerchants(directory);
  await index.importHistorical(hint);
  await writeFile(path.join(directory, 'merchants.json'), '{broken');
  await expect(assertCompleteLauncherSources(index, [directory])).rejects.toThrow('完整核查');
  await rm(path.join(directory, 'merchants.json'));
  expect(await listLauncherCandidates(index, [])).toEqual([]);
  await expect(assertCompleteLauncherSources(index, [])).rejects.toThrow('完整核查');
});
it('blocks reception scope when a declared profile is skipped by read-only discovery', async () => {
  const { directory, state, index } = await fixture();
  await rm(path.join(state, 'storage.json'));
  expect(await readHistoricalMerchants(directory)).toEqual([]);
  await expect(assertCompleteLauncherSources(index, [directory])).rejects.toThrow('完整核查');
});
it('ignores broken records and linked metadata', async () => {
  const { base, directory, root, state, record } = await fixture();
  await writeFile(path.join(directory, 'merchants.json'), JSON.stringify({ format: 1, profiles: { '123456': record, '99': { ...record, sellerId: '99' } } }));
  expect(await readHistoricalMerchants(directory)).toHaveLength(1);
  const externalState = path.join(base, 'external-state'); await mkdir(externalState);
  await writeFile(path.join(externalState, 'storage.json'), JSON.stringify({ version: 1, dataRoot: root }));
  await rm(state, { recursive: true });
  await symlink(externalState, state, process.platform === 'win32' ? 'junction' : 'dir');
  expect(await readHistoricalMerchants(directory)).toEqual([]);
});
