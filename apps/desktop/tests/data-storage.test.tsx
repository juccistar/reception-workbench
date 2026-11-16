import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DataStorage } from '../src/renderer/components/DataStorage';
import type { ReceptionBridge, ServiceEvent, StorageInfo } from '../src/shared/api';

const info: StorageInfo = { current_root: 'C:/synthetic/current', default_root: 'C:/synthetic/default', configured_root: null, source: 'argument', config_file: 'C:/synthetic/config', last_backup: null, busy: false };
function setup() {
  let listener: (event: ServiceEvent) => void = () => {};
  const bridge = {
    getStorageInfo: vi.fn().mockResolvedValue(info),
    chooseStorageDirectory: vi.fn(),
    openStorageDirectory: vi.fn().mockResolvedValue(undefined),
    manageStorage: vi.fn().mockResolvedValue({ status: 'cancelled', message: '已取消' }),
    onEvent: vi.fn((callback: typeof listener) => { listener = callback; return () => {}; }),
  } as unknown as ReceptionBridge;
  render(<DataStorage bridge={bridge} />);
  return { bridge, emit: (event: ServiceEvent) => listener(event) };
}

describe('data storage settings', () => {
  it('reports an unavailable bridge and displays the actual directory', async () => {
    const view = render(<DataStorage />);
    expect(screen.getByText(/当前桌面版本不支持/)).toBeInTheDocument();
    view.unmount();
    setup();
    expect(await screen.findByLabelText('当前业务文件夹')).toHaveValue('C:/synthetic/current');
    expect(screen.getByText(/启动参数指定/)).toBeInTheDocument();
    expect(screen.getByText('尚无备份记录')).toBeInTheDocument();
  });
  it('confirms backup before selecting a directory and respects picker cancellation', async () => {
    const { bridge } = setup();
    fireEvent.click(await screen.findByRole('button', { name: '创建备份' }));
    const dialog = screen.getByRole('dialog', { name: '创建完整备份' });
    expect(bridge.chooseStorageDirectory).not.toHaveBeenCalled();
    vi.mocked(bridge.chooseStorageDirectory!).mockResolvedValueOnce(null);
    fireEvent.click(within(dialog).getByRole('button', { name: '创建备份' }));
    await waitFor(() => expect(bridge.chooseStorageDirectory).toHaveBeenCalledWith('backup'));
    expect(bridge.manageStorage).not.toHaveBeenCalled();
  });
  it('migrates only after confirming and choosing a destination', async () => {
    const { bridge } = setup();
    fireEvent.click(await screen.findByRole('button', { name: '更换位置' }));
    vi.mocked(bridge.chooseStorageDirectory!).mockResolvedValueOnce('D:/synthetic/new');
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '选择文件夹' }));
    await waitFor(() => expect(bridge.manageStorage).toHaveBeenCalledWith({ action: 'migrate', target: 'D:/synthetic/new' }));
    expect(await screen.findByText('已取消')).toBeInTheDocument();
  });
  it('chooses backup and empty destination before restore and displays progress while locked', async () => {
    const { bridge, emit } = setup();
    fireEvent.click(await screen.findByRole('button', { name: '恢复备份' }));
    vi.mocked(bridge.chooseStorageDirectory!).mockResolvedValueOnce('D:/synthetic/archive').mockResolvedValueOnce('D:/synthetic/empty');
    let resolve!: (value: { status: 'restarting'; message: string }) => void;
    vi.mocked(bridge.manageStorage!).mockReturnValueOnce(new Promise(r => { resolve = r; }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '选择备份' }));
    await waitFor(() => expect(bridge.manageStorage).toHaveBeenCalledWith({ action: 'restore', backup: 'D:/synthetic/archive', target: 'D:/synthetic/empty' }));
    emit({ event: 'storage.progress', data: { stage: 'copy', message: '正在复制' } });
    expect(await screen.findByText('正在复制')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '更换位置' })).toBeDisabled();
    resolve({ status: 'restarting', message: '即将重启' });
    expect(await screen.findByText('即将重启')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '更换位置' })).toBeDisabled();
  });
});
