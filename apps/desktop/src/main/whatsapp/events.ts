import type { WhatsAppHostInfo } from '../../shared/whatsapp';

type AccountStatus = 'connecting' | 'qr' | 'authenticated' | 'ready' | 'disconnected' | 'error';
export type WhatsAppEvent =
  | ({ type: 'account'; status: AccountStatus } & WhatsAppHostInfo)
  | { type: 'messages'; account_id: string; connection_epoch: string; messages: WhatsAppMessage[] }
  | { type: 'ack'; message_id: string; ack: number; account_id: string; connection_epoch: string };
export interface WhatsAppMessage { id: string; chat_id: string; body: string; timestamp_ms: number; from_me: boolean; name: string; is_group: boolean; is_history: boolean; metadata_only?: boolean; ack?: number; has_image?: boolean }
