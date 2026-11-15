export interface TranslationSettings {
  enabled: boolean;
  base_url: string;
  model: string;
  revision: number;
}

export interface TranslationUsage {
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  unknown_requests: number;
  cache_hits: number;
}

export interface TranslationInfo extends TranslationSettings {
  has_key: boolean;
  status: 'not_configured' | 'disabled' | 'ready' | 'checking' | 'failed' | 'demo';
  message: string;
  usage: TranslationUsage;
  /** Connection fields are shared by this Windows user; enabled remains local. */
  connection_scope?: 'shared';
  shared_revision?: number;
}

export interface TranslationSettingsInput extends TranslationSettings {
  /** Expected revision for the settings update. */
  revision: number;
  api_key?: string;
  clear_key?: boolean;
  /** Expected revision of the Windows-user shared connection. */
  shared_revision?: number;
}

export interface TranslationHostBridge {
  getTranslationSettings(): Promise<TranslationInfo>;
  setTranslationEnabled(enabled: boolean): Promise<TranslationInfo>;
  importLegacyTranslation(): Promise<void>;
  saveTranslationSettings(value: TranslationSettingsInput): Promise<TranslationInfo>;
  checkTranslationProvider(): Promise<{ job_id: string }>;
  clearTranslationCache(): Promise<{ removed: number }>;
}
