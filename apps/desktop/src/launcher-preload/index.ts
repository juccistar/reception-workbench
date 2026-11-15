import { contextBridge, ipcRenderer } from 'electron';
import type { LauncherBridge } from '../shared/launcher';

const bridge: LauncherBridge = Object.freeze({
  merchants: () => ipcRenderer.invoke('launcher:merchants'),
  importMerchants: () => ipcRenderer.invoke('launcher:import-merchants'),
  openMerchant: (id: string) => ipcRenderer.invoke('launcher:open', id),
  authorization: () => ipcRenderer.invoke('launcher:authorization'),
  copyAuthorization: () => ipcRenderer.invoke('launcher:copy-authorization'),
  modelSettings: () => ipcRenderer.invoke('launcher:models:get'),
  saveModelSettings: (input: unknown) => ipcRenderer.invoke('launcher:models:save', input),
  checkModel: (profileId: string, purpose: 'reception' | 'translation') => ipcRenderer.invoke('launcher:models:check', profileId, purpose),
  cancelModelCheck: (profileId: string, purpose: 'reception' | 'translation') => ipcRenderer.invoke('launcher:models:cancel', profileId, purpose),
});
contextBridge.exposeInMainWorld('launcher', bridge);
