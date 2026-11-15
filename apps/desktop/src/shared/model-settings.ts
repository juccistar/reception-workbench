export const MODEL_CAPABILITIES = ['text', 'structure', 'single_image', 'multi_image', 'combined', 'translation'] as const;
export type ModelCapability = typeof MODEL_CAPABILITIES[number];
export type ModelPurpose = 'reception' | 'translation';
export interface ModelCapabilityState { status: 'not_checked' | 'passed' | 'failed'; checked_at: number | null }
export interface ModelProfile {
  profile_id: string; name: string; protocol: 'chat-completions' | 'anthropic'; base_url: string; model: string;
  credential_ref: string | null; has_key: boolean; config_revision: number; credential_revision: number;
  capability_revision: number; capabilities: Record<ModelCapability, ModelCapabilityState>;
}
export interface ModelSettingsSnapshot {
  format: 'model-settings-v1'; directory_revision: number; profiles: ModelProfile[];
  routes: Record<ModelPurpose, { profile_id: string | null; route_revision: number }>;
}
export interface ModelProfileEdit { profile_id: string; name: string; protocol: ModelProfile['protocol']; base_url: string; model: string; credential_ref: string | null }
export type ModelCredentialChange = { action: 'keep' | 'clear' } | { action: 'replace'; key: string };
export interface ModelSettingsSaveInput {
  expected_directory_revision: number; profiles: ModelProfileEdit[];
  routes: Record<ModelPurpose, string | null>; credential_changes: Record<string, ModelCredentialChange>;
}
export interface ModelRuntimePacket { format: 'model-runtime-v1'; authority_file: string; settings: ModelSettingsSnapshot; credentials: Record<string, string> }
