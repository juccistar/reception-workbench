import type { ReplyBatchProgress } from './api';
import type { WhatsAppContact, WhatsAppContext, WhatsAppHostInfo } from './whatsapp';

/** Public projections contain routing metadata, never browser credentials or model keys. */
export interface WhatsAppAccountProfile {
  workspace_id: string;
  label: string;
  account_tail: string;
  revision: number;
}
export interface WhatsAppMerchantChoice {
  merchant_id: string;
  name: string;
  account_tail: string;
  available: boolean;
  reason?: string;
}
export interface WhatsAppIdentityInput {
  workspace_id: string;
  expected_account_id: string;
  expected_connection_epoch: string;
}
export interface WhatsAppAccountBinding {
  chat_id: string;
  merchant_id: string | null;
  conversation_id: string | null;
  revision: number;
  segment_id: string;
  requested_binding?: { merchant_id: string | null; conversation_id: string | null } | null;
  requested_binding_revision?: number;
}
export interface WhatsAppAccountConversation extends WhatsAppContact {
  human_owned: boolean;
  reception_paused?: boolean;
  pending: boolean;
  last_message_at?: number;
  error_code?: string | null;
}
export interface WhatsAppAccountOperation {
  operation_id: string;
  chat_id: string;
  merchant_id: string | null;
  status: string;
  message_id: string | null;
  ack: number | null;
}
export interface WhatsAppLegacyStatus {
  status: 'unverified' | 'blocked' | 'ready';
  source_count: number;
  pending_count: number;
  reason: string;
}
export interface WhatsAppAccountState {
  workspace_id: string;
  account_id: string | null;
  connection_epoch: string | null;
  connected: boolean;
  enabled: boolean;
  default_merchant_id: string | null;
  default_revision: number;
  allowed_merchant_ids: string[];
  /** Persisted account selection; independent of the current automatic grant. */
  selected_merchant_ids?: string[];
  conversations: WhatsAppAccountConversation[];
  bindings: WhatsAppAccountBinding[];
  pending_operations: WhatsAppAccountOperation[];
  reply_batches: ReplyBatchProgress[];
  legacy: WhatsAppLegacyStatus;
  model: { ready: boolean; status: string; message: string };
}
export interface WhatsAppAccountSummary extends WhatsAppAccountProfile {
  host: WhatsAppHostInfo;
  enabled: boolean;
  pending_count: number;
  runtime_status: 'idle' | 'starting' | 'running' | 'closing' | 'error';
  runtime_instance_id: string | null;
  error?: string;
}
export interface WhatsAppLauncherSnapshot {
  accounts: WhatsAppAccountSummary[];
  merchants: WhatsAppMerchantChoice[];
}
export interface WhatsAppAccountSnapshot {
  profile: WhatsAppAccountProfile;
  host: WhatsAppHostInfo;
  state: WhatsAppAccountState | null;
  merchants: WhatsAppMerchantChoice[];
  runtime_instance_id: string | null;
}
export interface WhatsAppRenameInput { workspace_id: string; label: string; expected_revision: number }
export interface WhatsAppBindingInput extends WhatsAppIdentityInput {
  chat_id: string;
  merchant_id: string | null;
  conversation_id: string | null;
  expected_revision: number;
}
export interface WhatsAppDefaultInput extends WhatsAppIdentityInput {
  merchant_id: string | null;
  expected_revision: number;
}
export interface WhatsAppReceptionInput extends WhatsAppIdentityInput {
  enabled: boolean;
  allowed_merchant_ids: string[];
}
export interface WhatsAppTakeoverInput extends WhatsAppIdentityInput {
  chat_id: string;
  enabled: boolean;
}
export interface WhatsAppWritebackSource {
  account_id: string;
  chat_id: string;
  message_ids: string[];
  source_versions: Record<string, string>;
  segment_id: string;
  merchant_id: string;
  binding_revision: number;
  content_sha256: string;
}
export interface WhatsAppWritebackTarget {
  destination: 'merchant_workbench' | 'international_platform';
  merchant_id: string;
  seller_id: string;
  data_root: string;
  conversation_id: string;
  object_kind: 'summary' | 'requirement_changes' | 'note' | 'message_reference';
  expected_revision: string;
}
/** Reserved contract. No preview, confirmation or mutation is enabled in 0.4.1. */
export interface WhatsAppWritebackPlanRequest {
  format: 'wa-writeback-plan-v1';
  source: WhatsAppWritebackSource;
  target: WhatsAppWritebackTarget;
  payload: Record<string, unknown>;
  payload_sha256: string;
  idempotency_key: string;
}
export interface WhatsAppWritebackPreview {
  format: 'wa-writeback-preview-v1';
  preview_id: string;
  source_sha256: string;
  target_revision: string;
  payload_sha256: string;
  expires_at_ms: number;
}
export interface WhatsAppWritebackSubmit {
  format: 'wa-writeback-submit-v1';
  preview_id: string;
  native_approval_id: string;
  payload_sha256: string;
  idempotency_key: string;
}
export interface WhatsAppWritebackReceipt {
  format: 'wa-writeback-receipt-v1';
  operation_id: string;
  destination: WhatsAppWritebackTarget['destination'];
  status: 'unavailable' | 'prepared' | 'unknown' | 'accepted' | 'rejected';
  target_object_id: string | null;
  evidence_id: string | null;
}
export interface WhatsAppWritebackCapabilities {
  format: 'wa-writeback-capabilities-v1';
  available: false;
  enabled: false;
  reason: string;
  destinations: Array<'merchant_workbench' | 'international_platform'>;
  content_kinds: Array<'summary' | 'requirement_changes' | 'note' | 'message_reference'>;
}
export interface WhatsAppAccountBridge {
  getWhatsAppAccounts(): Promise<WhatsAppLauncherSnapshot>;
  getWhatsAppAccount(workspaceId: string): Promise<WhatsAppAccountSnapshot>;
  createWhatsAppAccount(label: string): Promise<WhatsAppAccountSnapshot>;
  renameWhatsAppAccount(input: WhatsAppRenameInput): Promise<WhatsAppAccountSnapshot>;
  connectWhatsAppAccount(workspaceId: string): Promise<WhatsAppAccountSnapshot>;
  openWhatsAppAccount(workspaceId: string): Promise<void>;
  disconnectWhatsAppAccount(workspaceId: string): Promise<WhatsAppAccountSnapshot>;
  readWhatsAppContacts(workspaceId: string): Promise<WhatsAppAccountSnapshot>;
  verifyWhatsAppLegacySources(workspaceId: string): Promise<WhatsAppAccountSnapshot>;
  saveWhatsAppAccountBinding(input: WhatsAppBindingInput): Promise<WhatsAppAccountSnapshot>;
  saveWhatsAppDefaultMerchant(input: WhatsAppDefaultInput): Promise<WhatsAppAccountSnapshot>;
  setWhatsAppAccountReception(input: WhatsAppReceptionInput): Promise<WhatsAppAccountSnapshot>;
  takeoverWhatsAppContact(input: WhatsAppTakeoverInput): Promise<WhatsAppAccountSnapshot>;
  reconcileWhatsAppAccountOperation(input: { workspace_id: string; operation_id: string }): Promise<WhatsAppAccountSnapshot>;
  getWhatsAppAccountContext(input: { workspace_id: string; chat_id: string }): Promise<WhatsAppContext>;
  getWhatsAppWritebackCapabilities(): Promise<WhatsAppWritebackCapabilities>;
}

/** Host-owned JSONL context lease; never supplied by a renderer or Chrome page. */
export interface WhatsAppMerchantContext {
  format: 'wa-merchant-context-v1';
  merchant_id: string;
  seller_id: string;
  data_root: string;
  instance_id: string;
  conversation_id: string | null;
  revision: string;
  observed_at_ms: number;
  expires_at_ms: number;
  context: {
    knowledge: unknown[];
    guidance: unknown[];
    style: unknown[];
    collection_rules: unknown;
    international_background?: WhatsAppContext['international_background'];
  };
}
export interface WhatsAppContextRequest {
  request_id: string;
  account_id: string;
  chat_id: string;
  merchant_id: string;
  conversation_id: string | null;
  binding_revision: number;
}
