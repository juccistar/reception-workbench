export interface LauncherMerchant {
  id: string;
  name: string;
  account_tail: string;
  status: 'pending' | 'reauthorize' | 'opened';
  data_root?: string;
  verified_at?: number | null;
}

import type { WhatsAppAccountBridge } from './whatsapp-account';

export interface LauncherBridge extends WhatsAppAccountBridge {
  merchants(): Promise<LauncherMerchant[]>;
  importMerchants(): Promise<number>;
  openMerchant(id: string): Promise<void>;
  authorization(): Promise<string>;
  copyAuthorization(): Promise<void>;
  modelSettings(): Promise<import('./model-settings').ModelSettingsSnapshot>;
  saveModelSettings(input: unknown): Promise<import('./model-settings').ModelSettingsSnapshot>;
  checkModel(profileId: string, purpose: 'reception' | 'translation'): Promise<import('./model-settings').ModelSettingsSnapshot>;
  cancelModelCheck(profileId: string, purpose: 'reception' | 'translation'): Promise<void>;
}

declare global { interface Window { launcher: LauncherBridge } }
