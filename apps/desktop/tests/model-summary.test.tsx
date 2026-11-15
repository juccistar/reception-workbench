import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ModelSummary } from '../src/renderer/components/ModelSummary';
import type { ReceptionBridge } from '../src/shared/api';

describe('global model summary', () => {
  it('shows the selected profile names and opens the launcher without exposing connection fields', async () => {
    const openModelLauncher = vi.fn().mockResolvedValue(undefined);
    const bridge = { openModelLauncher, getModelSummary: vi.fn().mockResolvedValue({
      profiles: [{ profile_id: 'a', name: '接待模型', base_url: 'https://example.invalid', model: 'private-model' }, { profile_id: 'b', name: '翻译模型' }],
      routes: { reception: { profile_id: 'a' }, translation: { profile_id: 'b' } },
    }) } as unknown as ReceptionBridge;
    render(<ModelSummary bridge={bridge} />);
    expect(await screen.findByText(/接待 API：接待模型/)).toBeInTheDocument();
    expect(screen.getByText(/翻译 API：翻译模型/)).toBeInTheDocument();
    expect(screen.queryByText(/example.invalid|private-model/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '在启动台管理' }));
    expect(openModelLauncher).toHaveBeenCalledOnce();
  });
});
