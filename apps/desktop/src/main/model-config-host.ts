import type { ModelRuntimePacket, ModelSettingsSaveInput, ModelSettingsSnapshot } from '../shared/model-settings';
import { ModelSettingsStore } from './model-settings-store';

type Invoke = (method: string, params: Record<string, unknown>) => Promise<unknown>;
/** Private host boundary. Never expose runtime() or this invoke function to a renderer. */
export class ModelConfigHost {
  constructor(readonly store: ModelSettingsStore, private readonly invoke: Invoke) {}
  read(): Promise<ModelSettingsSnapshot> { return this.store.read(); }
  async save(input: ModelSettingsSaveInput): Promise<ModelSettingsSnapshot> { return this.store.save(input); }
  async synchronize(): Promise<void> {
    const packet: ModelRuntimePacket = await this.store.runtime();
    await this.invoke('_configure_models', packet as unknown as Record<string, unknown>);
  }
}
