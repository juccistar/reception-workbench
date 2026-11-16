import { act, fireEvent, render, screen, waitFor, within, cleanup } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ExitDialogEvent } from '../src/shared/exit-dialog';
import type { LauncherBridge } from '../src/shared/launcher';
import { App } from '../src/launcher/index';

vi.mock('../src/launcher/WhatsAppAccounts', () => ({ WhatsAppAccounts: () => null }));
const profile = { profile_id: 'example', name: 'Example', protocol: 'chat-completions', base_url: 'https://example.invalid', model: 'synthetic', credential_ref: null, has_key: true, config_revision: 3, credential_revision: 4, capability_revision: 0, capabilities: Object.fromEntries(['text', 'structure', 'single_image', 'multi_image', 'combined', 'translation'].map(k => [k, { status: 'not_checked', checked_at: null }])) };
const snapshot = { format: 'model-settings-v1', directory_revision: 5, profiles: [profile], routes: { reception: { profile_id: null, route_revision: 0 }, translation: { profile_id: null, route_revision: 0 } } };
afterEach(cleanup);
function setup() {
  let close: (event: ExitDialogEvent) => void = () => {};
  const bridge = {
    getInitialPage: vi.fn(async () => 'models'), onPageRequest: vi.fn(() => () => {}), merchants: vi.fn(async () => []),
    modelSettings: vi.fn(async () => structuredClone(snapshot)), saveModelSettings: vi.fn(async () => structuredClone(snapshot)),
    checkModel: vi.fn(async () => structuredClone(snapshot)), respondClose: vi.fn(async () => {}),
    onCloseRequest: (listener: typeof close) => { close = listener; return () => {}; },
  };
  window.launcher = bridge as unknown as LauncherBridge;
  render(<App />);
  return { bridge, requestClose: () => act(() => close({ kind: 'open', request_id: '11111111-1111-4111-8111-111111111111' })) };
}
describe('launcher model confirmations', () => {
  it('requires explicit consent before checking the exact saved revisions', async () => {
    const { bridge } = setup();
    fireEvent.click(await screen.findByText('编辑'));
    fireEvent.click(screen.getByText('连接与能力检查'));
    fireEvent.click(screen.getByText('检查接待'));
    expect(bridge.checkModel).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole('dialog', { name: '模型能力检查' })).getByText('取消'));
    expect(bridge.checkModel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('检查接待'));
    fireEvent.click(within(screen.getByRole('dialog', { name: '模型能力检查' })).getByText('开始检查'));
    await waitFor(() => expect(bridge.checkModel).toHaveBeenCalledWith('example', 'reception', { directory_revision: 5, config_revision: 3, credential_revision: 4 }));
  });
  it('keeps editor and key on cancel or save failure, then saves them before close', async () => {
    const { bridge, requestClose } = setup();
    fireEvent.click(await screen.findByText('编辑'));
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: 'New name' } });
    fireEvent.change(screen.getByPlaceholderText('留空保留已有 Key'), { target: { value: 'synthetic-key' } });
    requestClose();
    fireEvent.click(within(screen.getByRole('dialog', { name: '还有内容未保存' })).getByText('继续编辑'));
    await waitFor(() => expect(bridge.respondClose).toHaveBeenLastCalledWith(expect.objectContaining({ choice: 'cancel' })));
    expect(screen.getByPlaceholderText('留空保留已有 Key')).toHaveValue('synthetic-key');
    bridge.respondClose.mockClear();
    bridge.saveModelSettings.mockRejectedValueOnce(new Error('save failed'));
    requestClose();
    fireEvent.click(within(screen.getByRole('dialog', { name: '还有内容未保存' })).getByText('保存并退出'));
    await waitFor(() => expect(bridge.respondClose).toHaveBeenLastCalledWith(expect.objectContaining({ choice: 'cancel' })));
    expect(screen.getByPlaceholderText('留空保留已有 Key')).toHaveValue('synthetic-key');
    bridge.respondClose.mockClear();
    requestClose();
    fireEvent.click(within(screen.getByRole('dialog', { name: '还有内容未保存' })).getByText('保存并退出'));
    await waitFor(() => expect(bridge.respondClose).toHaveBeenLastCalledWith(expect.objectContaining({ choice: 'save' })));
    expect(bridge.saveModelSettings).toHaveBeenLastCalledWith(expect.objectContaining({ credential_changes: { example: { action: 'replace', key: 'synthetic-key' } }, profiles: [expect.objectContaining({ name: 'New name' })] }));
  });
  it('asks before deleting an API', async () => {
    setup();
    fireEvent.click(await screen.findByText('编辑'));
    fireEvent.click(screen.getByText('删除 API'));
    const dialog = screen.getByRole('dialog', { name: '删除 API' });
    fireEvent.click(within(dialog).getByText('取消'));
    expect(screen.getByDisplayValue('Example')).toBeTruthy();
  });
});
