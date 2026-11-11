import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Assistance } from '../src/renderer/components/Assistance';
import { CustomerList } from '../src/renderer/components/Reception';
import type { AssistanceTask } from '../src/shared/api';
import type { Workbench } from '../src/renderer/hooks/useWorkbench';

const task: AssistanceTask = { task_id: 't1', seller_id: 's', buyer_id: 'b', conversation_id: 'c', case_id: 'case', kind: 'fact_conflict', status: 'open', title: '核实交期', detail: '请核实', source_message_id: 'm', source_job_id: null, fact_keys: ['delivery'], revision: 3, created_at: 1, updated_at: 1, resolution: '', result_public: false, followup_status: 'not_scheduled' };

describe('assistance continuity', () => {
  it('reuses the mutation id on uncertain submission and never schedules an internal result', async () => {
    const invoke = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ task, followup_id: null, followup_status: 'not_scheduled' });
    const model = { connected: true, invoke, refreshSnapshot: vi.fn(), loadDetail: vi.fn() } as unknown as Workbench;
    render(<Assistance model={model} tasks={[task]} />);
    fireEvent.click(screen.getByText(/待你协助与跟进/));
    fireEvent.change(screen.getByLabelText('核实结果'), { target: { value: '尚待确认' } });
    fireEvent.click(screen.getByText('保存核实结果'));
    await screen.findByText(/保存未确认/);
    fireEvent.click(screen.getByText('保存核实结果'));
    await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
    expect(invoke.mock.calls[0][1]).toMatchObject({ expected_revision: 3, result_public: false, schedule_followup: false });
    expect(invoke.mock.calls[1][1]).toEqual(invoke.mock.calls[0][1]);
    expect(screen.queryByText(/平台已受理跟进/)).not.toBeInTheDocument();
  });
  it('requests a public follow-up without claiming it was sent', async () => {
    const invoke = vi.fn().mockResolvedValue({ task, followup_id: 'f1', followup_status: 'waiting_manual' });
    const model = { connected: true, invoke, refreshSnapshot: vi.fn(), loadDetail: vi.fn() } as unknown as Workbench;
    render(<Assistance model={model} tasks={[task]} />);
    fireEvent.click(screen.getByText(/待你协助与跟进/));
    fireEvent.change(screen.getByLabelText('核实结果'), { target: { value: '已核实的条件' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /核实结果供 AI 回复客户/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /安排自动跟进/ }));
    fireEvent.click(screen.getByText('保存核实结果'));
    expect(await screen.findByText(/等待恢复此客户自动接待/)).toBeInTheDocument();
    expect(invoke.mock.calls[0][1]).toMatchObject({ result_public: true, schedule_followup: true, expected_revision: 3 });
    expect(screen.queryByText(/平台已受理跟进/)).not.toBeInTheDocument();
  });
  it('keeps unfinished verification text through delivery-status revisions and guards navigation', async () => {
    const setFormDirty = vi.fn();
    const model = { connected: true, invoke: vi.fn(), refreshSnapshot: vi.fn(), loadDetail: vi.fn(), setFormDirty } as unknown as Workbench;
    const saved = { ...task, status: 'resolved', resolution: 'previous public result', result_public: true, followup_status: 'queued' };
    const view = render(<Assistance model={model} tasks={[saved]} />);
    fireEvent.click(screen.getByText(/待你协助与跟进/));
    fireEvent.change(screen.getByLabelText('核实结果'), { target: { value: 'unfinished correction' } });
    expect(setFormDirty).toHaveBeenLastCalledWith('assistance:t1', true);
    view.rerender(<Assistance model={model} tasks={[{ ...saved, revision: 4, followup_status: 'sending' }]} />);
    expect(screen.getByLabelText('核实结果')).toHaveValue('unfinished correction');
    fireEvent.click(screen.getByText('放弃本次核实修改'));
    await waitFor(() => expect(setFormDirty).toHaveBeenLastCalledWith('assistance:t1', false));
    expect(screen.getByLabelText('核实结果')).toHaveValue('previous public result');
  });
  it('lets a saved public result be scheduled later without editing its content', async () => {
    const saved = { ...task, status: 'resolved', resolution: 'confirmed result', result_public: true };
    const invoke = vi.fn().mockResolvedValue({ task: saved, followup_id: 'later', followup_status: 'queued' });
    const model = { connected: true, invoke, refreshSnapshot: vi.fn(), loadDetail: vi.fn() } as unknown as Workbench;
    render(<Assistance model={model} tasks={[saved]} />);
    fireEvent.click(screen.getByText(/待你协助与跟进/));
    expect(screen.getByText('保存核实结果')).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: /安排自动跟进/ }));
    fireEvent.click(screen.getByText('保存核实结果'));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('save_assistance_result', expect.objectContaining({ result: 'confirmed result', schedule_followup: true, expected_revision: 3 })));
  });
  it('filters assistance separately from actual human ownership', () => {
    const item = (id: string, owned: boolean, count: number) => ({ conversation_id: id, case_id: id, customer_name: id, country: '', last_message: '', last_timestamp: 0, state: 'clarifying', pending_count: 0, unread_count: 0, human_owned: owned, assistance_open_count: count });
    const model = { snapshot: { conversations: [item('协助客户', false, 1), item('接管客户', true, 0)] }, connected: true, loading: false, busy: () => false, selection: {}, selectConversation: vi.fn() } as unknown as Workbench;
    render(<CustomerList model={model} />);
    fireEvent.click(screen.getByRole('button', { name: '待你协助' }));
    expect(screen.getByText('协助客户')).toBeInTheDocument();
    expect(screen.queryByText('接管客户')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '人工接管' }));
    expect(screen.getByText('接管客户')).toBeInTheDocument();
    expect(screen.queryByText('协助客户')).not.toBeInTheDocument();
  });
});
