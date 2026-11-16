export type ExitDialogChoice = 'save' | 'cancel';
export type ExitDialogRequest = { request_id: string };
export type ExitDialogResponse = ExitDialogRequest & { choice: ExitDialogChoice };
export type ExitDialogEvent = { kind: 'open' | 'dismiss'; request_id: string; reason?: 'save_failed' };

export function isExitDialogResponse(value: unknown): value is ExitDialogResponse {
  if (!value || typeof value !== 'object') return false;
  const response = value as Record<string, unknown>;
  return Object.keys(response).sort().join(',') === 'choice,request_id'
    && typeof response.request_id === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(response.request_id)
    && (response.choice === 'save' || response.choice === 'cancel');
}
