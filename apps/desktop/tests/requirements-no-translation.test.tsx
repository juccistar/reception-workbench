import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Requirements } from '../src/renderer/components/Reception';
import type { ReceptionBridge } from '../src/shared/api';
import type { Workbench } from '../src/renderer/hooks/useWorkbench';

it('uses Accio-generated Chinese requirement values without requesting translation', () => {
  const current = {
    case_id: 'case', conversation_id: 'conversation', customer_name: 'Customer',
    procurement_key: 'mugs', category_label: '陶瓷杯', revision: 1, state: 'clarifying', missing: ['destination'],
    summary: '客户需要500件陶瓷杯，目的地待确认。',
    facts: { product: { value: 'ceramic mugs', display_value: '陶瓷杯', evidence_ids: ['m1'], status: 'confirmed' },
      quantity: { value: '500 pcs', display_value: '500件', evidence_ids: ['m1'], status: 'confirmed' } },
  };
  const model = {
    detail: { case: current, cases: [current], conversation: { conversation_id: 'conversation' }, draft: null,
      history: { message: '历史来自平台' } },
    selection: { conversationId: 'conversation', caseId: 'case' }, editor: null,
    snapshot: { app: { mode: 'live', seller_id: 'seller' }, checks: { ai: { status: 'passed' } },
      policies: [], outbox: [], translation: { enabled: true, has_key: true, model: 'configured-model' } },
    selectConversation: vi.fn(), navigate: vi.fn(),
  } as unknown as Workbench;
  const invoke = vi.fn();
  const bridge = { invoke, onEvent: () => () => {} } as unknown as ReceptionBridge;
  render(<Requirements model={model} bridge={bridge} locate={vi.fn()} />);
  expect(screen.getByText('客户需要500件陶瓷杯，目的地待确认。')).toBeInTheDocument();
  expect(screen.getByText('陶瓷杯', { selector: '.case-title' })).toBeInTheDocument();
  expect(screen.queryByRole('combobox', { name: '当前采购事项' })).not.toBeInTheDocument();
  expect(screen.getByText('500件')).toBeInTheDocument();
  expect(invoke).not.toHaveBeenCalled();
  expect(current.facts.quantity.value).toBe('500 pcs');
});
