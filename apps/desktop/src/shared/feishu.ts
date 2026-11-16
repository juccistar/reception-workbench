export interface FeishuConfiguration {
  enabled: boolean;
  app_id: string;
  table_url: string;
  fields: Record<string, { name: string; type: string }>;
  account_option: string;
  initial_status: string;
}

// The host must revoke the current write grant before changing either setting or credential.
// A blank secret retains the existing secret; the secret is never returned to the renderer.
export interface FeishuBridge {
  saveFeishuConfiguration(input: { settings: FeishuConfiguration; app_secret: string; expected_revision: number }): Promise<boolean>;
  grantFeishuWrite(input: { enabled: boolean; expected_revision: number }): Promise<boolean>;
}
