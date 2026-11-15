import type { ReplyBatchProgress } from './api';
import type { WhatsAppContact, WhatsAppContext, WhatsAppHostInfo } from './whatsapp';

/** Public projections contain routing metadata, never browser credentials or model keys. */
export interface WhatsAppAccountProfile {
  workspace_id: string;
  label: string;
  account_tail: string;
  selected: boolean;
}
export interface WhatsAppMerchantChoice {
  merchant_id: string;
  name: string;
  account_tail: string;
  available: boolean;
  reason?: string;
}
export interface WhatsAppIdentityInput {
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
  conversations: WhatsAppAccountConversation[];
  bindings: WhatsAppAccountBinding[];
  pending_operations: WhatsAppAccountOperation[];
  reply_batches: ReplyBatchProgress[];
  legacy: WhatsAppLegacyStatus;
  model: { ready: boolean; status: string; message: string };
}
export interface WhatsAppLauncherSnapshot {
  profiles: WhatsAppAccountProfile[];
  selected_workspace_id: string | null;
  host: WhatsAppHostInfo;
  state: WhatsAppAccountState | null;
  merchants: WhatsAppMerchantChoice[];
}
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
  createWhatsAppAccount(label: string): Promise<WhatsAppLauncherSnapshot>;
  selectWhatsAppAccount(workspaceId: string): Promise<WhatsAppLauncherSnapshot>;
  connectWhatsAppAccount(): Promise<WhatsAppLauncherSnapshot>;
  openWhatsAppAccount(): Promise<void>;
  disconnectWhatsAppAccount(): Promise<WhatsAppLauncherSnapshot>;
  readWhatsAppContacts(): Promise<WhatsAppLauncherSnapshot>;
  verifyWhatsAppLegacySources(): Promise<WhatsAppLauncherSnapshot>;
  saveWhatsAppAccountBinding(input: WhatsAppBindingInput): Promise<WhatsAppLauncherSnapshot>;
  saveWhatsAppDefaultMerchant(input: WhatsAppDefaultInput): Promise<WhatsAppLauncherSnapshot>;
  setWhatsAppAccountReception(input: WhatsAppReceptionInput): Promise<WhatsAppLauncherSnapshot>;
  takeoverWhatsAppContact(input: WhatsAppTakeoverInput): Promise<WhatsAppLauncherSnapshot>;
  reconcileWhatsAppAccountOperation(operationId: string): Promise<WhatsAppLauncherSnapshot>;
  getWhatsAppAccountContext(chatId: string): Promise<WhatsAppContext>;
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
