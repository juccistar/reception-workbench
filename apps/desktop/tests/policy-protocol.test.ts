import { describe, expect, it } from 'vitest';
import { validateChatParameters } from '../src/shared/chat-protocol';

describe('policy and assistance bridge boundaries', () => {
  it('accepts explicit internal usage without converting it to public material', () => {
    expect(() => validateChatParameters('save_policy', { kind: 'policy', title: 'Reception', content: 'Local guidance', product_scope: [], confirmed: true, public: false, enabled: true, usage: 'internal_guidance', category: 'reception', customer_wording: '', internal_notes: '' })).not.toThrow();
  });
  it('rejects arbitrary template metadata and unknown policy operations', () => {
    expect(() => validateChatParameters('adopt_builtin_policy', { template_id: 'B01', enabled: true, template_version: 100, update: false })).toThrow();
    expect(() => validateChatParameters('list_builtin_policies', { execute: 'send' })).toThrow();
  });
  it('requires a revision and mutation id before recording processing', () => {
    expect(() => validateChatParameters('start_assistance_task', { task_id: 'a', expected_revision: 1, client_mutation_id: 'start-1' })).not.toThrow();
    expect(() => validateChatParameters('start_assistance_task', { task_id: 'a', expected_revision: 1 })).toThrow();
    expect(() => validateChatParameters('start_assistance_task', { task_id: 'a', expected_revision: 1.5, client_mutation_id: 'start-1' })).toThrow();
  });
});
