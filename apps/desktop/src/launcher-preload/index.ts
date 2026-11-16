import { contextBridge, ipcRenderer } from 'electron';
import type { LauncherBridge } from '../shared/launcher';

const bridge: LauncherBridge = Object.freeze<LauncherBridge>({
  getWhatsAppAccounts: () => ipcRenderer.invoke('launcher:wa:get'),
  getWhatsAppAccount: workspaceId => ipcRenderer.invoke('launcher:wa:detail', workspaceId),
  createWhatsAppAccount: label => ipcRenderer.invoke('launcher:wa:create', label),
  renameWhatsAppAccount: input => ipcRenderer.invoke('launcher:wa:rename', input),
  connectWhatsAppAccount: workspaceId => ipcRenderer.invoke('launcher:wa:connect', workspaceId),
  openWhatsAppAccount: workspaceId => ipcRenderer.invoke('launcher:wa:open', workspaceId),
  disconnectWhatsAppAccount: workspaceId => ipcRenderer.invoke('launcher:wa:disconnect', workspaceId),
  readWhatsAppContacts: workspaceId => ipcRenderer.invoke('launcher:wa:contacts', workspaceId),
  verifyWhatsAppLegacySources: workspaceId => ipcRenderer.invoke('launcher:wa:verify-legacy', workspaceId),
  saveWhatsAppAccountBinding: input => ipcRenderer.invoke('launcher:wa:binding', input),
  saveWhatsAppDefaultMerchant: input => ipcRenderer.invoke('launcher:wa:default', input),
  setWhatsAppAccountReception: input => ipcRenderer.invoke('launcher:wa:reception', input),
  takeoverWhatsAppContact: input => ipcRenderer.invoke('launcher:wa:takeover', input),
  reconcileWhatsAppAccountOperation: input => ipcRenderer.invoke('launcher:wa:reconcile', input),
  getWhatsAppAccountContext: input => ipcRenderer.invoke('launcher:wa:context', input),
  getWhatsAppWritebackCapabilities: () => ipcRenderer.invoke('launcher:wa:writeback-capabilities'),
  merchants: () => ipcRenderer.invoke('launcher:merchants'),
  importMerchants: () => ipcRenderer.invoke('launcher:import-merchants'),
  openMerchant: id => ipcRenderer.invoke('launcher:open', id),
  renameMerchant: input => ipcRenderer.invoke('launcher:rename', input),
  authorization: () => ipcRenderer.invoke('launcher:authorization'),
  copyAuthorization: () => ipcRenderer.invoke('launcher:copy-authorization'),
  modelSettings: () => ipcRenderer.invoke('launcher:models:get'),
  saveModelSettings: input => ipcRenderer.invoke('launcher:models:save', input),
  checkModel: (profileId, purpose) => ipcRenderer.invoke('launcher:models:check', profileId, purpose),
  cancelModelCheck: (profileId, purpose) => ipcRenderer.invoke('launcher:models:cancel', profileId, purpose),
});
contextBridge.exposeInMainWorld('launcher', bridge);
