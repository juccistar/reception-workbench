// @vitest-environment node
import { afterEach, expect, it } from 'vitest';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { LauncherRegistry } from '../src/main/launcher-registry';
import { companyName, launcherCompanyName } from '../src/main/launcher-company-name';

const dirs: string[] = [];
afterEach(async () => { for (const dir of dirs.splice(0)) await rm(dir, { recursive: true, force: true }); });
async function fixture() {
  const base = await mkdtemp(path.join(os.tmpdir(), 'launcher-company-')); dirs.push(base);
  const index = new LauncherRegistry(path.join(base, 'index'), async () => async () => {});
  const input = { seller_id: '123456', name: '商家 3456', registry_directory: path.join(base, 'registry'), state_directory: path.join(base, 'state'), data_root: path.join(base, 'data'), verified_at: 123 };
  return { base, index, input };
}
it('displays explicit pending states for legacy placeholders and unverified hints', () => {
  expect(launcherCompanyName('商家 3456', '123456', true)).toBe('公司名称待读取');
  expect(launcherCompanyName('商家3456', '123456', true)).toBe('公司名称待读取');
  expect(launcherCompanyName('某公司', '123456', false)).toBe('公司名称待授权');
  expect(launcherCompanyName('某公司', '123456', true)).toBe('某公司');
});
it('rejects invalid and control-bearing company names', () => {
  for (const value of ['', '  ', 'bad\nname', 'bad\u007fname', 'x'.repeat(401), null]) expect(companyName(value, '123456')).toBeNull();
  expect(companyName('  示例公司  ', '123456')).toBe('示例公司');
});
it('preserves valid long Unicode names in the index and offline projection', async () => {
  const { index, input } = await fixture();
  const record = await index.registerVerified(input);
  const name = '公司' + '𠮷'.repeat(398);
  expect(companyName(name, input.seller_id)).toBe(name);
  expect(await index.updateCompanyName(record, name)).toBe(true);
  const saved = (await index.list())[0];
  expect(launcherCompanyName(saved.name, saved.seller_id, true)).toBe(name);
  expect(saved.verified_at).toBe(123);
  expect(await index.updateCompanyName(saved, name + '甲')).toBe(false);
});
it('persists delayed verified names without renewing verification and preserves them on reauthorization', async () => {
  const { index, input } = await fixture();
  const record = await index.registerVerified(input);
  expect(await index.updateCompanyName(record, ' 示例公司 ')).toBe(true);
  expect((await index.list())[0]).toMatchObject({ name: '示例公司', verified_at: 123, record_id: record.record_id });
  const renewed = await index.registerVerified({ ...input, verified_at: 124 });
  expect(renewed.name).toBe('示例公司');
  expect(await index.updateCompanyName(record, '过期名称')).toBe(false);
  expect((await index.list())[0].name).toBe('示例公司');
});
it('rejects stale seller and root bindings and does not carry names to moved roots', async () => {
  const { base, index, input } = await fixture();
  const record = await index.registerVerified(input);
  expect(await index.updateCompanyName({ ...record, seller_id: '987654' }, '错误公司')).toBe(false);
  expect(await index.updateCompanyName({ ...record, data_root: path.join(base, 'other') }, '错误公司')).toBe(false);
  await index.updateCompanyName(record, '旧公司');
  const moved = await index.registerVerified({ ...input, data_root: path.join(base, 'other'), verified_at: 124 });
  expect(moved.name).toBe(input.name);
  expect(await index.updateCompanyName(record, '迟到名称')).toBe(false);
});
