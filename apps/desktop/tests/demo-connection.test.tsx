import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DemoConnection } from '../src/renderer/components/DemoConnection';
import type { ReceptionBridge } from '../src/shared/api';
import type { AccioLaunchInfo } from '../src/shared/launch';

const info: AccioLaunchInfo = {
  version: '0.2.15', distribution: 'portable', executable_path: "E:\\商家 文件\\O'Brien\\工作台.exe",
  command: 'host command',
  instructions: "请使用 E:\\商家 文件\\O'Brien\\工作台.exe\n完整指令",
};
const makeBridge = (get = vi.fn().mockResolvedValue(info), copy = vi.fn().mockResolvedValue(info)) => ({ getAccioLaunchInfo: get, copyAccioLaunchInstructions: copy } as unknown as ReceptionBridge);
const open = () => fireEvent.click(screen.getByRole('button', { name: '查看位置与指令' }));

describe('demo connection', () => {
  it('keeps launch instructions available without claiming demo mode when the service is disconnected', async () => {
    const copy = vi.fn().mockResolvedValue(info);
    const inspect = vi.fn();
    render(<DemoConnection bridge={makeBridge(vi.fn().mockResolvedValue(info), copy)} disconnected onInspectConnection={inspect} />);
    expect(screen.getByRole('region', { name: '启动与连接指引' })).toBeInTheDocument();
    expect(screen.queryByText('演示模式')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: '复制 Accio 启动指令' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '复制 Accio 启动指令' }));
    await waitFor(() => expect(copy).toHaveBeenCalledExactlyOnceWith());
    fireEvent.click(screen.getByRole('button', { name: '查看连接诊断' }));
    expect(inspect).toHaveBeenCalledOnce();
  });

  it('loads host paths without copying, then copies full host instructions on request', async () => {
    const get = vi.fn().mockResolvedValue(info); const copy = vi.fn().mockResolvedValue(info);
    render(<DemoConnection bridge={makeBridge(get, copy)} />);
    await waitFor(() => expect(get).toHaveBeenCalledOnce());
    expect(copy).not.toHaveBeenCalled();
    open();
    expect(await screen.findByText(info.executable_path)).toBeInTheDocument();
    expect(screen.queryByText(/数据目录：/)).not.toBeInTheDocument();
    expect(screen.getByText(/执行指令的 Accio 授权商家决定工作区/)).toBeInTheDocument();
    expect(screen.getByText('便携版')).toBeInTheDocument();
    expect(screen.getByText(info.version)).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '启动指令' })).toHaveValue(info.instructions);
    fireEvent.click(screen.getByRole('button', { name: '关闭位置与启动指令' }));
    fireEvent.click(screen.getByRole('button', { name: '复制 Accio 启动指令' }));
    await screen.findByText(/启动指令已复制/);
    expect(copy).toHaveBeenCalledExactlyOnceWith();
  });

  it('keeps the instructions selectable if native clipboard copying fails', async () => {
    const copy = vi.fn().mockRejectedValue(new Error('private error'));
    render(<DemoConnection bridge={makeBridge(vi.fn().mockResolvedValue(info), copy)} />);
    await waitFor(() => expect(screen.getByRole('button', { name: '复制 Accio 启动指令' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '复制 Accio 启动指令' }));
    await screen.findByText(/复制失败/);
    const instructions = screen.getByRole('textbox', { name: '启动指令' });
    expect(instructions).toHaveValue(info.instructions);
    expect(instructions).toHaveAttribute('readonly');
    expect(screen.queryByText(/private error/)).not.toBeInTheDocument();
  });

  it('allows retrying failed metadata loading', async () => {
    const get = vi.fn().mockRejectedValueOnce(new Error('failure')).mockResolvedValueOnce(info);
    render(<DemoConnection bridge={makeBridge(get)} />);
    await screen.findByText(/无法读取启动信息/);
    open();
    fireEvent.click(screen.getByRole('button', { name: '重新读取启动信息' }));
    expect(await screen.findByText(info.executable_path)).toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(2);
  });

  it('explains missing support in an older bridge', () => {
    render(<DemoConnection bridge={{} as ReceptionBridge} />);
    expect(screen.getByRole('button', { name: '复制 Accio 启动指令' })).toBeDisabled();
    open();
    expect(screen.getByText(/查看帮助菜单/)).toBeInTheDocument();
  });

  it('ignores an old bridge response after the bridge changes', async () => {
    let resolve!: (value: AccioLaunchInfo) => void;
    const old = new Promise<AccioLaunchInfo>(done => { resolve = done; });
    const fresh = { ...info, executable_path: '新程序位置' };
    const firstGet = vi.fn().mockReturnValue(old);
    const { rerender } = render(<DemoConnection bridge={makeBridge(firstGet)} />);
    await waitFor(() => expect(firstGet).toHaveBeenCalledOnce());
    rerender(<DemoConnection bridge={makeBridge(vi.fn().mockResolvedValue(fresh))} />);
    open();
    await screen.findByText('新程序位置');
    await act(async () => { resolve(info); await old; });
    expect(screen.getByText('新程序位置')).toBeInTheDocument();
    expect(screen.queryByText(info.executable_path)).not.toBeInTheDocument();
  });
});
