import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TranslationSettingsPanel } from '../src/renderer/components/TranslationSettings';
import type { ReceptionBridge, ServiceEvent } from '../src/shared/api';
import type { TranslationInfo } from '../src/shared/translation';

const info: TranslationInfo = {
  enabled: false, base_url: '', model: '', revision: 3, shared_revision: 5, connection_scope: 'shared', has_key: true, status: 'disabled', message: '',
  usage: { requests: 2, prompt_tokens: 3, completion_tokens: 4, unknown_requests: 1, cache_hits: 5 },
};
function setup(disabled = false) {
  let listener: (event: ServiceEvent) => void = () => {};
  const bridge = {
    getTranslationSettings: vi.fn().mockResolvedValue(info),
    saveTranslationSettings: vi.fn().mockResolvedValue({ ...info, revision: 4, model: 'model-a' }),
    checkTranslationProvider: vi.fn().mockResolvedValue({ job_id: 'synthetic-job' }),
    clearTranslationCache: vi.fn().mockResolvedValue({ removed: 2 }),
    onEvent: vi.fn((callback: typeof listener) => { listener = callback; return () => {}; }),
  };
  const onDirtyChange = vi.fn();
  render(<TranslationSettingsPanel bridge={bridge as unknown as ReceptionBridge} disabled={disabled} onDirtyChange={onDirtyChange} />);
  return { bridge, onDirtyChange, emit: (event: ServiceEvent) => listener(event) };
}

