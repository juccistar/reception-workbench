import { useEffect, useRef, useState } from 'react';
import type { ReceptionBridge, StorageInfo, StorageRequest } from '../../shared/api';
import { Button, Drawer, Feedback } from './ui';

const backupTime = (value: number) => new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
import './data-storage.css';

type StorageDialog = 'backup' | 'restore' | 'move';

export function DataStorage({ bridge }: { bridge?: ReceptionBridge }) {
  const [info, setInfo] = useState<StorageInfo | null>(null);
  const [dialog, setDialog] = useState<StorageDialog | null>(null);
  const [message, setMessage] = useState('');
  const [progress, setProgress] = useState('');
  const [working, setWorking] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const locked = useRef(false);
  const available = !!(bridge?.getStorageInfo && bridge.chooseStorageDirectory && bridge.openStorageDirectory && bridge.manageStorage);

  useEffect(() => {
    if (!available || !bridge?.getStorageInfo) return;
    let active = true;
    void bridge.getStorageInfo().then(value => { if (active) setInfo(value); }).catch(() => { if (active) setMessage('无法读取数据目录信息，请重启桌面应用后重试。'); });
    const unsubscribe = bridge.onEvent(event => {
      if (event.event !== 'storage.progress' || !event.data || typeof event.data !== 'object') return;
      const data = event.data as { message?: unknown; stage?: unknown };
      if (active && typeof data.message === 'string') setProgress(data.message);
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
  const start = (action: StorageDialog) => {
    setDialog(null);
    void run(async () => {
      if (action === 'backup') {
        const target = await bridge!.chooseStorageDirectory!('backup');
        if (target) await manage({ action: 'backup', target });
      } else if (action === 'restore') {
        const backup = await bridge!.chooseStorageDirectory!('restore');
        if (!backup) return;
        const target = await bridge!.chooseStorageDirectory!('target');
        if (target) await manage({ action: 'restore', backup, target });
      } else {
        const target = await bridge!.chooseStorageDirectory!('target');
        if (target) await manage({ action: 'migrate', target });
      }
    });
  };
  return <section className="form-card data-storage" aria-label="数据与备份">
    <div className="data-storage-body">
      <h2>数据与备份</h2>
      <p className="data-storage-intro">完整保留聊天、图片、采购、回执与发送保护数据。</p>
      {!available ? <Feedback>当前桌面版本不支持数据存储管理，请更新桌面应用。</Feedback> : <>
        {!info && !message && <p>正在读取数据目录…</p>}
        {info && <>
          <label className="data-storage-folder">当前业务文件夹<input aria-label="当前业务文件夹" readOnly value={info.current_root} /></label>
          <p className="data-storage-hint">{info.source === 'argument' ? '启动参数指定的目录' : info.source === 'configured' ? '自定义目录' : info.source === 'legacy' ? '原工作区' : '默认目录'}</p>
          <div className="data-storage-actions">
            <div className="data-storage-row"><div><span>完整备份</span><p>包含账本、回执及有效的未合并写入。</p></div><Button variant="primary" disabled={disabled} onClick={() => setDialog('backup')}>创建备份</Button></div>
            <div className="data-storage-row"><div><span>恢复备份</span><p>选择完整备份，恢复到空文件夹。</p></div><Button disabled={disabled} onClick={() => setDialog('restore')}>恢复备份</Button></div>
            <div className="data-storage-row"><div><span>更换数据文件夹</span><p>保留原数据，正常关闭后再切换。</p></div><Button disabled={disabled} onClick={() => setDialog('move')}>更换位置</Button></div>
          </div>
          <div className="data-storage-recent"><h2>最近备份</h2><div className="data-storage-recent-row"><span>{info.last_backup ? backupTime(info.last_backup.completed_at) : '尚无备份记录'}</span>{info.last_backup && <span className="data-storage-badge">完整备份</span>}</div>
            {info.last_backup && <div className="data-storage-backup-path">{info.last_backup.path} <Button disabled={disabled} onClick={() => void run(() => bridge!.openStorageDirectory!('backup'))}>打开备份</Button></div>}
            <p>登录环境不包含在业务备份中。</p>
          </div>
          {info.recovery_backup && <p className="data-storage-hint">启动恢复已保留原始快照：{info.recovery_backup}。此快照用于故障核查，恢复工作区请使用完整备份。</p>}
          {info.error && <Feedback tone="red">{info.error}</Feedback>}
          {info.last_result && <Feedback>{info.last_result.message}</Feedback>}
        </>}
        {(working || info?.busy) && <p role="status">{progress || '正在处理数据，请稍候…'}</p>}
        {restarting && <p role="status">程序可能正在重启，请勿重复操作。</p>}
        {message && <Feedback>{message}</Feedback>}
      </>}
    </div>
    <div className="data-storage-footer">数据操作完成后会显示结果。</div>
    {dialog && <Drawer side="center" title={dialog === 'backup' ? '创建完整备份' : dialog === 'restore' ? '恢复完整备份' : '更换数据文件夹'} onClose={() => setDialog(null)} className="data-storage-dialog">
      <div className="data-storage-dialog-body">
        {dialog === 'backup' ? <><p>完整备份包含当前商家的聊天、图片、采购资料、账本及回执。</p><p>请选择保存备份的文件夹，程序将在其中创建带日期的备份。</p></> : dialog === 'restore' ? <><p>选择完整业务备份，并恢复到空文件夹。已有业务数据与未决操作不会被覆盖或清除。</p><div className="data-storage-note">恢复前保存编辑、暂停接待并核对备份。若发送账本与现有记录冲突，恢复会被拒绝。</div><p>恢复会重启程序，重启后需要重新授权。</p></> : <><p>先保存编辑、暂停接待并完成完整备份，再切换新的业务文件夹。</p><p>原数据保留，程序会重启，重启后需重新授权。</p></>}
      </div>
      <div className="data-storage-dialog-actions"><Button onClick={() => setDialog(null)}>取消</Button><Button variant="primary" disabled={disabled} onClick={() => start(dialog)}>{dialog === 'backup' ? '创建备份' : dialog === 'restore' ? '选择备份' : '选择文件夹'}</Button></div>
    </Drawer>}
  </section>;
}
