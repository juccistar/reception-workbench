export interface WhatsAppContact { chat_id: string; name: string }
export interface WhatsAppBinding { chat_id: string; conversation_id: string | null; revision: number }
export interface WhatsAppConversation extends WhatsAppContact { human_owned: boolean; pending: boolean }
export interface WhatsAppOperation { operation_id: string; chat_id: string; status: string; message_id?: string | null; ack?: number | null }
export interface WhatsAppState {
  connected: boolean;
  account_id: string | null;
  connection_epoch: string | null;
  enabled: boolean;
  conversations: WhatsAppConversation[];
  bindings: WhatsAppBinding[];
  pending_operations: WhatsAppOperation[];
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
export type WhatsAppHostErrorCode = 'http_error' | 'network_error' | 'tls_error' | 'navigation_timeout' | 'sdk_timeout' | 'ready_timeout' | 'auth_failed' | 'sdk_error' | 'invalid_response' | 'page_closed' | 'unexpected_navigation';
export interface WhatsAppHostInfo { status: string; account_id: string; connection_epoch: string; error_code?: WhatsAppHostErrorCode; http_status?: number }
export interface WhatsAppViewRequest { visible: boolean; bounds?: { x: number; y: number; width: number; height: number } }
export interface WhatsAppHostBridge {
  getWhatsAppHostInfo(): Promise<WhatsAppHostInfo>;
  connectWhatsApp(): Promise<WhatsAppHostInfo>;
  disconnectWhatsApp(): Promise<void>;
  setWhatsAppView(request: WhatsAppViewRequest): Promise<void>;
  listWhatsAppChats(): Promise<WhatsAppContact[]>;
}
