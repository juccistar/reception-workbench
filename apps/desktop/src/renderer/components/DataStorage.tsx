import { useEffect, useRef, useState } from 'react';
import { FolderOpen } from 'lucide-react';
import type { ReceptionBridge, StorageInfo, StorageRequest } from '../../shared/api';
import { Button, Feedback, dateTime } from './ui';

export function DataStorage({ bridge }: { bridge?: ReceptionBridge }) {
  const [info, setInfo] = useState<StorageInfo | null>(null);
  const [target, setTarget] = useState('');
  const [message, setMessage] = useState('');
  const [progress, setProgress] = useState('');
  const [working, setWorking] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const locked = useRef(false);
  const available = !!(bridge?.getStorageInfo && bridge.chooseStorageDirectory && bridge.openStorageDirectory && bridge.manageStorage);

  useEffect(() => {
    if (!available || !bridge?.getStorageInfo) return;
    let active = true;
    void bridge.getStorageInfo().then(value => { if (active) { setInfo(value); setTarget(value.configured_root ?? value.current_root); } }).catch(() => { if (active) setMessage('无法读取数据目录信息，请重启桌面应用后重试。'); });
    const unsubscribe = bridge.onEvent(event => {
      if (event.event !== 'storage.progress' || !event.data || typeof event.data !== 'object') return;
      const data = event.data as { message?: unknown; stage?: unknown };
      if (typeof data.message === 'string') setProgress(data.message);
      if (data.stage === 'ready' || data.stage === 'failed') void bridge.getStorageInfo!().then(value => { if (active) setInfo(value); }).catch(() => { if (active) setMessage('无法刷新数据目录状态，请重新打开设置页。'); });
    });
    return () => { active = false; unsubscribe(); };
  }, [bridge, available]);

  const run = async (operation: () => Promise<void>) => {
    if (locked.current || info?.busy || restarting) return;
    locked.current = true; setWorking(true); setMessage(''); setProgress('');
    try { await operation(); } catch { setMessage('操作未能确认完成。若程序正在重启，请重新打开后查看数据目录和最近操作结果；请勿重复提交。'); }
    finally { locked.current = false; setWorking(false); }
  };
  const manage = async (request: StorageRequest) => {
    if (!bridge?.manageStorage) return;
    let result;
    try { result = await bridge.manageStorage(request); }
    catch (error) {
      if (request.action === 'backup') throw error;
      setRestarting(true);
      setMessage('操作结果尚未确认，程序可能正在重启。请重新打开后查看最近操作结果，勿重复提交。');
      return;
    }
    setMessage(result.message);
    if (result.status === 'restarting') { setRestarting(true); return; }
    if (bridge.getStorageInfo) setInfo(await bridge.getStorageInfo());
  };
  const disabled = working || restarting || !!info?.busy || !info;
  return <section className="form-card storage-card" aria-label="数据存储与备份">
    <div className="form-card-heading"><h2><FolderOpen size={18} />数据存储与备份</h2></div>
    {!available ? <Feedback>当前桌面版本不支持数据存储管理，请更新桌面应用。</Feedback> : <>
      {!info && !message && <p className="form-description">正在读取数据目录…</p>}
      {info && <>
        <dl className="storage-details"><div><dt>当前实际数据目录</dt><dd>{info.current_root}</dd></div><div><dt>默认数据目录</dt><dd>{info.default_root}</dd></div><div><dt>当前来源</dt><dd>{info.source === 'argument' ? '启动参数指定（覆盖已配置目录）' : info.source === 'configured' ? '自定义目录' : info.source === 'legacy' ? '原工作区' : '默认目录'}</dd></div><div><dt>最近备份</dt><dd>{info.last_backup ? <>{dateTime(info.last_backup.completed_at)} · {info.last_backup.path}</> : '尚无备份记录'}</dd></div></dl>
        {info.recovery_backup && <p className="form-description">启动恢复已保留原始快照：{info.recovery_backup}。此快照用于故障核查，恢复工作区请使用“最近备份”。</p>}
        {info.error && <Feedback tone="red">{info.error}</Feedback>}
        {info.last_result && <Feedback>{info.last_result.message}</Feedback>}
        <p className="form-description">迁移、打开已有工作区或恢复前，请先保存其他设置和草稿。操作会重启程序，原数据保留；重启后自动接待保持暂停，需重新授权。</p>
        <label>自定义数据目录<div className="input-with-button"><input aria-label="自定义数据目录" value={target} disabled={disabled} onChange={event => setTarget(event.target.value)} placeholder="输入目录或选择文件夹" /><Button disabled={disabled} onClick={() => void run(async () => { const selected = await bridge!.chooseStorageDirectory!('target'); if (selected) setTarget(selected); })}>选择目录</Button></div></label>
        <div className="storage-actions"><Button disabled={disabled || !target.trim()} onClick={() => void run(() => manage({ action: 'migrate', target: target.trim() }))}>迁移当前数据</Button><Button disabled={disabled || !target.trim()} onClick={() => void run(() => manage({ action: 'open', target: target.trim() }))}>打开已有工作区</Button><Button disabled={disabled} onClick={() => void run(() => bridge!.openStorageDirectory!('current'))}>打开当前目录</Button></div>
        <div className="storage-actions"><Button disabled={disabled} onClick={() => void run(async () => { const folder = await bridge!.chooseStorageDirectory!('backup'); if (folder) await manage({ action: 'backup', target: folder }); })}>备份到文件夹</Button><Button disabled={disabled || !info.last_backup} onClick={() => void run(() => bridge!.openStorageDirectory!('backup'))}>打开最近备份</Button><Button disabled={disabled} onClick={() => void run(async () => { const backup = await bridge!.chooseStorageDirectory!('restore'); if (!backup) return; const destination = await bridge!.chooseStorageDirectory!('target'); if (destination) await manage({ action: 'restore', backup, target: destination }); })}>从备份恢复</Button></div>
        <p className="form-description">备份会在所选文件夹下创建带日期的备份。恢复时请选择备份及空的目标目录；若发送账本与现有记录冲突，恢复会被拒绝，请核对原记录后处理。</p>
      </>}
      {(working || info?.busy) && <p role="status">{progress || '正在处理数据，请稍候…'}</p>}
      {restarting && <p role="status">程序可能正在重启，请勿重复操作。</p>}
      {message && <Feedback>{message}</Feedback>}
    </>}
  </section>;
}
