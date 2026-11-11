import { useEffect, useRef, useState } from 'react';
import type { ReceptionBridge } from '../../shared/api';
import type { AccioLaunchInfo } from '../../shared/launch';
import { Button, Drawer, Feedback } from './ui';
import './DemoConnection.css';

const distributions: Record<AccioLaunchInfo['distribution'], string> = { installed: '安装版', portable: '便携版', development: '开发版' };

export function DemoConnection({ bridge, disconnected = false, onInspectConnection }: { bridge?: ReceptionBridge | null; disconnected?: boolean; onInspectConnection?: () => void }) {
  const [info, setInfo] = useState<AccioLaunchInfo | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [copying, setCopying] = useState(false);
  const [message, setMessage] = useState<{ tone: 'red' | 'green' | 'amber'; text: string } | null>(null);
  const generation = useRef(0);
  const request = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    setInfo(null); setMessage(null); setLoading(false); setCopying(false);
    if (bridge?.getAccioLaunchInfo) {
      const id = ++request.current;
      setLoading(true);
      Promise.resolve().then(() => bridge.getAccioLaunchInfo!()).then(value => {
        if (generation.current === current && request.current === id) setInfo(value);
      }).catch(() => {
        if (generation.current === current && request.current === id) setMessage({ tone: 'red', text: '无法读取启动信息，请重试或查看帮助菜单。' });
      }).finally(() => {
        if (generation.current === current && request.current === id) setLoading(false);
      });
    }
    return () => { generation.current++; };
  }, [bridge]);

  async function retry() {
    if (!bridge?.getAccioLaunchInfo) return;
    const current = generation.current; const id = ++request.current;
    setLoading(true); setMessage(null);
    try {
      const value = await bridge.getAccioLaunchInfo();
      if (generation.current === current && request.current === id) setInfo(value);
    } catch {
      if (generation.current === current && request.current === id) setMessage({ tone: 'red', text: '无法读取启动信息，请重试或查看帮助菜单。' });
    } finally {
      if (generation.current === current && request.current === id) setLoading(false);
    }
  }

  async function copy() {
    if (!bridge?.copyAccioLaunchInstructions) return;
    const current = generation.current; const id = ++request.current;
    setLoading(false); setCopying(true); setMessage(null);
    try {
      const value = await bridge.copyAccioLaunchInstructions();
      if (generation.current === current && request.current === id) {
        setInfo(value); setMessage({ tone: 'green', text: '启动指令已复制。重连同一商家前请保存并退出其旧窗口；不同商家可保留各自窗口。' });
      }
    } catch {
      if (generation.current === current && request.current === id) {
        setOpen(true); setMessage({ tone: 'red', text: info ? '复制失败，请在下方手动选择并复制指令；也可重试或查看帮助菜单。' : '暂时无法复制启动指令，请重新读取启动信息，或查看帮助菜单。' });
      }
    } finally {
      if (generation.current === current && request.current === id) setCopying(false);
    }
  }

  const unavailable = !bridge?.getAccioLaunchInfo || !bridge?.copyAccioLaunchInstructions;
  return <section className="demo-connection" aria-label={disconnected ? '启动与连接指引' : '演示模式连接指引'}>
    <div className="demo-connection-bar">
      <p>{disconnected ? <><strong>启动与连接</strong> · 接待服务尚未连接，可查看程序位置或复制启动指令。</> : <><strong>演示模式</strong> · 复制启动指令，在当前商家已授权的 Accio 中连接真实工作台。</>}</p>
      <div className="demo-connection-actions">
        <Button variant="primary" loading={copying} disabled={unavailable || loading} onClick={() => void copy()}>复制 Accio 启动指令</Button>
        <Button onClick={() => setOpen(true)}>查看位置与指令</Button>
        {disconnected && onInspectConnection && <Button onClick={onInspectConnection}>查看连接诊断</Button>}
      </div>
    </div>
    {message && !open && <Feedback tone={message.tone}>{message.text}</Feedback>}
    {open && <Drawer title="位置与启动指令" onClose={() => setOpen(false)} className="demo-connection-drawer">
      <p>重新连接同一商家前，请先保存并正常退出该商家的旧窗口；不同商家可保留各自窗口。新窗口仅验证身份和读取，自动接待保持暂停。</p>
      <p>执行指令的 Accio 授权商家决定工作区和数据目录；同一指令可用于不同商家。</p>
      <p>真实连接会使用当前 Accio 会话的授权环境，请先确认该环境允许启动此工作台。复制按钮只复制指令，不会执行连接。</p>
      {unavailable && <Feedback>当前桌面版本不支持完整的启动指引，请查看帮助菜单。</Feedback>}
      {message && <Feedback tone={message.tone}>{message.text}</Feedback>}
      {loading && <p role="status">正在读取启动信息…</p>}
      {info && <>
        <dl className="demo-connection-details">
          <dt>版本</dt><dd>{info.version}</dd>
          <dt>发行方式</dt><dd>{distributions[info.distribution]}</dd>
          <dt>程序位置</dt><dd>{info.executable_path}</dd>
        </dl>
        <label className="demo-connection-label" htmlFor="demo-launch-instructions">启动指令</label>
        <textarea id="demo-launch-instructions" readOnly rows={10} value={info.instructions} />
      </>}
      <Button variant="primary" loading={copying} disabled={unavailable || loading} onClick={() => void copy()}>复制 Accio 启动指令</Button>
      {bridge?.getAccioLaunchInfo && <Button disabled={loading || copying} onClick={() => void retry()}>重新读取启动信息</Button>}
    </Drawer>}
  </section>;
}
