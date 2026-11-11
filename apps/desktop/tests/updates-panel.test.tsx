import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UpdatesPanel } from '../src/renderer/components/UpdatesPanel';
import type { ReceptionBridge, ServiceEvent } from '../src/shared/api';
import type { UpdateInfo } from '../src/shared/updates';

const state = (overrides: Partial<UpdateInfo> = {}): UpdateInfo => ({ current_version: '0.2.16', available_version: null, status: 'idle', installed: true, feed_url: 'https://updates.example/', checked_at: null, progress: null, release_notes: '', message: '', ...overrides });
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes; }); return { promise, resolve }; };
function setup(initial = state()) {
  let listener: (event: ServiceEvent) => void = () => {};
  const bridge = {
    onEvent: vi.fn((fn: typeof listener) => { listener = fn; return () => {}; }),
    getUpdateInfo: vi.fn(async () => initial),
    saveUpdateSettings: vi.fn(async ({ feed_url }: { feed_url: string }) => state({ feed_url })),
    checkForUpdates: vi.fn(async () => state({ status: 'available', available_version: '0.2.17', release_notes: '<b>安全更新</b>' })),
    downloadUpdate: vi.fn(async () => state({ status: 'downloaded', available_version: '0.2.17' })),
    installUpdate: vi.fn(async () => state({ status: 'installing' })),
  };
  render(<UpdatesPanel bridge={bridge as unknown as ReceptionBridge} />);
  return { bridge, emit: () => listener({ event: 'updates.changed', data: {} }) };
}

