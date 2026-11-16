import { contextBridge, ipcRenderer } from 'electron';
import { API_METHODS, type ReceptionBridge, type ServiceEvent } from '../shared/api';
import { validateChatParameters } from '../shared/chat-protocol';
import { SUPPLEMENT_MAX_FILES, SUPPLEMENT_MAX_ENCODED_BYTES, SUPPLEMENT_MAX_TOTAL_BYTES } from '../shared/media-limits';
const allowed = new Set<string>(API_METHODS);
function checkConversation(value: unknown): asserts value is string {
  if (typeof value !== 'string' || !value || value.length > 500) throw new Error('客户会话无效。');
}
const bridge: ReceptionBridge = {
  saveFeishuConfiguration: input => ipcRenderer.invoke('reception:feishu:save', input),
  grantFeishuWrite: input => ipcRenderer.invoke('reception:feishu:grant', input),
  getModelSummary: () => ipcRenderer.invoke('reception:models:summary'),
  openModelLauncher: () => ipcRenderer.invoke('reception:models:launcher'),
  openLauncher: () => ipcRenderer.invoke('reception:launcher:open'),
  onExitDialog: listener => {
    const handler = (_event: unknown, value: import('../shared/exit-dialog').ExitDialogEvent) => listener(value);
    ipcRenderer.on('reception:exit-dialog', handler);
    return () => ipcRenderer.removeListener('reception:exit-dialog', handler);
  },
  respondExitDialog: response => ipcRenderer.invoke('reception:exit-dialog:respond', response),
  getImageOriginal: (reference, requestId) => ipcRenderer.invoke('reception:image:original', reference, requestId),
  cancelImageOriginal: requestId => ipcRenderer.invoke('reception:image:cancel', requestId),
  showImageMenu: reference => ipcRenderer.invoke('reception:image:menu', reference),
  openProductLink: reference => ipcRenderer.invoke('reception:product:open', reference),
  copyProductText: reference => ipcRenderer.invoke('reception:product:copy-text', reference),
  invoke: (method, params = {}) => {
    if (!allowed.has(method)) return Promise.reject(new Error('不允许此界面操作。'));
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
  chooseWorkctl: () => ipcRenderer.invoke('reception:choose-workctl'),
  chooseSupplementAttachment: conversationId => ipcRenderer.invoke('reception:choose-supplement-attachment', conversationId),
  chooseSupplementAttachments: conversationId => {
    checkConversation(conversationId);
    return ipcRenderer.invoke('reception:choose-supplement-attachments', conversationId);
  },
  uploadSupplementAttachments: (conversationId, files) => {
    checkConversation(conversationId);
    if (!Array.isArray(files) || files.length > SUPPLEMENT_MAX_FILES || files.some(file => !file || typeof file.name !== 'string' || file.name.length > 255 || typeof file.content_b64 !== 'string' || file.content_b64.length > SUPPLEMENT_MAX_ENCODED_BYTES)) return Promise.reject(new Error('附件参数或大小无效。'));
    if (files.reduce((sum, file) => sum + file.content_b64.length, 0) > Math.ceil(SUPPLEMENT_MAX_TOTAL_BYTES / 3) * 4 + SUPPLEMENT_MAX_FILES * 4) return Promise.reject(new Error('附件参数或大小无效。'));
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
