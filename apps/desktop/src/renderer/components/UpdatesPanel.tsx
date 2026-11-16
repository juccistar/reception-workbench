import { useEffect, useRef, useState } from 'react';
import { parseUpdateSource } from '../../../../../scripts/update-source.mjs';
import type { ReceptionBridge } from '../../shared/api';
import type { UpdateInfo, UpdateStatus } from '../../shared/updates';
import { Button, Feedback } from './ui';
import './updates-panel.css';

function getSource(value: string) {
  try { return parseUpdateSource(value); } catch { return null; }
}

function normalizeFeed(value: string): string | null {
  return getSource(value)?.url ?? null;
}

const labels: Record<UpdateStatus, string> = { unavailable: '不可用', idle: '等待检查', checking: '正在检查', available: '发现新版本', downloading: '正在下载', downloaded: '下载完成', installing: '正在安装', error: '操作失败' };

export function UpdatesPanel({ bridge, currentVersion }: { bridge?: ReceptionBridge; currentVersion?: string }) {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [feed, setFeed] = useState('');
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const editVersion = useRef(0);
  const dirtyRef = useRef(false);
  const lock = useRef(false);
  const request = useRef(0);
  const active = useRef(false);

  useEffect(() => {
    active.current = true;
    setInfo(null); setError(''); setFeed(''); setDirty(false); dirtyRef.current = false;
    const refresh = () => {
      if (!bridge?.getUpdateInfo) return;
      const id = ++request.current;
      void bridge.getUpdateInfo().then(value => {
        if (!active.current || id !== request.current) return;
        setInfo(value);
        if (!dirtyRef.current) setFeed(value.feed_url);
      }).catch(() => { if (active.current && id === request.current) setError('无法读取更新状态，请重试。'); });
    };
    refresh();
    const unsubscribe = bridge?.onEvent(event => { if (event.event === 'updates.changed') refresh(); });
    return () => { active.current = false; request.current++; unsubscribe?.(); };
  }, [bridge]);

  const run = async (operation: () => Promise<UpdateInfo>, saving = false) => {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError('');
    const editAtStart = editVersion.current;
    const id = ++request.current;
    try {
      const value = await operation();
      if (!active.current) return;
      const saved = saving && value.status !== 'error' && editAtStart === editVersion.current && normalizeFeed(value.feed_url) === normalizeFeed(feed) && normalizeFeed(feed) !== null;
      if (saved) {
        dirtyRef.current = false; setDirty(false); setFeed(value.feed_url);
        request.current++;
      }
      if (saved || id === request.current) {
        setInfo(value);
        if (!dirtyRef.current) setFeed(value.feed_url);
      }
    } catch {
      if (active.current) setError('操作失败，请重试。');
    } finally {
      lock.current = false;
      if (active.current) setBusy(false);
    }
  };
  const updating = busy || info?.status === 'checking' || info?.status === 'downloading' || info?.status === 'installing';
  const source = getSource(feed);
  const validFeed = source !== null;
  const percent = Math.max(0, Math.min(100, info?.progress?.percent ?? 0));
  return <section className="updates-panel form-card" aria-label="版本与更新">
    <h2>版本与更新</h2>
    <p className="updates-intro">查看当前版本和更新状态。</p>
    <div className="updates-line">
      <div><strong>客户接待工作台</strong><p>当前版本：{info?.current_version || currentVersion || '未知'}</p></div>
      {info && <span className="updates-version-badge">{info.installed ? '已安装' : '便携版'}</span>}
    </div>
    <div className="updates-line">
      <div><strong>检查更新</strong><p>从配置的发行来源读取更新。</p></div>
      {bridge?.getUpdateInfo && <Button disabled={!info || !!updating || dirty || !bridge.checkForUpdates} onClick={() => void run(() => bridge.checkForUpdates!())}>检查更新</Button>}
    </div>
    {!bridge?.getUpdateInfo ? <Feedback>当前桌面版本不支持在线更新，请安装支持更新的桌面版本。</Feedback> : <>
      {!info && !error && <p role="status">正在读取更新状态…</p>}
      {info && <>
        <p className="updates-status" role="status">更新状态：{labels[info.status]}{info.available_version && ` · ${info.available_version}`}</p>
        {info.message && <Feedback tone={info.status === 'error' ? 'red' : 'amber'}>{info.message}</Feedback>}
        {info.status !== 'idle' && info.status !== 'unavailable' && <div className="updates-actions">
          {['available', 'downloading', 'error'].includes(info.status) && <Button disabled={!!updating || dirty || !bridge.downloadUpdate || !['available', 'error'].includes(info.status)} onClick={() => void run(() => bridge.downloadUpdate!())}>下载更新</Button>}
          {['downloaded', 'installing'].includes(info.status) && <Button variant="primary" disabled={!!updating || dirty || !bridge.installUpdate || info.status !== 'downloaded'} onClick={() => void run(() => bridge.installUpdate!())}>安装并重启</Button>}
        </div>}
        {info.progress && <div className="updates-progress"><label htmlFor="update-progress">下载进度：{percent.toFixed(1)}%</label><progress id="update-progress" max={100} value={percent} /><p>{info.progress.transferred} / {info.progress.total} 字节</p></div>}
        {info.release_notes && <div className="updates-notes"><strong>更新说明</strong><p>{info.release_notes}</p></div>}
        <div className="updates-note">更新前请保存编辑。程序会先暂停接待，等待任务结束并完成备份，重启后需重新授权。</div>
        <details className="updates-advanced"><summary>更新来源设置</summary>
          <label className="updates-feed">更新源地址<input aria-label="HTTPS 更新源" type="url" value={feed} onChange={event => { editVersion.current++; dirtyRef.current = true; setDirty(true); setFeed(event.target.value); }} placeholder="https://github.com/用户名/仓库名" /></label>
          <p>请输入公开 GitHub 仓库或静态更新目录的 HTTPS 地址。留空可清除更新源。</p>
          {source?.kind === 'github' && <p>更新源类型：GitHub Releases（{source.owner}/{source.repo}）</p>}
          {source?.kind === 'generic' && <p>更新源类型：静态 HTTPS 目录</p>}
          {feed.trim() && !source && <p>请输入有效的更新源地址。</p>}
          {dirty && <p>更新源已修改，请先保存。</p>}
          <Button disabled={!!updating || !dirty || !validFeed || !bridge.saveUpdateSettings} onClick={() => void run(() => bridge.saveUpdateSettings!({ feed_url: source!.url }), true)}>保存更新源</Button>
        </details>
      </>}
      {error && <Feedback tone="red">{error}{!info && <Button disabled={busy} onClick={() => void run(() => bridge.getUpdateInfo!())}>重试</Button>}</Feedback>}
    </>}
  </section>;
}
