import { contextBridge, ipcRenderer } from 'electron';
import { API_METHODS, type ReceptionBridge, type ServiceEvent } from '../shared/api';
import { validateChatParameters } from '../shared/chat-protocol';
const allowed = new Set<string>(API_METHODS);
const bridge: ReceptionBridge = {
  invoke: (method, params = {}) => {
    if (!allowed.has(method) || method === 'confirm_send') return Promise.reject(new Error('不允许此界面操作。'));
    try { validateChatParameters(method, params); } catch (error) { return Promise.reject(error); }
    return ipcRenderer.invoke('reception:invoke', method, params);
  },
  onEvent: listener => {
    const handler = (_event: unknown, value: ServiceEvent) => listener(value);
    ipcRenderer.on('reception:event', handler);
    return () => ipcRenderer.removeListener('reception:event', handler);
  },
  getPreferences: () => ipcRenderer.invoke('reception:preferences:get'),
  savePreferences: partial => ipcRenderer.invoke('reception:preferences:save', partial),
  confirmSend: confirmation => ipcRenderer.invoke('reception:confirm-send', { draft_id: confirmation.draft_id, revision: confirmation.revision }),
  chooseWorkctl: () => ipcRenderer.invoke('reception:choose-workctl'),
  getStorageInfo: () => ipcRenderer.invoke('reception:storage:get'),
  chooseStorageDirectory: purpose => ipcRenderer.invoke('reception:storage:choose', purpose),
  openStorageDirectory: which => ipcRenderer.invoke('reception:storage:open', which),
  manageStorage: request => ipcRenderer.invoke('reception:storage:manage', request),
  getAccioLaunchInfo: () => ipcRenderer.invoke('reception:launch:get'),
  copyAccioLaunchInstructions: () => ipcRenderer.invoke('reception:launch:copy'),
  getUpdateInfo: () => ipcRenderer.invoke('reception:update:get'),
  saveUpdateSettings: settings => ipcRenderer.invoke('reception:update:settings', { feed_url: settings.feed_url }),
  checkForUpdates: () => ipcRenderer.invoke('reception:update:check'),
  downloadUpdate: () => ipcRenderer.invoke('reception:update:download'),
  installUpdate: () => ipcRenderer.invoke('reception:update:install'),
};
contextBridge.exposeInMainWorld('reception', Object.freeze(bridge));
