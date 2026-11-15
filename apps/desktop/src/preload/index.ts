import { contextBridge, ipcRenderer } from 'electron';
import { API_METHODS, type ReceptionBridge, type ServiceEvent } from '../shared/api';
import { validateChatParameters } from '../shared/chat-protocol';
const allowed = new Set<string>(API_METHODS);
function checkConversation(value: unknown): asserts value is string {
  if (typeof value !== 'string' || !value || value.length > 500) throw new Error('客户会话无效。');
}
const bridge: ReceptionBridge = {
  getTranslationSettings: () => ipcRenderer.invoke('reception:translation:get'),
  setTranslationEnabled: enabled => ipcRenderer.invoke('reception:translation:enable', enabled),
  importLegacyTranslation: () => ipcRenderer.invoke('reception:translation:import-legacy'),
  clearTranslationCache: () => ipcRenderer.invoke('reception:translation:clear'),
  getModelSummary: () => ipcRenderer.invoke('reception:models:summary'),
  openModelLauncher: () => ipcRenderer.invoke('reception:models:launcher'),
  getImageOriginal: (reference, requestId) => ipcRenderer.invoke('reception:image:original', reference, requestId),
  cancelImageOriginal: requestId => ipcRenderer.invoke('reception:image:cancel', requestId),
  showImageMenu: reference => ipcRenderer.invoke('reception:image:menu', reference),
  openProductLink: reference => ipcRenderer.invoke('reception:product:open', reference),
  copyProductText: reference => ipcRenderer.invoke('reception:product:copy-text', reference),
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
  chooseSupplementAttachment: conversationId => ipcRenderer.invoke('reception:choose-supplement-attachment', conversationId),
  chooseSupplementAttachments: conversationId => {
    checkConversation(conversationId);
    return ipcRenderer.invoke('reception:choose-supplement-attachments', conversationId);
  },
  uploadSupplementAttachments: (conversationId, files) => {
    checkConversation(conversationId);
    if (!Array.isArray(files) || files.length > 5 || files.some(file => !file || typeof file.name !== 'string' || file.name.length > 255 || typeof file.content_b64 !== 'string' || file.content_b64.length > 8 * 1024 * 1024)) return Promise.reject(new Error('附件参数或大小无效。'));
    return ipcRenderer.invoke('reception:upload-supplement-attachments', conversationId, files);
  },
  pasteSupplementAttachments: conversationId => {
    checkConversation(conversationId);
    return ipcRenderer.invoke('reception:paste-supplement-attachments', conversationId);
  },
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