describe('translation settings', () => {
  it('handles missing bridges and displays defaults without exposing a saved key', async () => {
    const view = render(<TranslationSettingsPanel />);
    expect(screen.getByText(/不支持第三方聊天翻译/)).toBeInTheDocument();
    view.unmount();
    setup();
    expect(await screen.findByLabelText('服务地址')).toHaveValue('https://api.deepseek.com/v1');
    expect(screen.getByLabelText('模型')).toHaveValue('');
    expect(screen.getByLabelText('新 API Key')).toHaveAttribute('type', 'password');
    expect(screen.getByLabelText('新 API Key')).toHaveValue('');
    expect(screen.getByText(/买卖双方聊天文字或人工生成\/润色的回复建议，以及最多 2 条前文/)).toBeInTheDocument();
    expect(screen.getByText(/不删除原文/)).toBeInTheDocument();
    expect(screen.getByText(/请求 2 次/)).toBeInTheDocument();
  });

  it('saves with expected revision and keeps later edits and a later key', async () => {
    const { bridge, onDirtyChange } = setup();
    await screen.findByLabelText('模型');
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'model-a' } });
    fireEvent.change(screen.getByLabelText('新 API Key'), { target: { value: 'first-key' } });
    let finish!: (value: TranslationInfo) => void;
    bridge.saveTranslationSettings.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    fireEvent.click(screen.getByText('保存翻译设置'));
    expect(bridge.saveTranslationSettings).toHaveBeenCalledWith(expect.objectContaining({ revision: 3, model: 'model-a', api_key: 'first-key' }));
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'model-b' } });
    fireEvent.change(screen.getByLabelText('新 API Key'), { target: { value: 'later-key' } });
    await act(async () => finish({ ...info, revision: 4, shared_revision: 6, model: 'model-a' }));
    expect(screen.getByLabelText('模型')).toHaveValue('model-b');
    expect(screen.getByLabelText('新 API Key')).toHaveValue('later-key');
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(screen.getByText('保存翻译设置'));
    expect(bridge.saveTranslationSettings).toHaveBeenLastCalledWith(expect.objectContaining({ revision: 4, shared_revision: 6, model: 'model-b', api_key: 'later-key' }));
  });

  it('clears the submitted key if only other fields change during save', async () => {
    const { bridge } = setup();
    await screen.findByLabelText('模型');
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'model-a' } });
    fireEvent.change(screen.getByLabelText('新 API Key'), { target: { value: 'first-key' } });
    let finish!: (value: TranslationInfo) => void;
    bridge.saveTranslationSettings.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    fireEvent.click(screen.getByText('保存翻译设置'));
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'model-b' } });
    await act(async () => finish({ ...info, revision: 4, model: 'model-a' }));
    expect(screen.getByLabelText('新 API Key')).toHaveValue('');
    expect(screen.getByLabelText('模型')).toHaveValue('model-b');
  });

  it('preserves dirty form and expected revision on event refresh', async () => {
    const { bridge, emit } = setup();
    await screen.findByLabelText('模型');
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'my-edit' } });
    bridge.getTranslationSettings.mockResolvedValueOnce({ ...info, revision: 8, model: 'external' });
    act(() => emit({ event: 'translation.settings.changed', data: {} }));
    await waitFor(() => expect(bridge.getTranslationSettings).toHaveBeenCalledTimes(2));
    await act(async () => {});
    expect(screen.getByLabelText('模型')).toHaveValue('my-edit');
    fireEvent.click(screen.getByText('保存翻译设置'));
    expect(bridge.saveTranslationSettings).toHaveBeenCalledWith(expect.objectContaining({ revision: 3, model: 'my-edit' }));
  });

  it('runs checks and cache clearing only on click, and supports clearing the key', async () => {
    const { bridge } = setup();
    await screen.findByLabelText('模型');
    expect(bridge.checkTranslationProvider).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('合成检查'));
    expect(await screen.findByText('已开始合成检查。')).toBeInTheDocument();
    fireEvent.click(screen.getByText('清除聊天译文缓存'));
    expect(await screen.findByText('已清除 2 条聊天译文。')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'model-a' } });
    fireEvent.click(screen.getByText('清除已保存 Key'));
    fireEvent.click(screen.getByText('保存翻译设置'));
    expect(bridge.saveTranslationSettings).toHaveBeenCalledWith(expect.objectContaining({ clear_key: true, api_key: '' }));
  });

  it('saves the local Magpie gateway and explains invalid HTTP addresses without losing input', async () => {
    const { bridge } = setup();
    await screen.findByLabelText('模型');
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'codex/gpt-6-astra' } });
    fireEvent.change(screen.getByLabelText('新 API Key'), { target: { value: 'magpie' } });
    fireEvent.change(screen.getByLabelText('服务地址'), { target: { value: 'http://remote.example.com/v1' } });
    fireEvent.click(screen.getByText('保存翻译设置'));
    expect(await screen.findByText(/服务地址须为有效的 HTTPS API 地址/)).toBeInTheDocument();
    expect(bridge.saveTranslationSettings).not.toHaveBeenCalled();
    expect(screen.getByLabelText('新 API Key')).toHaveValue('magpie');
    fireEvent.change(screen.getByLabelText('服务地址'), { target: { value: 'http://127.0.0.1:3425/v1' } });
    fireEvent.click(screen.getByText('保存翻译设置'));
    expect(await screen.findByText('翻译设置已保存。')).toBeInTheDocument();
    expect(bridge.saveTranslationSettings).toHaveBeenCalledWith(expect.objectContaining({
      base_url: 'http://127.0.0.1:3425/v1', model: 'codex/gpt-6-astra', api_key: 'magpie',
    }));
    expect(screen.getByLabelText('新 API Key')).toHaveValue('');
  });

  it('shows a known host failure through the Electron prefix and preserves edits', async () => {
    const { bridge } = setup();
    await screen.findByLabelText('模型');
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'my-model' } });
    bridge.saveTranslationSettings.mockRejectedValueOnce(new Error("Error invoking remote method 'reception:translation:save': Error: 翻译设置已变化，请保留输入并重新读取。"));
    bridge.getTranslationSettings.mockResolvedValueOnce({ ...info, revision: 8, shared_revision: 6, model: 'latest-model' });
    fireEvent.click(screen.getByText('保存翻译设置'));
    expect(await screen.findByText('翻译设置已变化，请保留输入并重新读取。')).toBeInTheDocument();
    expect(screen.getByLabelText('模型')).toHaveValue('my-model');
    await waitFor(() => expect(screen.getByText('放弃修改')).not.toBeDisabled());
    fireEvent.click(screen.getByText('放弃修改'));
    expect(screen.getByLabelText('模型')).toHaveValue('latest-model');
  });

  it('keeps arbitrary backend errors and credentials out of the error message', async () => {
    const { bridge } = setup();
    await screen.findByLabelText('模型');
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'my-model' } });
    bridge.saveTranslationSettings.mockRejectedValueOnce(new Error('unexpected-provider-body secret-api-key'));
    fireEvent.click(screen.getByText('保存翻译设置'));
    expect(await screen.findByText('保存失败，请检查设置并重试。')).toBeInTheDocument();
    expect(screen.queryByText(/secret-api-key/)).not.toBeInTheDocument();
  });

  it('explains shared credentials and preserves the shared revision through external updates', async () => {
    const { bridge, emit } = setup();
    await screen.findByLabelText('模型');
    expect(screen.getByText(/本机同一 Windows 用户的各商家间自动同步/)).toBeInTheDocument();
    expect(screen.getByText(/更换或清除 Key 会同步影响本机其他商家/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'pending-model' } });
    bridge.getTranslationSettings.mockResolvedValueOnce({ ...info, revision: 8, shared_revision: 6, model: 'external-model' });
    act(() => emit({ event: 'translation.settings_changed', data: {} }));
    await waitFor(() => expect(bridge.getTranslationSettings).toHaveBeenCalledTimes(2));
    await act(async () => {});
    expect(screen.getByLabelText('模型')).toHaveValue('pending-model');
    fireEvent.click(screen.getByText('保存翻译设置'));
    expect(bridge.saveTranslationSettings).toHaveBeenCalledWith(expect.objectContaining({ revision: 3, shared_revision: 5 }));
  });

  it('disables all operations when requested', async () => {
    const { bridge } = setup(true);
    await screen.findByLabelText('模型');
    expect(screen.getByLabelText('模型')).toBeDisabled();
    expect(screen.getByText('合成检查')).toBeDisabled();
    expect(screen.getByText('清除聊天译文缓存')).toBeDisabled();
    expect(screen.getByText('清除已保存 Key')).toBeDisabled();
    expect(bridge.checkTranslationProvider).not.toHaveBeenCalled();
  });
});
