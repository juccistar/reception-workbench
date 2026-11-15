import { contextBridge, ipcRenderer } from 'electron';
import type { LauncherBridge } from '../shared/launcher';

const bridge: LauncherBridge = Object.freeze<LauncherBridge>({
  getWhatsAppAccounts: () => ipcRenderer.invoke('launcher:wa:get'),
  createWhatsAppAccount: label => ipcRenderer.invoke('launcher:wa:create', label),
  selectWhatsAppAccount: workspaceId => ipcRenderer.invoke('launcher:wa:select', workspaceId),
  connectWhatsAppAccount: () => ipcRenderer.invoke('launcher:wa:connect'),
  openWhatsAppAccount: () => ipcRenderer.invoke('launcher:wa:open'),
  disconnectWhatsAppAccount: () => ipcRenderer.invoke('launcher:wa:disconnect'),
  readWhatsAppContacts: () => ipcRenderer.invoke('launcher:wa:contacts'),
  verifyWhatsAppLegacySources: () => ipcRenderer.invoke('launcher:wa:verify-legacy'),
  saveWhatsAppAccountBinding: input => ipcRenderer.invoke('launcher:wa:binding', input),
  saveWhatsAppDefaultMerchant: input => ipcRenderer.invoke('launcher:wa:default', input),
  setWhatsAppAccountReception: input => ipcRenderer.invoke('launcher:wa:reception', input),
  takeoverWhatsAppContact: input => ipcRenderer.invoke('launcher:wa:takeover', input),
  reconcileWhatsAppAccountOperation: operationId => ipcRenderer.invoke('launcher:wa:reconcile', operationId),
  getWhatsAppAccountContext: chatId => ipcRenderer.invoke('launcher:wa:context', chatId),
  getWhatsAppWritebackCapabilities: () => ipcRenderer.invoke('launcher:wa:writeback-capabilities'),
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
