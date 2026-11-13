import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PoliciesPage } from '../src/renderer/components/PolicyLibrary';
import type { Workbench } from '../src/renderer/hooks/useWorkbench';

const builtin = { template_id: 'T01', template_version: 2, kind: 'product', category: 'product', title: '产品模板', content: '产品：[待填写]', usage: 'customer_reference', is_template: true, product_scope: [], adopted_policy_id: null, enabled: false, available_update: false };
const old = { policy_id: 'p1', kind: 'policy', title: '旧资料', content: '原有正文', product_scope: [], confirmed: true, public: false, revision: 3, updated_at: 0, valid_until: null };
function setup(policies: unknown[] = [], catalog: unknown[] = [builtin]) {
  const invoke = vi.fn(async (method: string, params: Record<string, unknown>) => method === 'list_builtin_policies' ? { items: catalog } : { ...old, ...params, policy_id: 'p1', revision: 4 });
  const model = { connected: true, snapshot: { policies }, invoke, action: async (_key: string, fn: () => Promise<unknown>) => fn(), busy: () => false, setFormDirty: vi.fn(), refreshSnapshot: vi.fn(async () => {}) } as unknown as Workbench;
  render(<PoliciesPage model={model} />);
  return { model, invoke };
}

describe('资料与内置内容', () => {
  it('默认关闭，模板先查看再填写，不提交占位内容', async () => {
    const { invoke } = setup();
    fireEvent.click(screen.getByRole('button', { name: '内置内容' }));
    fireEvent.click(await screen.findByRole('button', { name: /产品模板/ }));
    expect(screen.getByText('产品：[待填写]')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '编辑并采用' }));
    expect(screen.getByRole('textbox', { name: '资料内容' })).toHaveValue('');
    expect(screen.getByRole('checkbox', { name: /启用资料/ })).not.toBeChecked();
    fireEvent.change(screen.getByRole('textbox', { name: '资料内容' }), { target: { value: '产品：[待填写]' } });
    fireEvent.click(screen.getByRole('button', { name: '采用资料' }));
    expect(screen.getByText('请删除待填写提示，并填写已核实的实际内容。')).toBeInTheDocument();
    expect(invoke.mock.calls.filter(call => call[0] === 'adopt_builtin_policy')).toHaveLength(0);
    fireEvent.change(screen.getByRole('textbox', { name: '资料内容' }), { target: { value: '面料：棉' } });
    fireEvent.click(screen.getByRole('button', { name: '采用资料' }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('adopt_builtin_policy', expect.objectContaining({ content: '面料：棉', enabled: false, update: false })));
  });
  it('新版先对照本地正文，再以原版本和关闭状态明确更新', async () => {
    const { invoke } = setup([{ ...old, enabled: false }], [{ ...builtin, is_template: false, content: '新版正文', adopted_policy_id: 'p1', available_update: true }]);
    fireEvent.click(screen.getByRole('button', { name: '内置内容' }));
    fireEvent.click(await screen.findByRole('button', { name: /产品模板/ }));
    expect(screen.getByText('原有正文')).toBeInTheDocument();
    expect(screen.getByText('新版正文')).toBeInTheDocument();
    expect(invoke.mock.calls.filter(call => call[0] === 'adopt_builtin_policy')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '编辑新版并更新' }));
    expect(screen.getByRole('checkbox', { name: /启用资料/ })).not.toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: '确认更新资料' }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('adopt_builtin_policy', expect.objectContaining({ content: '新版正文', expected_revision: 3, enabled: false, update: true })));
  });
  it('聊天风格可配置优先级并说明选择规则', async () => {
    const { invoke } = setup([{ ...old, category: 'style', enabled: true, usage: 'internal_guidance', priority: 4 }]);
    fireEvent.click(screen.getByRole('button', { name: /旧资料/ }));
    expect(screen.getByLabelText(/风格优先级/)).toHaveValue(4);
    expect(screen.getByText(/优先级高者优先/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/风格优先级/), { target: { value: '9' } });
    fireEvent.click(screen.getByRole('button', { name: '保存资料' }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('save_policy', expect.objectContaining({ priority: 9 })));
  });
  it('旧非公开资料仅人工查看，保存带版本且保留保存期间输入', async () => {
    const { invoke, model } = setup([old]);
    let resolve!: (value: unknown) => void;
    invoke.mockImplementation(async (method: string) => method === 'save_policy' ? new Promise(done => { resolve = done; }) : { items: [builtin] });
    fireEvent.click(screen.getByRole('button', { name: /旧资料/ }));
    expect(screen.getByLabelText('用途')).toHaveValue('human_only');
    fireEvent.change(screen.getByRole('textbox', { name: '资料内容' }), { target: { value: '第一版' } });
    fireEvent.click(screen.getByRole('button', { name: '保存资料' }));
    expect(invoke).toHaveBeenCalledWith('save_policy', expect.objectContaining({ expected_revision: 3, usage: 'human_only', public: false }));
    fireEvent.change(screen.getByRole('textbox', { name: '资料内容' }), { target: { value: '保存期间的新内容' } });
    resolve({ ...old, content: '第一版', revision: 4 });
    await screen.findByText('前一版已保存，您刚输入的修改仍待保存。');
    expect(screen.getByRole('textbox', { name: '资料内容' })).toHaveValue('保存期间的新内容');
    expect(model.setFormDirty).toHaveBeenLastCalledWith('policy', true);
  });
});
