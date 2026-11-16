import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SnapshotDiagnostics } from '../src/renderer/components/SnapshotDiagnostics';
import { ActivityPage } from '../src/renderer/components/Pages';
import type { Workbench } from '../src/renderer/hooks/useWorkbench';

const diagnostics = { snapshot_difference: 'content', snapshot_message_id: '4397577601655.PNM', snapshot_from: 'buyer', snapshot_sender_role: 'missing', snapshot_content_type: 'string', snapshot_content_length: 0, snapshot_platform_content_length: 24, snapshot_role_equal: true, snapshot_content_equal: false };

describe('safe snapshot mismatch details', () => {
  it('shows the exact platform ID and bounded comparison facts without requiring a body', () => {
    render(<SnapshotDiagnostics diagnostics={diagnostics} />);
    const detail = screen.getByRole('region', { name: '快照核验差异' });
    expect(within(detail).getByText('4397577601655.PNM')).toBeInTheDocument();
    expect(within(detail).getByText('未提供')).toBeInTheDocument();
    expect(within(detail).getByText('0 字符')).toBeInTheDocument();
    expect(within(detail).getByText('24 字符')).toBeInTheDocument();
    expect(within(detail).getByText('不一致', { exact: true })).toBeInTheDocument();
  });

  it('does not render arbitrary strings, nested objects, URLs or unbounded numbers', () => {
    const privateValue = 'PRIVATE_BODY https://private.invalid/token';
    render(<SnapshotDiagnostics diagnostics={{ ...diagnostics, snapshot_message_id: privateValue, snapshot_from: privateValue, snapshot_sender_role: '__proto__', snapshot_content_type: privateValue, snapshot_content_length: 1000001, snapshot_platform_content_length: true, snapshot_role_equal: { content: privateValue }, snapshot_content_equal: privateValue, extra: privateValue, body: privateValue }} />);
    const detail = screen.getByRole('region', { name: '快照核验差异' });
    expect(detail).not.toHaveTextContent('PRIVATE_BODY');
    expect(detail).not.toHaveTextContent('private.invalid');
    expect(detail).not.toHaveTextContent('1000001');
    expect(detail).not.toHaveTextContent('__proto__');
  });

  it.each([undefined, {}, { snapshot_difference: 'PRIVATE_BODY' }, { snapshot_difference: { content: 'PRIVATE_BODY' } }])('keeps old or invalid diagnostics empty', value => {
    render(<SnapshotDiagnostics diagnostics={value} />);
    expect(screen.queryByRole('region', { name: '快照核验差异' })).not.toBeInTheDocument();
  });

  it('does not expose send tasks or their diagnostic details in activity', () => {
    const model = { connected: true, busy: () => false, jobs: [{ job_id: 'synthetic-snapshot-failure', kind: 'automatic_send', status: 'failed', stage: 'snapshot', message: '快照消息与平台消息不一致', created_at: 1794190644082, started_at: 1794190649416, completed_at: 1794190658479, conversation_id: 'synthetic-conversation', case_id: 'synthetic-case', diagnostics, error: { code: 'snapshot_message_mismatch', message: '快照消息与平台消息不一致' } }], snapshot: { conversations: [{ conversation_id: 'synthetic-conversation', customer_name: 'Synthetic buyer' }], outbox: [] } } as unknown as Workbench;
    render(<ActivityPage model={model} />);
    expect(screen.queryByRole('region', { name: '快照核验差异' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /自动发送回复/ })).not.toBeInTheDocument();
    expect(screen.queryByText('Synthetic buyer')).not.toBeInTheDocument();
  });
});
