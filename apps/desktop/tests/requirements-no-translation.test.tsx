import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Requirements } from '../src/renderer/components/Reception';
import type { ReceptionBridge } from '../src/shared/api';
import type { Workbench } from '../src/renderer/hooks/useWorkbench';

it('renders canonical Chinese requirements without requesting translation or duplicating the conversation summary', () => {
  const current = {
    case_id: 'case', conversation_id: 'conversation', customer_name: 'Customer',
    procurement_key: 'mugs', category_label: '陶瓷杯', revision: 1, state: 'clarifying',
    requirements_format: 'ai-v1', requirements_revision: 1,
    requirements: {
      product: { value: '陶瓷杯', evidence_ids: ['m1'] },
      quantity: { value: '500件', evidence_ids: ['m1'] },
    },
  };
  const before = structuredClone(current);
  const invoke = vi.fn();
  const bridge = { invoke, onEvent: () => () => {} } as unknown as ReceptionBridge;
  const model = {
    detail: { case: current, cases: [current], conversation: { conversation_id: 'conversation' }, draft: null,
      conversation_summary: { conversation_id: 'conversation', body: '客户需要500件陶瓷杯，目的地待确认。' },
      history: { message: '历史来自平台' } },
    selection: { conversationId: 'conversation', caseId: 'case' }, editor: null,
    snapshot: { app: { mode: 'live', seller_id: 'seller' }, checks: { ai: { status: 'passed' } },
      policies: [], outbox: [], translation: { enabled: true, has_key: true, model: 'configured-model' } },
    connected: true, invoke, bridge,
    selectConversation: vi.fn(), navigate: vi.fn(),
  } as unknown as Workbench;
  render(<Requirements model={model} bridge={bridge} locate={vi.fn()} />);
  expect(screen.getByText('陶瓷杯')).toBeInTheDocument();
  expect(screen.getByText('500件')).toBeInTheDocument();
  expect(screen.queryByText('客户需要500件陶瓷杯，目的地待确认。')).not.toBeInTheDocument();
  expect(screen.queryByRole('combobox', { name: '当前采购事项' })).not.toBeInTheDocument();
  expect(invoke).not.toHaveBeenCalled();
  expect(current).toEqual(before);
});
