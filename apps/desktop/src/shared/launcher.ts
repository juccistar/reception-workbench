export interface LauncherMerchant {
  id: string;
  name: string;
  account_tail: string;
  status: 'pending' | 'reauthorize' | 'opened';
  data_root?: string;
  verified_at?: number | null;
  company_name?: string;
  display_revision?: number;
}

import type { WhatsAppAccountBridge } from './whatsapp-account';

export interface LauncherBridge extends WhatsAppAccountBridge {
  getInitialPage?(): Promise<import('./launcher-navigation').LauncherPage>;
  onPageRequest?(listener: (page: import('./launcher-navigation').LauncherPage) => void): () => void;
  merchants(): Promise<LauncherMerchant[]>;
  importMerchants(): Promise<number>;
  openMerchant(id: string): Promise<void>;
  renameMerchant(input: { merchant_id: string; name: string; expected_revision: number }): Promise<void>;
  authorization(): Promise<string>;
  copyAuthorization(): Promise<void>;
  modelSettings(): Promise<import('./model-settings').ModelSettingsSnapshot>;
  saveModelSettings(input: unknown): Promise<import('./model-settings').ModelSettingsSnapshot>;
  checkModel(profileId: string, purpose: 'reception' | 'translation', revisions: { directory_revision: number; config_revision: number; credential_revision: number }): Promise<import('./model-settings').ModelSettingsSnapshot>;
  onCloseRequest(listener: (event: import('./exit-dialog').ExitDialogEvent) => void): () => void;
  respondClose(response: import('./exit-dialog').ExitDialogResponse): Promise<void>;
  cancelModelCheck(profileId: string, purpose: 'reception' | 'translation'): Promise<void>;
}

declare global { interface Window { launcher: LauncherBridge } }
