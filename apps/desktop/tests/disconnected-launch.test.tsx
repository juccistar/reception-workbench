import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { App } from '../src/renderer/App';
import type { ReceptionBridge } from '../src/shared/api';
import type { AccioLaunchInfo } from '../src/shared/launch';

const info: AccioLaunchInfo = {
  version: '0.2.21', distribution: 'installed', executable_path: 'C:\\synthetic\\ReceptionWorkbench.exe',
  command: 'synthetic host-owned command', instructions: 'synthetic host-owned launch instructions',
};
function disconnectedBridge() {
  const invoke = vi.fn().mockRejectedValue(new Error('后台服务不可用。'));
  const copy = vi.fn().mockResolvedValue(info);
  const storage = vi.fn().mockResolvedValue({ current_root: 'C:\\synthetic\\data', default_root: 'C:\\synthetic\\data',
    configured_root: null, source: 'default', config_file: 'C:\\synthetic\\storage.json', busy: false,
    last_backup: null, last_result: null, error: '数据目录路径过长，请选择较短的目录。' });
  const bridge = {
    invoke, onEvent: vi.fn(() => () => {}),
    getPreferences: vi.fn().mockResolvedValue({ nav_preference: 'expanded', details_preference: 'expanded', list_width: 272, details_width: 320 }),
    savePreferences: vi.fn().mockResolvedValue(undefined),
    getAccioLaunchInfo: vi.fn().mockResolvedValue(info), copyAccioLaunchInstructions: copy,
    getStorageInfo: storage, chooseStorageDirectory: vi.fn().mockResolvedValue(null),
    openStorageDirectory: vi.fn().mockResolvedValue(undefined), manageStorage: vi.fn().mockResolvedValue({ status: 'failed', message: 'unused' }),
  } as unknown as ReceptionBridge;
  return { bridge, invoke, copy, storage };
}

describe('launch recovery without a backend snapshot', () => {
  it('copies the host launch instruction while the first snapshot fails, without platform actions', async () => {
    const { bridge, invoke, copy } = disconnectedBridge();
    render(<App bridge={bridge} />);
    const guide = await screen.findByRole('region', { name: '启动与连接指引' });
    const button = within(guide).getByRole('button', { name: '复制 Accio 启动指令' });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(copy).toHaveBeenCalledExactlyOnceWith());
    expect(invoke.mock.calls.length).toBeGreaterThan(0);
    expect(invoke.mock.calls.every(([method]) => method === 'get_snapshot')).toBe(true);
    expect(screen.queryByText('演示模式')).not.toBeInTheDocument();
  });

  it('opens local storage diagnostics even though the business backend cannot start', async () => {
    const { bridge, invoke, storage } = disconnectedBridge();
    render(<App bridge={bridge} />);
    fireEvent.click(await screen.findByRole('button', { name: '查看连接诊断' }));
    await waitFor(() => expect(storage).toHaveBeenCalledOnce());
    expect(await screen.findByText('数据目录路径过长，请选择较短的目录。')).toBeInTheDocument();
    expect(invoke.mock.calls.every(([method]) => method === 'get_snapshot')).toBe(true);
  });
});
