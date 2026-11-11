import { render, screen, waitFor, fireEvent } from '@testing-library/react';
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
  it('loads without a backend and reports an unavailable bridge', async () => {
    const view = render(<DataStorage />);
    expect(screen.getByText(/当前桌面版本不支持/)).toBeInTheDocument();
    view.unmount();
    setup();
    expect(await screen.findByText('C:/synthetic/current')).toBeInTheDocument();
    expect(screen.getByText(/启动参数指定/)).toBeInTheDocument();
  });
  it('preserves the typed path when the picker is cancelled and passes migration explicitly', async () => {
    const { bridge } = setup();
    const input = await screen.findByLabelText('自定义数据目录');
    fireEvent.change(input, { target: { value: 'D:/synthetic/new' } });
    vi.mocked(bridge.chooseStorageDirectory!).mockResolvedValueOnce(null);
    fireEvent.click(screen.getByText('选择目录'));
    await waitFor(() => expect(bridge.chooseStorageDirectory).toHaveBeenCalledWith('target'));
    await waitFor(() => expect(screen.getByText('迁移当前数据')).toBeEnabled());
    expect(input).toHaveValue('D:/synthetic/new');
    fireEvent.click(screen.getByText('迁移当前数据'));
    await waitFor(() => expect(bridge.manageStorage).toHaveBeenCalledWith({ action: 'migrate', target: 'D:/synthetic/new' }));
    expect(await screen.findByText('已取消')).toBeInTheDocument();
  });
  it('chooses backup and empty destination before restore and displays progress while locked', async () => {
    const { bridge, emit } = setup();
    await screen.findByText('C:/synthetic/current');
    vi.mocked(bridge.chooseStorageDirectory!).mockResolvedValueOnce('D:/synthetic/archive').mockResolvedValueOnce('D:/synthetic/empty');
    let resolve!: (value: { status: 'restarting'; message: string }) => void;
    vi.mocked(bridge.manageStorage!).mockReturnValueOnce(new Promise(r => { resolve = r; }));
    fireEvent.click(screen.getByText('从备份恢复'));
    await waitFor(() => expect(bridge.manageStorage).toHaveBeenCalledWith({ action: 'restore', backup: 'D:/synthetic/archive', target: 'D:/synthetic/empty' }));
    emit({ event: 'storage.progress', data: { stage: 'copy', message: '正在复制' } });
    expect(await screen.findByText('正在复制')).toBeInTheDocument();
    expect(screen.getByText('迁移当前数据')).toBeDisabled();
    resolve({ status: 'restarting', message: '即将重启' });
    expect(await screen.findByText('即将重启')).toBeInTheDocument();
    expect(screen.getByText('迁移当前数据')).toBeDisabled();
  });
});
