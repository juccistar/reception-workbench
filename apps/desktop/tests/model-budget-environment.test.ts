// @vitest-environment node
import path from 'node:path';
import { beforeEach, expect, it, vi } from 'vitest';
const resolver = vi.hoisted(() => vi.fn());
vi.mock('../src/main/windows-user-directories', () => ({ resolveWindowsUserLocalAppData: resolver }));
beforeEach(() => { vi.resetModules(); resolver.mockReset(); });
it('overrides inherited budget paths for every child using the real Windows user', async () => {
  const local = path.resolve('synthetic-user-local');
  resolver.mockResolvedValue(local);
  const { modelBudgetEnvironment } = await import('../src/main/model-budget-environment');
  const inherited = { Path: 'system-only', LOCALAPPDATA: 'redirected-accio-local', reception_model_budget_directory: 'other-budget', RECEPTION_MODEL_BUDGET_DIRECTORY: 'another-budget' };
  const results = await Promise.all([modelBudgetEnvironment(inherited), modelBudgetEnvironment(inherited)]);
  for (const result of results) {
    expect(result.RECEPTION_MODEL_BUDGET_DIRECTORY).toBe(path.join(local, 'AlibabaReceptionWorkbenchHost', 'model-budget'));
    expect(result.reception_model_budget_directory).toBeUndefined();
    expect(result.Path).toBe('system-only');
  }
  expect(inherited.RECEPTION_MODEL_BUDGET_DIRECTORY).toBe('another-budget');
  expect(resolver).toHaveBeenCalledOnce();
});
it('does not silently route to redirected data when KnownFolder lookup fails', async () => {
  resolver.mockRejectedValueOnce(new Error('unavailable')).mockResolvedValue(path.resolve('retry-user-local'));
  const { modelBudgetEnvironment } = await import('../src/main/model-budget-environment');
  await expect(modelBudgetEnvironment({ LOCALAPPDATA: 'redirected' })).rejects.toThrow('unavailable');
  await expect(modelBudgetEnvironment({})).resolves.toHaveProperty('RECEPTION_MODEL_BUDGET_DIRECTORY', path.join(path.resolve('retry-user-local'), 'AlibabaReceptionWorkbenchHost', 'model-budget'));
});
