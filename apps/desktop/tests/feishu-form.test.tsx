import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Workbench } from '../src/renderer/hooks/useWorkbench';
import { FeishuForm } from '../src/renderer/components/FeishuForm';

function setup(save = vi.fn().mockResolvedValue(true)) {
  Object.defineProperty(window, 'reception', { configurable: true, value: { saveFeishuConfiguration: save, grantFeishuWrite: vi.fn() } });
  const model = { snapshot: { settings: { feishu: {} }, checks: {} }, connected: true, setFormDirty: vi.fn(), registerFormSaver: vi.fn(() => () => {}), busy: () => false, mutate: vi.fn(), runJob: vi.fn() } as unknown as Workbench;
  render(<FeishuForm model={model} />);
  return { model, save };
}
describe('飞书登记', () => {
  it('saves the secret only through the private bridge', async () => {
    const { model, save } = setup();
    fireEvent.change(screen.getByLabelText('应用 ID'), { target: { value: 'cli_123' } });
    fireEvent.change(screen.getByLabelText('应用密钥'), { target: { value: 'secret' } });
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ app_secret: 'secret', settings: expect.objectContaining({ app_id: 'cli_123' }) })));
    expect(model.mutate).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByLabelText('应用密钥')).toHaveValue(''));
  });
  it('retains input on failure and during an older save', async () => {
    let resolve!: (value: boolean) => void;
    const save = vi.fn(() => new Promise<boolean>(r => { resolve = r; }));
    setup(save);
    fireEvent.change(screen.getByLabelText('应用密钥'), { target: { value: 'first' } });
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
    fireEvent.change(screen.getByLabelText('应用密钥'), { target: { value: 'new' } });
    resolve(true);
    await screen.findByText(/刚输入的修改仍待保存/);
    expect(screen.getByLabelText('应用密钥')).toHaveValue('new');
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
    resolve(false);
    await screen.findByText(/保存失败/);
    expect(screen.getByLabelText('应用密钥')).toHaveValue('new');
  });
});
