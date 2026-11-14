export interface WhatsAppContact { chat_id: string; name: string }
export interface WhatsAppBinding { chat_id: string; conversation_id: string | null; revision: number }
export interface WhatsAppConversation extends WhatsAppContact { human_owned: boolean; pending: boolean }
export interface WhatsAppOperation { operation_id: string; chat_id: string; status: string; message_id?: string | null; ack?: number | null }
export interface WhatsAppReplyBatch { batch_id: string; chat_id: string; status: string; total: number; accepted: number; current_index: number | null; pending_operation_ids: string[] }
export interface WhatsAppState {
  connected: boolean;
  account_id: string | null;
  connection_epoch: string | null;
  enabled: boolean;
  conversations: WhatsAppConversation[];
  bindings: WhatsAppBinding[];
  pending_operations: WhatsAppOperation[];
  other_account_pending_count?: number;
  other_account_pending_operations?: { operation_id: string; status: string }[];
  reply_batches?: WhatsAppReplyBatch[];
}
export interface WhatsAppContextMessage { source_id: string; role: 'buyer' | 'seller'; timestamp_ms: number; text: string }
export interface WhatsAppContext {
  task_mode: 'whatsapp_reply';
  whatsapp_messages: WhatsAppContextMessage[];
  wa_summary: string;
  knowledge: unknown[];
  guidance: unknown[];
  style: unknown[];
  binding_revision: number;
  international_background?: {
    messages: WhatsAppContextMessage[];
    summary: { body?: string } | null;
    requirements: Record<string, unknown>;
    supplement: { body?: string; attachments?: { id: string; name: string; text?: string }[] };
  };
}
export type WhatsAppPhase = 'page_reload' | 'view_setup' | 'cdp_connect' | 'auth_setup' | 'version_cache' | 'page_navigation' | 'app_modules' | 'sdk_inject' | 'sdk_callbacks' | 'qr_wait' | 'authenticated' | 'ready';
export type WhatsAppPageState = 'loading' | 'available' | 'error' | 'closed';
export type WhatsAppHostErrorCode = 'page_database_error' | 'http_error' | 'network_error' | 'tls_error' | 'navigation_timeout' | 'sdk_timeout' | 'ready_timeout' | 'auth_failed' | 'sdk_error' | 'invalid_response' | 'page_closed' | 'unexpected_navigation' | 'browser_unsupported' | 'cdp_error' | 'sdk_resource_missing' | 'page_context_lost' | 'app_module_error';
export interface WhatsAppHostInfo {
  status: string;
  account_id: string;
  connection_epoch: string;
  error_code?: WhatsAppHostErrorCode;
  http_status?: number;
  page_state?: WhatsAppPageState;
  phase?: WhatsAppPhase;
  previous_phase?: WhatsAppPhase;
  diagnostic_id?: string;
}
export interface WhatsAppViewRequest { visible: boolean; bounds?: { x: number; y: number; width: number; height: number } }
export interface WhatsAppHostBridge {
  getWhatsAppHostInfo(): Promise<WhatsAppHostInfo>;
  connectWhatsApp(): Promise<WhatsAppHostInfo>;
  rebuildWhatsAppSession(): Promise<WhatsAppHostInfo | null>;
  disconnectWhatsApp(): Promise<void>;
  setWhatsAppView(request: WhatsAppViewRequest): Promise<void>;
  listWhatsAppChats(): Promise<WhatsAppContact[]>;
}