describe('UpdatesPanel', () => {
  it('accepts canonical HTTPS settings and clearing, while rejecting credentials and queries', async () => {
    const { bridge } = setup();
    const input = await screen.findByRole('textbox', { name: 'HTTPS 更新源' });
    const save = screen.getByRole('button', { name: '保存更新源' });
    bridge.saveUpdateSettings.mockResolvedValueOnce(state({ feed_url: 'https://example.com/releases/' }));
    fireEvent.change(input, { target: { value: 'https://EXAMPLE.com:443/releases' } });
    fireEvent.click(save);
    await waitFor(() => expect(screen.queryByText(/更新源已修改/)).not.toBeInTheDocument());
    expect(input).toHaveValue('https://example.com/releases/');
    for (const invalid of ['https://user:pass@example.com/', 'https://example.com/?secret=1', 'https://@example.com/']) {
      fireEvent.change(input, { target: { value: invalid } }); expect(save).toBeDisabled();
    }
    fireEvent.change(input, { target: { value: '' } });
    expect(save).toBeEnabled(); fireEvent.click(save);
    await waitFor(() => expect(bridge.saveUpdateSettings).toHaveBeenLastCalledWith({ feed_url: '' }));
    await waitFor(() => expect(screen.queryByText(/更新源已修改/)).not.toBeInTheDocument());
  });
  it('recognizes and saves a canonical GitHub repository from a releases URL', async () => {
    const { bridge } = setup();
    const input = await screen.findByRole('textbox', { name: 'HTTPS 更新源' });
    expect(screen.getByText('更新源类型：静态 HTTPS 目录')).toBeInTheDocument();
    fireEvent.change(input, { target: { value: 'https://github.com/juccistar/reception-workbench-releases/releases' } });
    expect(screen.getByText('更新源类型：GitHub Releases（juccistar/reception-workbench-releases）')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '保存更新源' }));
    await waitFor(() => expect(bridge.saveUpdateSettings).toHaveBeenCalledWith({ feed_url: 'https://github.com/juccistar/reception-workbench-releases/' }));
    await waitFor(() => expect(input).toHaveValue('https://github.com/juccistar/reception-workbench-releases/'));
    expect(screen.queryByText(/更新源已修改/)).not.toBeInTheDocument();
  });
  it('rejects invalid GitHub paths and keeps later edits during a GitHub save', async () => {
    const { bridge } = setup();
    const input = await screen.findByRole('textbox', { name: 'HTTPS 更新源' });
    const save = screen.getByRole('button', { name: '保存更新源' });
    fireEvent.change(input, { target: { value: 'https://github.com/juccistar/reception-workbench-releases/releases/latest' } });
    expect(save).toBeDisabled();
    expect(screen.getByText('请输入有效的更新源地址。')).toBeInTheDocument();
    expect(bridge.saveUpdateSettings).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: 'https://github.com/juccistar/reception-workbench-releases/releases' } });
    const saving = deferred<UpdateInfo>();
    bridge.saveUpdateSettings.mockReturnValueOnce(saving.promise);
    fireEvent.click(save);
    fireEvent.change(input, { target: { value: 'https://later.example/updates' } });
    await act(async () => saving.resolve(state({ feed_url: 'https://github.com/juccistar/reception-workbench-releases/' })));
    expect(input).toHaveValue('https://later.example/updates');
    expect(screen.getByText('更新源类型：静态 HTTPS 目录')).toBeInTheDocument();
    expect(screen.getByText(/更新源已修改/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '检查更新' })).toBeDisabled();
  });
  it('checks only on click, renders plain text notes and installs after download', async () => {
    const { bridge } = setup();
    await screen.findByText(/当前版本：0.2.16/);
    expect(bridge.checkForUpdates).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '检查更新' }));
    await screen.findByText(/发现新版本/);
    expect(screen.getByText('<b>安全更新</b>').querySelector('b')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '下载更新' }));
    await screen.findByText(/下载完成/);
    fireEvent.click(screen.getByRole('button', { name: '安装并重启' }));
    await waitFor(() => expect(bridge.installUpdate).toHaveBeenCalledOnce());
  });
  it('refreshes progress on events and permits retry after failure', async () => {
    const { bridge, emit } = setup(state({ status: 'available' }));
    await screen.findByText(/发现新版本/);
    bridge.downloadUpdate.mockRejectedValueOnce(new Error('下载失败'));
    fireEvent.click(screen.getByRole('button', { name: '下载更新' }));
    await screen.findByText('操作失败，请重试。');
    expect(screen.getByRole('button', { name: '下载更新' })).toBeEnabled();
    bridge.getUpdateInfo.mockResolvedValue(state({ status: 'downloading', progress: { percent: 45, transferred: 45, total: 100 } }));
    act(emit);
    await screen.findByText('下载进度：45.0%');
    expect(screen.getByRole('progressbar')).toHaveValue(45);
    expect(screen.getByRole('button', { name: '下载更新' })).toBeDisabled();
  });
  it('preserves edits across late refresh and save responses', async () => {
    const { bridge, emit } = setup(state({ status: 'downloaded' }));
    const input = await screen.findByRole('textbox', { name: 'HTTPS 更新源' });
    const pending = deferred<UpdateInfo>();
    bridge.getUpdateInfo.mockReturnValueOnce(pending.promise);
    act(emit);
    fireEvent.change(input, { target: { value: 'https://new.example/' } });
    await act(async () => pending.resolve(state({ status: 'downloaded' })));
    expect(input).toHaveValue('https://new.example/');
    expect(screen.getByRole('button', { name: '安装并重启' })).toBeDisabled();
    const saving = deferred<UpdateInfo>();
    bridge.saveUpdateSettings.mockReturnValueOnce(saving.promise);
    fireEvent.click(screen.getByRole('button', { name: '保存更新源' }));
    fireEvent.change(input, { target: { value: 'https://later.example/' } });
    await act(async () => saving.resolve(state({ feed_url: 'https://new.example/' })));
    expect(input).toHaveValue('https://later.example/');
    expect(screen.getByText(/更新源已修改/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '检查更新' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '保存更新源' }));
    await waitFor(() => expect(screen.queryByText(/更新源已修改/)).not.toBeInTheDocument());
  });
  it('degrades gracefully with an older bridge', () => {
    render(<UpdatesPanel currentVersion="0.2.15" />);
    expect(screen.getByText(/当前桌面版本不支持在线更新/)).toBeInTheDocument();
    expect(screen.getByText(/0.2.15/)).toBeInTheDocument();
  });
});
