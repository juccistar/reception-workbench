export interface LauncherMerchant {
  id: string;
  name: string;
  account_tail: string;
  status: 'pending' | 'reauthorize' | 'opened';
  data_root?: string;
  verified_at?: number | null;
}

export interface LauncherBridge {
  merchants(): Promise<LauncherMerchant[]>;
  openMerchant(id: string): Promise<void>;
  authorization(): Promise<string>;
  copyAuthorization(): Promise<void>;
  modelSettings(): Promise<import('./model-settings').ModelSettingsSnapshot>;
  saveModelSettings(input: unknown): Promise<import('./model-settings').ModelSettingsSnapshot>;
  checkModel(profileId: string, purpose: 'reception' | 'translation'): Promise<import('./model-settings').ModelSettingsSnapshot>;
  cancelModelCheck(profileId: string, purpose: 'reception' | 'translation'): Promise<void>;
}

declare global { interface Window { launcher: LauncherBridge } }
