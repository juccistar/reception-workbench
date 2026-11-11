import { useEffect, useRef, useState } from 'react';
import type { AssistanceTask } from '../../shared/api';
import type { Workbench } from '../hooks/useWorkbench';
import { Button } from './ui';

const followupLabels: Record<string, string> = {
  not_scheduled: '未安排跟进', queued: '跟进已排队', waiting_authorization: '等待接待授权', waiting_manual: '等待恢复此客户自动接待', waiting_connection: '等待连接恢复', waiting_message_processing: '等待当前客户消息处理', generating: '正在整理跟进回复', sending: '正在发送，等待平台回执', followed_up: '平台已受理跟进', waiting_reconciliation: '等待核查原发送结果', contact_stopped: '客户已要求停止联系', failed: '跟进未完成',
};

function TaskEditor({ task, model }: { task: AssistanceTask; model: Workbench }) {
  const [result, setResult] = useState(task.resolution ?? '');
  const [isPublic, setPublic] = useState(task.result_public ?? false);
  const [schedule, setSchedule] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const basis = useRef({ revision: task.revision, resolution: task.resolution, public: task.result_public });
  const mutation = useRef<{ payload: string; id: string } | null>(null);
  useEffect(() => {
    model.setFormDirty?.(`assistance:${task.task_id}`, dirty || busy);
    return () => model.setFormDirty?.(`assistance:${task.task_id}`, false);
  }, [dirty, busy, model.setFormDirty, task.task_id]);
  useEffect(() => {
    if (!dirty && !busy) {
      setResult(task.resolution ?? ''); setPublic(task.result_public ?? false);
      basis.current = { revision: task.revision, resolution: task.resolution, public: task.result_public };
    } else if (basis.current.resolution === task.resolution && basis.current.public === task.result_public) {
      // A delivery status update can advance the CAS revision without replacing
      // the operator's unfinished verification text.
      basis.current.revision = task.revision;
    }
  }, [task.revision, task.resolution, task.result_public, dirty, busy]);
  const discard = () => {
    setResult(task.resolution ?? ''); setPublic(task.result_public ?? false); setSchedule(false); setDirty(false); setMessage('');
    basis.current = { revision: task.revision, resolution: task.resolution, public: task.result_public };
  };
  const save = async () => {
    if (!result.trim() || busy) return;
    const payload = JSON.stringify({ task_id: task.task_id, expected_revision: basis.current.revision, result: result.trim(), result_public: isPublic, schedule_followup: isPublic && schedule });
    if (mutation.current?.payload !== payload) mutation.current = { payload, id: crypto.randomUUID() };
    setBusy(true); setMessage('');
    try {
      const response = await model.invoke<{ task: AssistanceTask; followup_id: string | null; followup_status: string }>('save_assistance_result', { ...JSON.parse(payload), client_mutation_id: mutation.current.id });
      basis.current = { revision: response.task.revision, resolution: response.task.resolution, public: response.task.result_public };
      setResult(response.task.resolution ?? result.trim()); setPublic(response.task.result_public); setSchedule(false); setDirty(false);
      setMessage(`已保存核实结果 · ${followupLabels[response.followup_status] ?? '跟进状态待确认'}`);
      await model.refreshSnapshot();
      await model.loadDetail(task.conversation_id, task.case_id, true);
    } catch (failure) {
      const text = failure instanceof Error ? failure.message : '';
      setMessage(text.includes('协助任务已变化') ? '任务版本已变化，请保留当前输入，刷新核实后再提交。' : text.includes('操作编号已用于不同结果') ? '提交内容与原操作冲突，请刷新后重新提交。' : '保存未确认。请核对任务状态；重试相同内容不会重复安排跟进。');
    } finally { setBusy(false); }
  };
  const unchanged = task.status === 'resolved' && result.trim() === task.resolution && isPublic === task.result_public;
  return <article className="assistance-task"><strong>{task.title}</strong><p>{task.detail}</p><small>{followupLabels[task.followup_status] ?? '跟进状态待确认'}</small>
    {task.status !== 'open' && task.resolution && <p>{task.result_public ? '可对客核实结果' : '内部核实结果'}：{task.resolution}</p>}
    {task.status !== 'dismissed' && <><label>核实结果<textarea maxLength={1200} disabled={busy} value={result} onChange={event => { setResult(event.target.value); setDirty(true); }} placeholder="填写实际核实结果" /></label>
      <label><input type="checkbox" disabled={busy} checked={isPublic} onChange={event => { setPublic(event.target.checked); if (!event.target.checked) setSchedule(false); setDirty(true); }} />核实结果供 AI 回复客户（不勾选则仅内部使用）</label>
      {isPublic && <><small>安排跟进后，将按当前权限发送这段核实结果。请使用适合客户阅读的表述。</small><label><input type="checkbox" disabled={busy} checked={schedule} onChange={event => { setSchedule(event.target.checked); setDirty(true); }} />安排自动跟进（仍需当前授权和平台回执）</label></>}
      <Button disabled={!model.connected || !result.trim() || busy || (unchanged && (!schedule || task.followup_status === 'followed_up'))} loading={busy} onClick={() => void save()}>保存核实结果</Button>
      {dirty && !busy && <Button variant="ghost" onClick={discard}>放弃本次核实修改</Button>}</>}
    {message && <p role="status">{message}</p>}
  </article>;
}

export function Assistance({ model, tasks }: { model: Workbench; tasks: AssistanceTask[] }) {
  if (!tasks.length) return null;
  return <details className="assistance-panel"><summary>待你协助与跟进（{tasks.filter(task => task.status === 'open').length} 项待处理）</summary>{tasks.map(task => <TaskEditor key={task.task_id} task={task} model={model} />)}</details>;
}
